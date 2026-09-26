"""`sch run`: open a shell that jumps straight into the harness TUI in
the active backend worktree; quitting the harness closes the session (no lingering
remote shell).
"""

import sys

from .. import cli as cli_mod
from .. import gitnative
from .. import harness as harness_mod, sync as sync_mod
from .. import presence as presence_mod, procs, runtime, workspace
from ..config import die, runtime_arn


_USAGE = "usage: sch run <workspace> [--harness <opencode|claude|pi>] [--model <id>] [--branch <name>] [--continue]"

# aws CLI read timeout for `prepare-run` with --continue: the shim waits up to
# its own bound (240 s, SCH_PREPARE_RUN_READY_TIMEOUT) for a cold workspace to
# finish restoring before it resolves the session to resume (TASK-29). The
# margin covers the invocation overhead; AgentCore allows 15 min per
# synchronous request.
PREPARE_RUN_READ_TIMEOUT_S = 300


def _parse_args(args):
    usage_msg = _USAGE
    if args and args[0] in ("-h", "--help"):
        print(usage_msg)
        raise SystemExit(0)
    if not args or not args[0]:
        die(usage_msg)
    ws = args[0]
    workspace.validate_workspace_name(ws)
    rest, sync_options = sync_mod.parse_options(args[1:], usage_msg)

    harness_flag = ""
    model = ""
    branch_flag = ""
    continue_flag = False
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg == "--continue":
            continue_flag = True
            i += 1
            continue
        if arg == "--harness":
            if i + 1 >= len(rest):
                die("usage: --harness <opencode|claude|pi>")
            harness_flag = rest[i + 1]
            i += 2
            continue
        if arg == "--model":
            if i + 1 >= len(rest):
                die("usage: --model <id>")
            model = cli_mod.validate_model_or_die(rest[i + 1])
            i += 2
            continue
        if arg == "--variant":
            # OpenCode 2's TUI has no --variant flag (`opencode run` folds the
            # variant into `--model provider/model#variant`), so there is
            # nothing to forward to: fail fast with the remedy instead of the
            # generic unknown-option error. Set the effort in the TUI model
            # picker, or use `sch task --variant` for headless tasks.
            die(
                "'sch run' does not support --variant (the opencode TUI has "
                "no such flag); pick the effort in the TUI model picker, or "
                "run headless with 'sch task --variant <name>'"
            )
        if arg == "--branch":
            if i + 1 >= len(rest):
                die("usage: --branch <name>")
            branch_flag = gitnative.validate_branch_or_die(rest[i + 1])
            i += 2
            continue
        if arg == "--":
            break
        if arg.startswith("-"):
            die("unknown option '{}'".format(arg))
        die("unexpected positional argument '{}'".format(arg))

    # Git-native mode is mutually exclusive with the mirror sync options
    # (add-git-native-workflow design D5; same pattern as --no-sync).
    gitnative.check_flag_exclusion_or_die(branch_flag, sync_options)

    return ws, harness_flag, model, branch_flag, continue_flag, sync_options


def cmd_run(cfg, args):
    ws, harness_flag, model, branch_flag, continue_flag, sync_options = _parse_args(args)
    procs.require_command(
        "agentcore", "install it with: npm install -g @aws/agentcore"
    )

    # provisioning=True: `run` boots a microVM, so it is one of the commands
    # allowed to rotate a session superseded by a runtime version change
    # (add-task-liveness-safety design D3).
    resolved = harness_mod.resolve_harness(
        cfg, ws, harness_flag, sync_options["storage"], provisioning=True
    )
    sid = resolved.sid
    harness = resolved.harness
    storage_backend = resolved.storage
    session_epoch = resolved.epoch
    remote_repo = (
        "/home/sch/workspace/repo"
        if storage_backend == "s3" else "/mnt/workspace/repo"
    )
    runtime_workspace = resolved.identity or ws

    # Git-native mode (add-git-native-workflow): resolved BEFORE the sync
    # binding — a git-native workspace never starts a sync helper, and the
    # cross-mode conflicts die here with the current mode and the remedy.
    git_mode = gitnative.resolve_mode(cfg, ws, runtime_workspace, branch_flag, sync_options)
    sync_root = None
    if git_mode is None:
        sync_root = sync_mod.resolve_binding(cfg, runtime_workspace, sync_options)
        if sync_root:
            print("sch: sync binding selected: {}".format(sync_root), file=sys.stderr)
    hint = "fresh" if resolved.was_created else "resumed"
    arn = runtime_arn(cfg)
    shell_id = presence_mod.new_shell_id()
    presence = presence_mod.PresenceLease(
        cfg, sid, shell_id, runtime_workspace, harness, storage_backend, session_epoch
    )

    print(
        "sch: provisioning microVM (storage hint: {}, harness: {})...".format(
            hint, harness
        ),
        file=sys.stderr,
    )
    warmup = runtime.invoke_verified(
        cfg, sid, runtime.payload_noop(
            runtime_workspace, harness, hint, storage_backend, session_epoch
        ), "run-warmup"
    )
    storage_error = runtime.storage_verification_error(warmup, storage_backend)
    if storage_error:
        die(storage_error)

    if git_mode is not None:
        # The TUI opens only after a completed seed (spec: "the harness starts
        # only after seed completion"). On a seeded workspace this reuses the
        # recorded branch/base without transferring anything.
        gitnative.ensure_seeded(
            cfg, ws, git_mode, arn, sid, runtime_workspace,
            harness, storage_backend, session_epoch,
        )

    exec_args = [
        "agentcore", "exec", "--it", "--runtime", arn,
        "--session-id", sid, "--region", cfg.region,
        "--shell-id", shell_id,
    ]

    def arm_and_open():
        # A synced run reaches this only after the helper's barrier-backed
        # ready record, so the TUI cannot observe an incomplete worktree.
        prepare_kwargs = {}
        if continue_flag:
            # With --continue the shim resolves the session to resume only
            # once the workspace is ready (TASK-29): on a cold boot right
            # after `sch stop` that means waiting for the restore, up to the
            # shim's bound. Raise the aws CLI read timeout (default 60 s)
            # above that bound so the pause is a pause, not a failed call.
            prepare_kwargs["read_timeout_s"] = PREPARE_RUN_READ_TIMEOUT_S
            print(
                "sch: resolving the {} session to resume (on a cold boot this "
                "waits for the workspace restore)...".format(harness),
                file=sys.stderr,
            )
        result = runtime.invoke_verified(
            cfg, sid, runtime.payload_prepare_run(
                runtime_workspace, harness, storage_backend, session_epoch,
                model=model, continue_flag=continue_flag,
            ), "run", **prepare_kwargs
        )
        if not result.ok:
            die("prepare-run invocation failed")
        prep_status = result.get("status", "unknown")
        if prep_status != "ok":
            # The shim's own error text (e.g. the workspace still restoring
            # after the bound with --continue) is the actionable part; fall
            # back to the raw response when there is none.
            detail = result.get("error", "") or ""
            if detail:
                print("sch: prepare-run error: {}".format(detail), file=sys.stderr)
            else:
                print("sch: prepare-run response: {}".format(result.raw_text), file=sys.stderr)
            die("cannot arm harness autostart (status={})".format(prep_status))
        if model and result.get("model", "") != model:
            print(
                "sch: warning: runtime did not echo requested model '{}'; "
                "the runtime image may not support --model (continuing)".format(model),
                file=sys.stderr,
            )
        if continue_flag:
            if result.get("continue", False) is not True:
                print(
                    "sch: warning: runtime did not echo requested --continue; "
                    "the runtime image may not support it (opening a fresh session)",
                    file=sys.stderr,
                )
            elif not result.get("session_id", ""):
                print(
                    "sch: no prior {} session to resume; opening a fresh one".format(harness),
                    file=sys.stderr,
                )
        runtime.invoke_best_effort(
            cfg, sid, runtime.payload_mark_interactive(
                runtime_workspace, harness, True, storage_backend, session_epoch
            )
        )
        print(
            "sch: opening '{}' straight into {} in {} (session {})".format(
                ws, harness, remote_repo, sid
            ),
            file=sys.stderr,
        )
        print("sch: quitting {} closes the session; detach instead with Ctrl+]".format(harness), file=sys.stderr)
        print(
            "sch: reconnect with: sch shell {} --shell-id {}".format(ws, shell_id),
            file=sys.stderr,
        )
        workspace.mark_status(cfg, ws, "run-opened")

    if not sync_root:
        arm_and_open()
        procs.exec_or_wait(exec_args, presence=presence)
    try:
        # The watch helper emits ready only after its initial barrier, so one
        # tunnel both gates the TUI and owns the subsequent live-sync lease.
        return procs.supervise_interactive(
            sync_mod.helper_argv(
                cfg, arn, sid, runtime_workspace, sync_root,
                sync_mod.baseline_path(cfg, runtime_workspace),
                sync_options["bootstrap"], sync_options["conflict"], "watch",
                storage_backend,
                session_epoch,
            ),
            exec_args,
            after_ready=arm_and_open,
            presence=presence,
        )
    except procs.SyncStartError as exc:
        die("sync preflight failed: {}".format(exc))
