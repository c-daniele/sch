"""AWS teardown primitives shared by `sch destroy`.

Every call goes through the `aws` CLI, like the rest of this client: the
package stays stdlib-only and inherits the operator's credential chain
(profile, SSO, MFA session) with no boto3 dependency and no second
credential-resolution story to explain.

Two rules the helpers here exist to enforce:

- **Absence is success.** A teardown is re-runnable, so every function
  reports "already absent" instead of failing when the resource is gone.
  A partially deleted account must be finishable by running the command
  again.
- **Versioned buckets need a purge, not `rm`.** `s3 rm --recursive` only
  writes delete markers in a versioned bucket (the checkpoint bucket is
  versioned by design), so the bucket would refuse to be deleted. Every
  version and delete marker is removed explicitly.
"""

import json
import re
import subprocess
import urllib.request


class TeardownError(Exception):
    """A step failed in a way the caller should report but survive."""


def _aws(args, timeout=None):
    """Run an aws CLI command; returns (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            ["aws", *args], capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError:
        raise TeardownError("the aws CLI is not installed or not on PATH")
    except subprocess.TimeoutExpired:
        return 1, "", "timed out"
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def account_id(region):
    """The account the current credentials belong to, or "" when unknown.

    Printed before anything is deleted: a teardown pointed at the wrong
    account is the one mistake that cannot be undone, and the account is the
    only identifier that makes it obvious.
    """
    rc, out, _ = _aws([
        "sts", "get-caller-identity", "--query", "Account",
        "--output", "text", "--region", region,
    ])
    return out if rc == 0 and out and out != "None" else ""


def stack_exists(name, region):
    rc, _, _ = _aws([
        "cloudformation", "describe-stacks", "--stack-name", name,
        "--region", region, "--output", "text", "--query", "Stacks[0].StackName",
    ])
    return rc == 0


def bucket_exists(name, region):
    rc, _, _ = _aws(["s3api", "head-bucket", "--bucket", name, "--region", region])
    return rc == 0


def repository_exists(name, region):
    rc, _, _ = _aws([
        "ecr", "describe-repositories", "--repository-names", name,
        "--region", region, "--query", "repositories[0].repositoryName",
        "--output", "text",
    ])
    return rc == 0


def stack_delete_failure_reason(name, region):
    """The resource and message behind a DELETE_FAILED, or "".

    The CloudFormation waiter only reports that it "matched expected path
    DELETE_FAILED", which says nothing about the cause. The cause is one event
    away, and it is always the interesting part (a non-empty bucket, a
    permission, a resource in use).
    """
    rc, out, _ = _aws([
        "cloudformation", "describe-stack-events", "--stack-name", name,
        "--region", region, "--output", "json",
        "--query",
        "StackEvents[?ResourceStatus=='DELETE_FAILED']."
        "{id:LogicalResourceId,reason:ResourceStatusReason}",
    ])
    if rc != 0 or not out:
        return ""
    try:
        events = json.loads(out)
    except ValueError:
        return ""
    for event in events:
        reason = (event.get("reason") or "").strip()
        resource = event.get("id") or ""
        # The stack-level event only repeats the resource list; the resource's
        # own event carries the service message.
        if reason and resource and resource != name:
            return "{}: {}".format(resource, reason)
    return ""


def delete_stack(name, region, retain_resources=(), timeout=1800):
    """Delete a stack and wait for it. Returns a human-readable outcome."""
    if not stack_exists(name, region):
        return "already absent"
    args = ["cloudformation", "delete-stack", "--stack-name", name, "--region", region]
    if retain_resources:
        args += ["--retain-resources", *retain_resources]
    rc, _, err = _aws(args)
    if rc != 0:
        raise TeardownError("delete-stack {} failed: {}".format(name, err or rc))
    rc, _, err = _aws([
        "cloudformation", "wait", "stack-delete-complete",
        "--stack-name", name, "--region", region,
    ], timeout=timeout)
    if rc != 0:
        detail = stack_delete_failure_reason(name, region) or err or str(rc)
        raise TeardownError(
            "stack {} did not finish deleting — {}".format(name, detail)
        )
    return "deleted"


def purge_repository_images(name, region):
    """Delete every image in an ECR repository (a stack cannot delete a
    non-empty repository). Returns the number of images removed."""
    if not repository_exists(name, region):
        return 0
    rc, out, _ = _aws([
        "ecr", "list-images", "--repository-name", name, "--region", region,
        "--query", "imageIds", "--output", "json",
    ])
    if rc != 0 or not out:
        return 0
    try:
        image_ids = json.loads(out)
    except ValueError:
        return 0
    if not image_ids:
        return 0
    rc, _, err = _aws([
        "ecr", "batch-delete-image", "--repository-name", name, "--region", region,
        "--image-ids", json.dumps(image_ids), "--output", "json",
    ])
    if rc != 0:
        raise TeardownError("could not delete images of {}: {}".format(name, err or rc))
    return len(image_ids)


def delete_bucket(name, region, max_rounds=100):
    """Empty (all versions and delete markers) and delete a bucket."""
    if not bucket_exists(name, region):
        return "already absent"
    for _ in range(max_rounds):
        rc, out, _ = _aws([
            "s3api", "list-object-versions", "--bucket", name, "--region", region,
            "--query",
            "[Versions[].{Key:Key,VersionId:VersionId},"
            "DeleteMarkers[].{Key:Key,VersionId:VersionId}][]",
            "--output", "json",
        ])
        if rc != 0:
            raise TeardownError("could not list {}".format(name))
        try:
            objects = json.loads(out) if out else []
        except ValueError:
            objects = []
        if not objects:
            break
        # delete-objects caps at 1000 keys per call.
        for start in range(0, len(objects), 1000):
            chunk = objects[start:start + 1000]
            rc, _, err = _aws([
                "s3api", "delete-objects", "--bucket", name, "--region", region,
                "--delete", json.dumps({"Objects": chunk, "Quiet": True}),
                "--output", "json",
            ])
            if rc != 0:
                raise TeardownError(
                    "could not empty {}: {}".format(name, err or rc)
                )
    else:
        raise TeardownError(
            "{} still had objects after {} purge rounds".format(name, max_rounds)
        )
    rc, _, err = _aws(["s3api", "delete-bucket", "--bucket", name, "--region", region])
    if rc != 0:
        raise TeardownError("could not delete bucket {}: {}".format(name, err or rc))
    return "deleted"


def deregister_telegram_webhook(token, timeout=10):
    """Best-effort `deleteWebhook`.

    The registration lives on Telegram's side, not in AWS: deleting the stack
    alone would leave the bot pushing updates at a dead endpoint (and, worse,
    silently stealing them from any other deployment that re-registers later).
    Never fatal — the operator can always call deleteWebhook by hand.
    """
    if not token:
        return "skipped (no bot token available)"
    url = "https://api.telegram.org/bot{}/deleteWebhook".format(token)
    try:
        request = urllib.request.Request(url, data=b"", method="POST")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except Exception as exc:  # network, HTTP, JSON: all non-fatal here
        return "failed ({}) — remove it with: curl -X POST {}".format(
            type(exc).__name__, url.replace(token, "<TOKEN>")
        )
    return "deregistered" if payload.get("ok") else "refused by Telegram"


def read_setenv_token(repo_root):
    """The Telegram bot token from the operator's `infra/setenv.sh`, or "".

    `sch destroy` needs the token only to deregister the webhook. Reading it
    from the same file the deploy reads means the operator does not have to
    remember to export it for the teardown — the exact step that was easy to
    forget when this lived in a shell script.
    """
    if repo_root is None:
        return ""
    path = repo_root / "infra" / "setenv.sh"
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    match = re.search(
        r"""^\s*(?:export\s+)?TELEGRAM_BOT_TOKEN\s*=\s*["']?([^"'\s#]+)""",
        text,
        re.MULTILINE,
    )
    return match.group(1) if match else ""
