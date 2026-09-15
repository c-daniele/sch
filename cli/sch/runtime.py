"""AgentCore runtime invocation: payload builders + a thin wrapper around
``aws bedrock-agentcore invoke-agent-runtime``.

Rationale (design D2 of refactor-cli-python, publishable — also referenced
from docs/cli.md):

    `sch` deliberately delegates to the `aws` CLI instead of using boto3.
    `sch` is not a Python library but a process orchestrator: its job is
    to spawn external tools (`agentcore exec --it` is interactive and not
    replicable via an SDK; the tunnels are `node` processes), and
    delegating to `aws` is consistent with that design. This choice keeps
    the CLI at zero pip dependencies and inherits the user's entire
    credential configuration (profiles, SSO, MFA, `credential_process`,
    assume-role caching) without reimplementing it. Every invocation is an
    argv list with no shell (`subprocess.run([...])`, never `shell=True`):
    no injection surface. The layer is isolated in this module, so a
    future migration to boto3 would stay confined to a single module.
"""

import json
import os
import subprocess

from . import procs
from . import userenv
from .config import runtime_arn

# --- payload builders ------------------------------------------------------
#
# Every payload is built with `json.dumps` (never string interpolation),
# so user-controlled values (workspace names, prompts, timeouts) can never
# break out of their JSON string context (spec: cli-cross-platform,
# "Safe delegation to external processes").
#
# add-user-provider-keys (design D1, resolving the design's payload open
# question): the user's provider keys travel as ONE optional field of the
# ordinary payloads — `provider_keys` — instead of a dedicated `keys-update`
# action. A single mechanism then covers every path (warmup, task, git-seed,
# session-import, advisories) for free, and no command can arm a harness
# through an action that forgot to carry the keys. The field is omitted
# entirely when the user has none, so a payload without keys stays
# byte-for-byte what it was before this capability.


def _with_provider_keys(data, provider_keys):
    """Attach the allowlisted provider keys to a payload dict, in place.

    Absent/empty input adds NOTHING (not an empty object): the shim reads the
    absence of the field as "this user configured no provider keys" and stages
    an empty key set accordingly.
    """
    keys = userenv.select_provider_keys(provider_keys or {})
    if keys:
        data["provider_keys"] = keys
    return data


def _with_storage(data, storage_backend, session_epoch=0):
    if storage_backend:
        data["storage_backend"] = storage_backend
        data["session_epoch"] = session_epoch
    return data


def payload_noop(
    workspace, harness, storage_hint, storage_backend="", session_epoch=0,
    provider_keys=None,
):
    return json.dumps(
        _with_provider_keys(
            _with_storage({
                "action": "noop",
                "storage": storage_hint,
                "workspace": workspace,
                "harness": harness,
            }, storage_backend, session_epoch),
            provider_keys,
        )
    )


def payload_mark_interactive(
    workspace, harness, active, storage_backend="", session_epoch=0,
    provider_keys=None,
):
    return json.dumps(
        _with_provider_keys(
            _with_storage({
                "action": "mark-interactive",
                "workspace": workspace,
                "harness": harness,
                "active": bool(active),
            }, storage_backend, session_epoch),
            provider_keys,
        )
    )


def payload_presence(
    shell_id, attachment_id, state, ttl_s,
    workspace="", harness="", storage_backend="", session_epoch=0,
):
    data = _with_storage({
        "action": "command-shell-presence",
        "shellId": shell_id,
        "attachmentId": attachment_id,
        "state": state,
    }, storage_backend, session_epoch)
    data["ttl_s"] = ttl_s
    if workspace:
        data["workspace"] = workspace
    if harness:
        data["harness"] = harness
    return json.dumps(data)


def payload_prepare_run(
    workspace, harness, storage_backend="", session_epoch=0, model="",
    continue_flag=False, provider_keys=None,
):
    data = {"action": "prepare-run", "workspace": workspace, "harness": harness}
    if model:
        data["model"] = model
    if continue_flag:
        # Omit entirely when not requested so the no-flag payload stays
        # byte-for-byte identical (same pattern as `model`); an older shim
        # simply ignores the unknown key and arms a fresh TUI.
        data["continue"] = True
    return json.dumps(
        _with_provider_keys(
            _with_storage(
                data,
                storage_backend, session_epoch,
            ),
            provider_keys,
        )
    )


def payload_checkpoint(
    workspace, harness, storage_backend="", session_epoch=0, provider_keys=None,
):
    return json.dumps(
        _with_provider_keys(
            _with_storage(
                {"action": "checkpoint", "workspace": workspace, "harness": harness},
                storage_backend, session_epoch,
            ),
            provider_keys,
        )
    )


def payload_task(
    workspace, harness, prompt, continue_flag, timeout_s=None,
    storage_backend="", session_epoch=0, model="", variant="",
    provider_keys=None,
):
    data = _with_storage({
        "action": "task",
        "workspace": workspace,
        "harness": harness,
        "prompt": prompt,
        "continue": bool(continue_flag),
    }, storage_backend, session_epoch)
    if timeout_s is not None:
        data["timeout_s"] = timeout_s
    if model:
        # In-payload transport (design D1 of add-task-model-flag): the model
        # travels with the prompt; key omitted entirely when no model was
        # requested so the no-flag payload stays byte-for-byte identical.
        data["model"] = model
    if variant:
        # Same transport as `model` (spec: headless-task-execution R8a):
        # omitted entirely when not requested.
        data["variant"] = variant
    return json.dumps(_with_provider_keys(data, provider_keys))


def payload_info(
    workspace="", harness="", storage_backend="", session_epoch=0,
    provider_keys=None,
):
    data = {"action": "info"}
    if workspace:
        data["workspace"] = workspace
    if harness:
        data["harness"] = harness
    return json.dumps(
        _with_provider_keys(
            _with_storage(data, storage_backend, session_epoch), provider_keys
        )
    )


def payload_serve_ensure(
    workspace, harness, storage_backend="", session_epoch=0, provider_keys=None,
):
    return json.dumps(
        _with_provider_keys(
            _with_storage(
                {"action": "serve-ensure", "workspace": workspace, "harness": harness},
                storage_backend, session_epoch,
            ),
            provider_keys,
        )
    )


def payload_git_seed(
    workspace, harness, branch, storage_backend="", session_epoch=0,
    provider_keys=None, origin_url="",
):
    """`git-seed` action (add-git-native-workflow design D6): clone the repo
    from the staged seed bundle and provision the work branch. The bundle
    itself is transferred beforehand over the tunnel file channel; this
    payload only names the branch.

    ``origin_url`` (TASK-26) is the operator's local ``origin`` remote,
    sanitized to a credential-free github.com https URL (or "" when the
    local repo has no usable origin). The shim records it in the workspace
    state and, only while a GitHub token is staged, configures it as the
    remote's ``origin``. Omitted entirely when empty so the no-origin
    payload stays byte-for-byte identical."""
    data = _with_storage(
        {
            "action": "git-seed",
            "workspace": workspace,
            "harness": harness,
            "branch": branch,
        },
        storage_backend, session_epoch,
    )
    if origin_url:
        # Plain string assignment inside a json.dumps-built dict: no
        # interpolation, no breakout (same guarantee as every payload here).
        data["originUrl"] = origin_url
    return json.dumps(
        _with_provider_keys(data, provider_keys)
    )


def payload_git_snapshot(
    workspace, harness, storage_backend="", session_epoch=0, provider_keys=None,
):
    """`git-snapshot` action (design D6): mechanical snapshot + incremental
    delivery bundle. No inputs beyond the standard identity fields."""
    return json.dumps(
        _with_provider_keys(
            _with_storage(
                {"action": "git-snapshot", "workspace": workspace, "harness": harness},
                storage_backend, session_epoch,
            ),
            provider_keys,
        )
    )


def payload_session_import(
    workspace, harness, storage_backend="", session_epoch=0, provider_keys=None,
):
    return json.dumps(
        _with_provider_keys(
            _with_storage(
                {"action": "session-import", "workspace": workspace, "harness": harness},
                storage_backend, session_epoch,
            ),
            provider_keys,
        )
    )


# --- invocation wrapper ------------------------------------------------------


def _inject_provider_keys(cfg, payload):
    """Return ``payload`` carrying this user's provider keys.

    Central injection (spec: user-provider-keys, "Copertura di tutti i path di
    invoke"). Every payload reaches AgentCore through exactly one of the two
    functions below, so adding the keys HERE makes "no invoke path forgets the
    keys" a property of the code instead of a property of review discipline —
    including the advisory/warmup paths and any future action. It matters more
    than it looks: staging in the microVM is a TOTAL REPLACEMENT (design D3/D4,
    "ultimo invoke vince"), so an invoke that omitted the field would not just
    miss the keys, it would clear the ones a previous invoke had staged.

    Idempotent and never fatal: a payload that already carries the field (built
    explicitly through a ``provider_keys=`` argument) is returned untouched, and
    any problem — unparseable payload, a `cfg` without the attribute, an
    unreadable env file — degrades to the payload as it came in. No key value is
    ever logged here, and the returned string is only ever passed to the aws CLI
    argv.
    """
    try:
        keys = getattr(cfg, "provider_keys", None)
        if not keys:
            return payload
        data = json.loads(payload)
        if not isinstance(data, dict) or "provider_keys" in data:
            return payload
        return json.dumps(_with_provider_keys(data, keys))
    except Exception:  # noqa: BLE001 — a warmup must never die over this
        return payload


def invoke_best_effort(cfg, session_id, payload, timeout=None):
    """Fire-and-forget `invoke-agent-runtime` call: used for warmup
    (``noop``) and the ``mark-interactive`` advisory. Never raises and
    never fails the parent, mirroring the bash reference's ``|| true``.
    """
    arn = runtime_arn(cfg)
    payload = _inject_provider_keys(cfg, payload)
    out_path = procs.null_output_path()
    run_options = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if timeout is not None:
        run_options["timeout"] = timeout
    try:
        subprocess.run(
            [
                "aws",
                "bedrock-agentcore",
                "invoke-agent-runtime",
                "--cli-binary-format",
                "raw-in-base64-out",
                "--agent-runtime-arn",
                arn,
                "--runtime-session-id",
                session_id,
                "--payload",
                payload,
                "--region",
                cfg.region,
                out_path,
            ],
            **run_options,
        )
    except Exception:
        pass
    finally:
        if os.name != "posix":
            try:
                os.remove(out_path)
            except OSError:
                pass


class InvocationResult:
    """Result of a verified `invoke-agent-runtime` call.

    ``ok`` reflects whether the *invocation itself* succeeded (aws CLI
    exit code 0); it says nothing about the payload's own ``status``
    field, which callers inspect via :meth:`get`.
    """

    __slots__ = ("ok", "raw_text")

    def __init__(self, ok, raw_text):
        self.ok = ok
        self.raw_text = raw_text

    def get(self, key, default=None):
        try:
            data = json.loads(self.raw_text)
        except (json.JSONDecodeError, ValueError, TypeError):
            return default
        if not isinstance(data, dict):
            return default
        value = data.get(key, default)
        return default if value is None else value


def verify_storage(result, expected):
    """True only when a new runtime explicitly acknowledges the backend."""
    return result.ok and result.get("status") == "ok" and result.get("storage") == expected


def storage_verification_error(result, expected):
    """Describe why a runtime response did not acknowledge ``expected``."""
    if verify_storage(result, expected):
        return ""
    if not result.ok:
        return (
            "runtime warmup invocation failed before storage could be verified; "
            "retry the command and check AWS credentials/connectivity if it persists"
        )

    status = result.get("status")
    actual = result.get("storage")
    detail = result.get("error") or result.get("message")
    if status and status != "ok":
        suffix = ": {}".format(detail) if detail else ""
        return "runtime rejected storage='{}' (status='{}'){}".format(
            expected, status, suffix
        )
    if actual:
        return (
            "runtime acknowledged storage='{}', expected storage='{}'; "
            "stop stale runtime state or deploy the matching runtime image"
        ).format(actual, expected)
    return "runtime does not acknowledge storage='{}'; deploy the matching runtime image".format(
        expected
    )


def invoke_verified(cfg, session_id, payload, op, read_timeout_s=None):
    """Invoke the runtime and capture its JSON response for inspection.

    Unlike :func:`invoke_best_effort`, callers need to check the response
    (e.g. ``status == "ok"``) before proceeding, so failures are surfaced
    via the returned :class:`InvocationResult` rather than swallowed.

    ``read_timeout_s`` raises the aws CLI socket read timeout (default 60 s)
    for actions the shim may legitimately hold open longer, e.g. a
    ``prepare-run`` with ``continue`` that waits for a cold workspace to
    finish restoring before resolving the session (TASK-29). AgentCore itself
    allows up to 15 minutes per synchronous request.
    """
    arn = runtime_arn(cfg)
    payload = _inject_provider_keys(cfg, payload)
    with procs.temp_json_file(op) as out_path:
        argv = [
            "aws",
            "bedrock-agentcore",
            "invoke-agent-runtime",
            "--cli-binary-format",
            "raw-in-base64-out",
            "--agent-runtime-arn",
            arn,
            "--runtime-session-id",
            session_id,
            "--payload",
            payload,
            "--region",
            cfg.region,
        ]
        if read_timeout_s is not None:
            argv += ["--cli-read-timeout", str(int(read_timeout_s))]
        argv.append(out_path)
        try:
            result = subprocess.run(
                argv,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            return InvocationResult(ok=False, raw_text="")
        if result.returncode != 0:
            return InvocationResult(ok=False, raw_text="")
        try:
            with open(out_path, "r", encoding="utf-8") as fh:
                raw_text = fh.read()
        except OSError:
            raw_text = ""
        return InvocationResult(ok=True, raw_text=raw_text)
