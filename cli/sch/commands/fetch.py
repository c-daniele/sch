"""`sch fetch <workspace> [--push] [--force]`: collect the work of a
git-native session as a local branch (add-git-native-workflow design D3).

Flow: invoke the shim's `git-snapshot` action (mechanical service commit if
the remote worktree is dirty, incremental `base..branch` bundle in staging),
download the bundle over the tunnel file channel, import it into the local
repository with `git fetch <bundle> <branch>:<branch>` — fast-forward-only,
never touching the operator's current checkout. `--force` overwrites a
diverged local ref; `--push` forwards the imported branch to `origin` with
the operator's local git configuration and credentials (none of which ever
reach the remote workspace).

Named `fetch`, not `pull`: the command performs a fetch, not a merge — the
name must not lie (design D3).
"""

import os
import sys
import tempfile
from pathlib import Path

from .. import gitnative, harness as harness_mod, runtime, workspace
from ..config import die, runtime_arn

_USAGE = "usage: sch fetch <workspace> [--push] [--force]"


def _parse_args(args):
    if args and args[0] in ("-h", "--help"):
        print(_USAGE)
        raise SystemExit(0)
    if not args or not args[0]:
        die(_USAGE)
    ws = args[0]
    workspace.validate_workspace_name(ws)
    push = False
    force = False
    for arg in args[1:]:
        if arg == "--push":
            push = True
        elif arg == "--force":
            force = True
        else:
            die("unknown option '{}'".format(arg))
    return ws, push, force


def _git(repo, args):
    import subprocess

    try:
        return subprocess.run(
            ["git", "-C", str(repo)] + list(args),
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        die("git not found on PATH")


def cmd_fetch(cfg, args):
    ws, push, force = _parse_args(args)

    state = workspace.read_workspace_state(cfg, ws)
    if state is None:
        die("unknown workspace '{}' (see: sch list)".format(ws))
    if not state.git_native:
        die(
            "workspace '{}' is not in git-native mode; `sch fetch` collects work "
            "from sessions started with --branch (mirror-synced workspaces "
            "deliver through the sync itself)".format(ws)
        )
    branch = state.git_native.get("branch", "")
    local_repo = Path(state.git_native.get("localRepo", "") or ".")
    if not branch:
        die("workspace '{}' git-native state has no branch recorded".format(ws))
    if not (local_repo / ".git").exists():
        die(
            "local repository '{}' recorded at seed time is not a git repo "
            "anymore; cannot import the branch".format(local_repo)
        )

    # Delivery path, not a provisioning one: no runtime-version lookup and no
    # session rotation (add-task-liveness-safety task 2.5) — collecting work
    # must never abandon the session that produced it.
    resolved = harness_mod.resolve_harness(cfg, ws, "")
    sid = resolved.sid
    harness = resolved.harness
    storage_backend = resolved.storage
    session_epoch = resolved.epoch
    runtime_workspace = resolved.identity or ws

    # Warm up / verify the backend exactly like run/task do: fetch may be the
    # first invocation that revives a stopped microVM (checkpoint restore).
    warmup = runtime.invoke_verified(
        cfg, sid, runtime.payload_noop(
            runtime_workspace, harness, "resumed", storage_backend, session_epoch,
        ), "fetch-warmup"
    )
    storage_error = runtime.storage_verification_error(warmup, storage_backend)
    if storage_error:
        die(storage_error)

    print("sch: snapshotting remote worktree for '{}'...".format(ws), file=sys.stderr)
    snap = runtime.invoke_verified(
        cfg, sid, runtime.payload_git_snapshot(
            runtime_workspace, harness, storage_backend, session_epoch,
        ), "git-snapshot"
    )
    if not snap.ok:
        die("git-snapshot invocation failed")
    snap_status = snap.get("status", "unknown")
    if snap_status == "no-work":
        print(
            "sch: no work to fetch (branch '{}' has no commits beyond the seed "
            "base and the remote worktree is clean)".format(branch),
            file=sys.stderr,
        )
        return 0
    if snap_status != "ok":
        message = str(snap.get("message", "") or snap.get("error", ""))
        if "unknown action" in message:
            die(
                "the runtime image does not support git-native mode "
                "('git-snapshot' action missing); rebuild/update the runtime image"
            )
        die("remote snapshot failed: {}".format(message or "unknown error"))
    head_sha = snap.get("headSha", "")
    if snap.get("snapshotCommitted"):
        print(
            "sch: remote worktree was dirty — a service snapshot commit was "
            "created (author sch-session)",
            file=sys.stderr,
        )

    # Idempotence short-circuit: the local ref may already be at the remote
    # head (e.g. a repeated fetch after a delivered dirty snapshot, where the
    # snapshot itself reports ok because head != base).
    local_ref = _git(local_repo, ["rev-parse", "--verify", "-q", "refs/heads/{}".format(branch)])
    if local_ref.returncode == 0 and head_sha and local_ref.stdout.strip() == head_sha:
        print(
            "sch: branch '{}' is already up to date at {}".format(branch, head_sha[:12]),
            file=sys.stderr,
        )
        return 0

    fd, bundle_path = tempfile.mkstemp(prefix="sch-delivery-", suffix=".bundle")
    os.close(fd)
    try:
        gitnative.run_bundle_helper(
            gitnative.bundle_helper_argv(
                cfg, runtime_arn(cfg), sid, runtime_workspace, storage_backend,
                session_epoch, gitnative.DELIVERY_BUNDLE_NAME, download=bundle_path,
            ),
            "delivery bundle download",
        )

        refspec = "{b}:{b}".format(b=branch)
        if force:
            refspec = "+" + refspec
        imported = _git(local_repo, ["fetch", bundle_path, refspec])
        if imported.returncode != 0:
            detail = (imported.stderr or "").strip()
            if "non-fast-forward" in detail or "rejected" in detail:
                die(
                    "local branch '{}' has diverged from the session's history; "
                    "re-run with --force to overwrite the local ref, or merge "
                    "manually (git fetch {} {})".format(branch, bundle_path, branch)
                )
            if "refusing to fetch into branch" in detail:
                die(
                    "branch '{}' is currently checked out in {}; switch to another "
                    "branch (or use a worktree) and re-run `sch fetch`".format(
                        branch, local_repo
                    )
                )
            die("cannot import the delivery bundle: {}".format(detail))
    finally:
        try:
            os.remove(bundle_path)
        except OSError:
            pass

    final = _git(local_repo, ["rev-parse", "refs/heads/{}".format(branch)])
    final_sha = final.stdout.strip() if final.returncode == 0 else head_sha
    print(
        "sch: branch '{}' updated to {} in {}".format(
            branch, final_sha[:12], local_repo
        ),
        file=sys.stderr,
    )

    if push:
        # Only after a successful import; uses the operator's local git
        # configuration/credentials exclusively (design D3).
        print("sch: pushing '{}' to origin...".format(branch), file=sys.stderr)
        pushed = _git(local_repo, ["push", "origin", "{b}:{b}".format(b=branch)])
        if pushed.returncode != 0:
            die("git push origin {} failed: {}".format(
                branch, (pushed.stderr or "").strip()
            ))
        print("sch: pushed '{}' to origin".format(branch), file=sys.stderr)

    workspace.mark_status(cfg, ws, "fetched")
    return 0
