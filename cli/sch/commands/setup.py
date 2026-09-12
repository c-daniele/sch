"""`sch setup`: guided first-run bootstrap for the installed CLI.

Checks the local prerequisites, obtains the support repo (the checkout that
carries the `tunnel/*.js` helpers and `infra/deploy.sh`) when it is missing,
and reports whether the runtime stack is deployed — pointing at the exact
next command at every step.

This command is offline-first and non-destructive: it only runs AWS when
reporting stack status (a `describe-stacks` read), never deploys anything
unless `--deploy` is passed explicitly, and never touches workspaces.
"""

import argparse
import os
import shutil
import subprocess
import sys

from .. import repo
from ..config import die

_AWS_V2_MARKER = "aws-cli/2"


def _run(argv):
    """Run a command, returning (returncode, stdout+stderr); never raises."""
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return result.returncode, "{}{}".format(result.stdout, result.stderr)


class Check:
    """One named prerequisite check with a user-facing hint."""

    def __init__(self, name, ok, required, detail, hint):
        self.name = name
        self.ok = ok
        self.required = required
        self.detail = detail
        self.hint = hint


def check_prerequisites():
    """Collect prerequisite checks for the local machine.

    Returns a list of :class:`Check`. Never raises and never deploys:
    `docker` is warn-only (only local image builds need it; the default
    CodeBuild path does not) and the coding-agent CLIs (`opencode`) are
    warn-only (only some commands need them). `aws` CLI v2 is the only hard
    requirement.
    """
    checks = []
    checks.append(Check(
        "python", True, True, sys.version.split()[0], ""
    ))

    aws_bin = shutil.which("aws")
    aws_version = ""
    if aws_bin:
        rc, output = _run(["aws", "--version"])
        aws_version = output.strip().split()[0] if output.strip() else ""
        if _AWS_V2_MARKER not in output:
            checks.append(Check(
                "aws-cli", False, True, "AWS CLI v1 or unknown",
                "install AWS CLI v2 (https://aws.amazon.com/cli/)",
            ))
            aws_bin = None
    if aws_bin:
        checks.append(Check("aws-cli", True, True, aws_version, ""))
    else:
        checks.append(Check(
            "aws-cli", False, True, "not found",
            "install AWS CLI v2 (https://aws.amazon.com/cli/)",
        ))

    agentcore_bin = shutil.which("agentcore")
    agentcore_version = ""
    if agentcore_bin:
        rc, output = _run(["agentcore", "--version"])
        agentcore_version = output.strip() or "found"
    checks.append(Check(
        "agentcore", bool(agentcore_bin), True, agentcore_version,
        "npm install -g @aws/agentcore",
    ))

    node_bin = shutil.which("node")
    checks.append(Check(
        "node", bool(node_bin), True, "found" if node_bin else "not found",
        "install Node.js >= 18 (needed by the tunnel helpers)",
    ))

    git_bin = shutil.which("git")
    checks.append(Check(
        "git", bool(git_bin), True, "found" if git_bin else "not found",
        "install git (needed to obtain the support repo)",
    ))

    docker_bin = shutil.which("docker")
    checks.append(Check(
        "docker", bool(docker_bin), False,
        "found" if docker_bin else "not found",
        "install Docker (only needed for local image builds,"
        " 'sch deploy -l'; the default build runs on CodeBuild)",
    ))

    opencode_bin = shutil.which("opencode")
    checks.append(Check(
        "opencode", bool(opencode_bin), False,
        "found" if opencode_bin else "not found",
        "install opencode locally (needed by 'sch attach'; optional otherwise)",
    ))
    return checks


def stack_status(cfg):
    """Return (status, detail) for the runtime stack: deployed|missing|error.

    ``detail`` is the runtime ARN when deployed — deliberately terse: no
    full output dump (it carries account IDs the user does not need to
    re-read on every setup).
    """
    rc, output = _run([
        "aws", "cloudformation", "describe-stacks",
        "--stack-name", cfg.stack_name(),
        "--region", cfg.region,
        "--query", "Stacks[0].Outputs[?OutputKey=='RuntimeArn'].OutputValue",
        "--output", "text",
    ])
    if rc != 0:
        text = output.strip()
        if "ValidationError" in text or "does not exist" in text:
            return "missing", "stack {} not found in {}".format(
                cfg.stack_name(), cfg.region
            )
        return "error", text.splitlines()[0] if text else "aws call failed"
    return "deployed", output.strip()


def _clone_support_repo(url, destination):
    git_bin = shutil.which("git")
    if not git_bin:
        print(
            "sch: git not found — clone manually:\n"
            "  git clone {} '{}'".format(url, destination),
            file=sys.stderr,
        )
        return False
    print("sch: cloning support repo into '{}'".format(destination))
    result = subprocess.run(
        [git_bin, "clone", "--depth", "1", url, str(destination)]
    )
    return result.returncode == 0


def _deploy(repo_root):
    """Run the deploy script from the support checkout (POSIX only)."""
    script = repo_root / "infra" / "deploy.sh"
    if sys.platform == "win32" or not script.is_file():
        print(
            "sch: run the deployment from the support checkout:\n"
            "  cd '{}'\n"
            "  ./infra/deploy.sh".format(repo_root),
            file=sys.stderr,
        )
        return 1
    result = subprocess.run(["bash", str(script)])
    return result.returncode


def _git(root, *args):
    """Run git inside ``root``; returns (returncode, combined output)."""
    git_bin = shutil.which("git")
    if not git_bin:
        return 1, "git not found"
    return _run([git_bin, "-C", str(root), *args])


def refresh_support_repo(root):
    """Fast-forward the MANAGED support checkout; never fatal.

    Only the checkout `sch setup` itself created is touched. A `SCH_REPO_ROOT`
    override or a legacy checkout is somebody's working clone: pulling in it
    would be a surprise edit to code the user may be editing, so those are
    reported and left alone.

    Local modifications or a diverged history also stop the update: the point
    is to keep an untouched checkout current, not to resolve git situations
    behind the user's back.
    """
    if root != repo.managed_root():
        print("sch: support repo at '{}' is not managed by sch — not updating"
              " it (update it yourself when needed)".format(root))
        return True

    rc, _ = _git(root, "rev-parse", "--is-inside-work-tree")
    if rc != 0:
        print("sch: support repo at '{}' is not a git checkout — not updating"
              " it".format(root), file=sys.stderr)
        return False

    rc, dirty = _git(root, "status", "--porcelain")
    if rc == 0 and dirty.strip():
        print("sch: support repo has local changes — skipping the update"
              " (run 'git -C {} status' to see them)".format(root))
        return True

    print("sch: updating support repo at '{}'".format(root))
    rc, output = _git(root, "pull", "--ff-only")
    if rc != 0:
        first = output.strip().splitlines()[0] if output.strip() else "git pull failed"
        print("sch: could not update the support repo ({}) — continuing with"
              " the current checkout".format(first), file=sys.stderr)
        return False
    print("sch:   {}".format(output.strip().splitlines()[-1] if output.strip()
                             else "up to date"))
    return True


def ensure_tunnel_deps(root):
    """Install the tunnel's node_modules when missing. Returns True when ready.

    `sch setup` used to print the npm command and let the user run it, which
    left every `--sync`, `attach`, `web` and `acp` invocation broken until they
    noticed. Installing them is the whole point of a guided first run.
    """
    tunnel = root / "tunnel"
    if (tunnel / "node_modules").is_dir():
        print("sch: tunnel dependencies ok")
        return True
    npm_bin = shutil.which("npm")
    if not npm_bin:
        print(
            "sch: npm not found — install Node.js, then run:\n"
            "  (cd '{}' && npm install)".format(tunnel),
            file=sys.stderr,
        )
        return False
    print("sch: installing tunnel dependencies (npm install in '{}')".format(tunnel))
    result = subprocess.run([npm_bin, "install"], cwd=str(tunnel))
    if result.returncode != 0:
        print(
            "sch: npm install failed — retry manually:\n"
            "  (cd '{}' && npm install)".format(tunnel),
            file=sys.stderr,
        )
        return False
    print("sch: tunnel dependencies installed")
    return True


def cmd_setup(cfg, args):
    parser = argparse.ArgumentParser(
        prog="sch setup",
        description="Check prerequisites and prepare first use.",
        add_help=True,
    )
    parser.add_argument(
        "--repo", default="",
        help="support repo URL to clone (default: {})".format(
            repo.DEFAULT_REPO_URL
        ),
    )
    parser.add_argument(
        "--deploy", action="store_true",
        help="run infra/deploy.sh from the support checkout after checks",
    )
    opts = parser.parse_args(args)

    failed = False
    print("sch: checking prerequisites")
    for check in check_prerequisites():
        if check.ok:
            marker = "ok"
        elif check.required:
            marker = "MISSING"
            failed = True
        else:
            marker = "warn"
        print(
            "sch:   {:<10} {:<8} {}".format(check.name, marker, check.detail)
        )
        if not check.ok and check.hint:
            print("sch:              hint: {}".format(check.hint))
    if failed:
        print(
            "sch: required prerequisites are missing — resolve them and re-run"
            " 'sch setup'",
            file=sys.stderr,
        )
        return 1

    root = repo.repo_root()
    if root is None:
        url = opts.repo or _env_repo_url() or repo.DEFAULT_REPO_URL
        destination = repo.managed_root()
        if not _clone_support_repo(url, destination):
            return 1
        root = repo.repo_root()
        if root is None:
            print(
                "sch: cloned repo at '{}' is missing {} — layout unexpected".format(
                    destination, repo.MARKER
                ),
                file=sys.stderr,
            )
            return 1
    else:
        print("sch: support repo found at '{}'".format(root))
        refresh_support_repo(root)
    ensure_tunnel_deps(root)

    status, detail = stack_status(cfg)
    print("sch: runtime stack {}: {}".format(status, detail))
    if status == "missing":
        if opts.deploy:
            return _deploy(root)
        print(
            "sch: the AWS side is not deployed yet — run:\n"
            "  sch setup --deploy",
            file=sys.stderr,
        )
        return 1
    if status == "error":
        print(
            "sch: could not read stack status (aws credentials configured?)",
            file=sys.stderr,
        )
        return 1

    if opts.deploy:
        return _deploy(root)
    print("sch: setup complete — try 'sch shell <workspace>'")
    return 0


def _env_repo_url():
    return os.environ.get("SCH_REPO_URL") or ""
