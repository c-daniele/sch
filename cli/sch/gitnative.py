"""Git-native session mode (add-git-native-workflow).

Autonomous sessions started with ``--branch <name>`` skip the mirror sync
entirely: the local history travels to the remote workspace as a git bundle
over the tunnel file channel, the shim provisions the work branch before the
harness starts (design D2), and the work comes back as an incremental bundle
imported by ``sch fetch`` (design D3). No git credential ever leaves the
laptop and the flow needs no runtime outbound connectivity to the git
provider (spec git-native-workflow, "No git credentials in the remote
workspace").

This module owns:
  - ``--branch`` validation and the mutual-exclusion rules against the
    mirror sync (design D5);
  - the seed flow (bundle create -> upload -> `git-seed` -> persist state);
  - the delivery flow used by `sch fetch` (`git-snapshot` -> download ->
    local fast-forward import -> optional push);
  - the argv/controls for the Node bundle-transfer helper
    (tunnel/bundle.js), same pattern as sync.helper_argv.
"""

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from . import bundlexfer, runtime, sync as sync_mod, workspace
from .config import die

# Staging names shared with the shim (image/app/main.py SEED_BUNDLE_NAME /
# DELIVERY_BUNDLE_NAME) — keep in sync on any change.
SEED_BUNDLE_NAME = "seed.bundle"
DELIVERY_BUNDLE_NAME = "delivery.bundle"

# Remote bundle staging dir per storage backend (task 1.1): STATE_DIR/bundles
# under the backend's canonical workspace root, outside the repo worktree.

class GitNativeMode:
    """Resolved git-native mode for one invocation.

    ``seeded`` is True when the workspace already has a recorded seed
    (branch + baseSha + localRepo) — subsequent invocations, including
    ``--continue``, reuse it without re-seeding (design D5).
    """

    __slots__ = ("branch", "base_sha", "local_repo", "seeded")

    def __init__(self, branch, base_sha="", local_repo="", seeded=False):
        self.branch = branch
        self.base_sha = base_sha
        self.local_repo = local_repo
        self.seeded = seeded


def validate_branch_or_die(branch):
    """Validate ``branch`` as a git ref name via `git check-ref-format
    --branch`, dying (before any local or remote mutation) on an invalid
    name — spec git-native-workflow, "Nome branch non valido"."""
    if not branch:
        die("usage: --branch <name>")
    try:
        result = subprocess.run(
            ["git", "check-ref-format", "--branch", branch],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        die("git not found on PATH; --branch requires a local git installation")
    if result.returncode != 0:
        die("invalid --branch value '{}' (not a valid git branch name)".format(branch))
    return branch


def check_flag_exclusion_or_die(branch_flag, sync_options):
    """Die when ``--branch`` is combined with any mirror-sync option — same
    pattern as the ``--no-sync`` exclusion in sync.parse_options (design D5).
    Runs at parse time, before any local or remote mutation."""
    if branch_flag and (
        sync_options["sync"]
        or sync_options["bootstrap"] != "abort"
        or sync_options["conflict"] != "abort"
    ):
        die("--branch cannot be combined with --sync, --bootstrap, or --conflict")


def resolve_mode(cfg, ws, runtime_workspace, branch_flag, sync_options):
    """Resolve the invocation's session mode against the persisted workspace
    state (design D5). Returns a :class:`GitNativeMode` when the invocation
    is git-native (flag or persisted seed), else ``None`` (mirror-sync /
    no-sync flows unchanged). Dies on any cross-mode conflict, naming the
    current mode and the remedy."""
    state = workspace.read_workspace_state(cfg, ws)
    recorded = state.git_native if state is not None else None

    if recorded:
        branch = recorded.get("branch", "")
        if sync_options["sync"]:
            die(
                "workspace '{}' is in git-native mode on branch '{}'; --sync is "
                "unavailable here (collect the work with `sch fetch {}`, or use a "
                "new workspace name for mirror-synced sessions)".format(ws, branch, ws)
            )
        if branch_flag and branch_flag != branch:
            die(
                "workspace '{}' is already seeded on branch '{}' (cannot switch to "
                "'{}'; use a new workspace name for a different branch)".format(
                    ws, branch, branch_flag
                )
            )
        return GitNativeMode(
            branch=branch,
            base_sha=recorded.get("baseSha", ""),
            local_repo=recorded.get("localRepo", ""),
            seeded=True,
        )

    if not branch_flag:
        return None

    # A workspace with a saved mirror binding cannot flip to git-native:
    # the two modes disagree on the source of truth (design D5).
    if sync_mod.binding_path(cfg, runtime_workspace).is_file():
        die(
            "workspace '{}' has a mirror sync binding; --branch is unavailable "
            "here (keep using --sync/--no-sync on this workspace, or use a new "
            "workspace name for git-native sessions)".format(ws)
        )
    return GitNativeMode(branch=branch_flag, seeded=False)


# --- local git plumbing ---------------------------------------------------------


def _git(args, cwd=None):
    try:
        return subprocess.run(
            ["git"] + list(args),
            cwd=str(cwd) if cwd else None,
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        die("git not found on PATH")


def local_repo_root():
    """The toplevel of the git repository containing the current directory
    (the seed source). Dies when invoked outside a git worktree."""
    result = _git(["rev-parse", "--show-toplevel"])
    if result.returncode != 0:
        die(
            "--branch requires running from inside a git repository "
            "(the local HEAD is the seed source)"
        )
    return Path(result.stdout.strip())


# --- opt-in GitHub origin (TASK-26) -------------------------------------------
#
# The remote clone has no `origin` by default (spec git-native-workflow,
# "No git credentials in the remote workspace"). When the operator opts in
# to remote GitHub access, the seed records the local `origin` URL so the
# shim can configure it remotely — but only while a GITHUB_TOKEN is staged.
# The URL sent is always a credential-free github.com https URL: embedded
# userinfo is stripped, SSH forms are rewritten, anything else yields "".

_SSH_ORIGIN_RE = re.compile(
    r"^(?:ssh://git@github\.com(?::443)?/|git@github\.com:)"
    r"([^/\s]+)/([^/\s]+?)(\.git)?/?$",
    re.IGNORECASE,
)
_HTTPS_ORIGIN_RE = re.compile(
    r"^https://(?:[^/@\s]+@)?github\.com(?::443)?"
    r"/([^/\s]+)/([^/\s]+?)(\.git)?/?$",
    re.IGNORECASE,
)


def sanitize_origin_url(raw):
    """Normalize a local `origin` URL to a credential-free github.com https
    URL, or "" when it is not a usable GitHub remote. Pure function."""
    if not isinstance(raw, str):
        return ""
    text = raw.strip()
    if not text or len(text) > 2000 or any(c in text for c in " \t\r\n\0"):
        return ""
    match = _SSH_ORIGIN_RE.match(text)
    if match:
        owner, repo = match.group(1), match.group(2)
        suffix = ".git" if match.group(3) else ""
        if owner in (".", "..") or repo in (".", ".."):
            return ""
        return "https://github.com/{}/{}{}".format(owner, repo, suffix)
    match = _HTTPS_ORIGIN_RE.match(text)
    if match:
        owner, repo = match.group(1), match.group(2)
        suffix = ".git" if match.group(3) else ""
        if owner in (".", "..") or repo in (".", ".."):
            return ""
        return "https://github.com/{}/{}{}".format(owner, repo, suffix)
    return ""


def local_origin_url(repo_root):
    """The sanitized local `origin` URL for the seed, or "".

    Best-effort and never fatal: a repo without an `origin` (or with a
    non-GitHub one) simply seeds without a recorded URL, keeping the
    credential-less default."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(repo_root),
            capture_output=True, text=True,
        )
    except (OSError, ValueError):
        return ""
    if result.returncode != 0:
        return ""
    return sanitize_origin_url(result.stdout.strip())


# --- bundle helper (tunnel/bundle.js) -------------------------------------------


def staging_root(storage):
    return bundlexfer.staging_root(storage)


def bundle_helper_argv(
    cfg, runtime_arn, session_id, workspace_identity, storage, session_epoch,
    name, upload=None, download=None,
):
    """Compatibility wrapper; transfer implementation lives in bundlexfer."""
    return bundlexfer.bundle_helper_argv(
        cfg, runtime_arn, session_id, workspace_identity, storage, session_epoch,
        name, upload=upload, download=download,
    )


def run_bundle_helper(argv, doing):
    return bundlexfer.run_bundle_helper(argv, doing)


# --- seed flow (design D2, tasks 3.3/3.4) ---------------------------------------


def _die_if_unsupported(result, action):
    """Fail-fast when the runtime image predates the git-native actions
    (design D6): unlike --model there is no acceptable degraded path — a
    session without a seed has no worktree to work in."""
    message = str(result.get("message", "") or result.get("error", ""))
    if "unknown action" in message:
        die(
            "the runtime image does not support git-native mode ('{}' action "
            "missing); rebuild/update the runtime image before using --branch".format(action)
        )


def ensure_seeded(cfg, ws, mode, runtime_arn, sid, runtime_workspace, harness,
                  storage_backend, session_epoch):
    """Idempotent seed: on a seeded workspace this only prints the reused
    branch (no bundle, no remote action — task 3.4, includes --continue).
    On success the mode is updated in place and persisted in the workspace
    index. The caller starts the harness only after this returns."""
    if mode.seeded:
        print(
            "sch: git-native workspace '{}' on branch '{}' (base {}) — no re-seed".format(
                ws, mode.branch, (mode.base_sha or "")[:12]
            ),
            file=sys.stderr,
        )
        return mode

    repo_root = local_repo_root()

    dirty = _git(["status", "--porcelain"], cwd=repo_root)
    if dirty.returncode == 0 and dirty.stdout.strip():
        print(
            "sch: WARNING local worktree has uncommitted changes; they are NOT "
            "included in the seed (seeding from HEAD)",
            file=sys.stderr,
        )

    head = _git(["rev-parse", "HEAD"], cwd=repo_root)
    if head.returncode != 0:
        die("local repository has no HEAD commit to seed from")
    local_head = head.stdout.strip()

    # Opt-in GitHub origin (TASK-26): record the local `origin` when it is a
    # usable GitHub remote; "" keeps the credential-less default. Never fatal
    # and never a credential (sanitize_origin_url strips userinfo).
    origin_url = local_origin_url(repo_root)

    fd, bundle_path = tempfile.mkstemp(prefix="sch-seed-", suffix=".bundle")
    os.close(fd)
    try:
        create = _git(["bundle", "create", bundle_path, "HEAD"], cwd=repo_root)
        if create.returncode != 0:
            die("cannot create seed bundle: {}".format((create.stderr or "").strip()))

        size = os.path.getsize(bundle_path)
        print(
            "sch: seed bundle for '{}' is {:.1f} MB (full history from HEAD {})".format(
                ws, size / (1024 * 1024), local_head[:12]
            ),
            file=sys.stderr,
        )

        run_bundle_helper(
            bundle_helper_argv(
                cfg, runtime_arn, sid, runtime_workspace, storage_backend,
                session_epoch, SEED_BUNDLE_NAME, upload=bundle_path,
            ),
            "seed bundle upload",
        )
    finally:
        try:
            os.remove(bundle_path)
        except OSError:
            pass

    print("sch: provisioning branch '{}' in the remote workspace...".format(mode.branch), file=sys.stderr)
    result = runtime.invoke_verified(
        cfg, sid,
        runtime.payload_git_seed(
            runtime_workspace, harness, mode.branch, storage_backend, session_epoch,
            origin_url=origin_url,
        ),
        "git-seed",
    )
    if not result.ok:
        die("git-seed invocation failed; the task was not started")
    if result.get("status") != "ok":
        _die_if_unsupported(result, "git-seed")
        die("remote seed failed: {}".format(result.get("error", "unknown error")))

    base_sha = result.get("baseSha", "")
    if not base_sha:
        die("remote seed did not return a base sha")
    if base_sha != local_head:
        die(
            "remote seed base {} does not match the local HEAD {} — refusing to "
            "record an inconsistent binding".format(base_sha[:12], local_head[:12])
        )

    workspace.save_git_native_state(cfg, ws, mode.branch, base_sha, repo_root)
    mode.base_sha = base_sha
    mode.local_repo = str(repo_root)
    mode.seeded = True
    print(
        "sch: workspace '{}' seeded (branch '{}', base {})".format(
            ws, mode.branch, base_sha[:12]
        ),
        file=sys.stderr,
    )
    return mode
