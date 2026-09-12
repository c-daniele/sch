"""`sch shell` / `sch open`: open (or reconnect to) an interactive shell in
the workspace's microVM.
"""

import sys

from .. import harness as harness_mod, sync as sync_mod
from .. import presence as presence_mod, procs, runtime, workspace
from ..config import die, runtime_arn


def _parse_args(args, usage_msg):
    if not args or not args[0]:
        die(usage_msg)
    ws = args[0]
    workspace.validate_workspace_name(ws)
    rest, sync_options = sync_mod.parse_options(args[1:], usage_msg)

    harness_flag = ""
    shell_id = ""
    passthrough = []
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg == "--harness":
            if i + 1 >= len(rest):
                die("usage: --harness <opencode|claude|pi>")
            harness_flag = rest[i + 1]
            i += 2
            continue
        if arg == "--shell-id":
            if i + 1 >= len(rest):
                die("usage: --shell-id <id>")
            shell_id = rest[i + 1]
            i += 2
            continue
        if arg == "--":
            passthrough = rest[i + 1 :]
            break
        if arg.startswith("-"):
            die("unknown option '{}'".format(arg))
        die("unexpected positional argument '{}'".format(arg))

    return ws, harness_flag, shell_id, passthrough, sync_options


def cmd_shell(cfg, args):
    ws, harness_flag, shell_id, passthrough, sync_options = _parse_args(
        args, "usage: sch shell <workspace> [--harness <opencode|claude|pi>] [--shell-id <id>]"
    )
    procs.require_command(
        "agentcore", "install it with: npm install -g @aws/agentcore"
    )

    # Detect a brand-new workspace BEFORE resolve_harness mutates the index,
    # so we can hint "fresh" vs "resumed" correctly to the shim.
    resolved = harness_mod.resolve_harness(
        cfg, ws, harness_flag, sync_options["storage"], provisioning=True
    )
    sid = resolved.sid
    harness = resolved.harness
    storage_backend = resolved.storage
    session_epoch = resolved.epoch
    runtime_workspace = resolved.identity or ws
    sync_root = sync_mod.resolve_binding(cfg, runtime_workspace, sync_options)
    if sync_root:
        print("sch: sync binding selected: {}".format(sync_root), file=sys.stderr)
    hint = "fresh" if resolved.was_created else "resumed"
    arn = runtime_arn(cfg)
    shell_id = shell_id or presence_mod.new_shell_id()
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
        cfg, sid,
        runtime.payload_noop(
            runtime_workspace, harness, hint, storage_backend, session_epoch
        ),
        "shell-warmup",
    )
    storage_error = runtime.storage_verification_error(warmup, storage_backend)
    if storage_error:
        die(storage_error)
    runtime.invoke_best_effort(
        cfg, sid, runtime.payload_mark_interactive(
            runtime_workspace, harness, True, storage_backend, session_epoch
        )
    )

    print(
        "sch: opening shell in workspace '{}' (session {}, harness {})".format(
            ws, sid, harness
        ),
        file=sys.stderr,
    )
    print(
        "sch: detach with Ctrl+], reconnect with: sch shell {} --shell-id {}".format(
            ws, shell_id
        ),
        file=sys.stderr,
    )
    exec_args = [
        "agentcore",
        "exec",
        "--it",
        "--runtime",
        arn,
        "--session-id",
        sid,
        "--region",
        cfg.region,
    ]
    exec_args += ["--shell-id", shell_id]
    exec_args += passthrough
    workspace.mark_status(cfg, ws, "shell-opened")
    if not sync_root:
        procs.exec_or_wait(exec_args, presence=presence)
    try:
        return procs.supervise_interactive(
            sync_mod.helper_argv(
                cfg, arn, sid, runtime_workspace, sync_root,
                sync_mod.baseline_path(cfg, runtime_workspace),
                sync_options["bootstrap"], sync_options["conflict"], "watch",
                storage_backend,
                session_epoch,
            ),
            exec_args,
            presence=presence,
        )
    except procs.SyncStartError as exc:
        die("sync preflight failed: {}".format(exc))


def cmd_open(cfg, args):
    """`sch open` is an alias for `sch shell`."""
    return cmd_shell(cfg, args)
