"""`sch attach`: local OpenCode TUI connected to a remote `opencode serve`.

Only the ``opencode`` harness is supported (no client/server split exists
for ``claude``). New workspaces therefore default to ``opencode``, regardless
of ``SCH_DEFAULT_HARNESS``.
"""

import subprocess
import sys
import time
from pathlib import Path

from .. import deps, harness as harness_mod
from .. import procs, repo, runtime, workspace
from ..config import die, runtime_arn

_SERVE_ENSURE_ATTEMPTS = 5
_SERVE_ENSURE_RETRY_DELAY_S = 2


# add-pi-harness (design D11): harnesses with no client/server split — the rule
# `sch attach` and `sch web` reject on, instead of naming claude specifically.
_NO_CLIENT_SERVER_SPLIT = ("claude", "pi")
# Harnesses that have an ACP agent in the microVM (native or official adapter).
# `pi` has none, so it must not be offered `sch acp` as an alternative.
_HAS_ACP_AGENT = ("opencode", "claude")


def _alternatives(harness, ws):
    """Actionable alternatives for a rejected harness, naming only commands
    that actually work for it (spec: remote-ui-tunnel)."""
    if harness in _HAS_ACP_AGENT:
        return "'sch acp {}' or 'sch shell {}'".format(ws, ws)
    return "'sch shell {}' or 'sch run {}'".format(ws, ws)


def _parse_args(args):
    usage_msg = (
        "usage: sch attach <workspace> [--harness opencode] "
        "[--storage <s3|session>] [--force]"
    )
    if not args or not args[0]:
        die(usage_msg)
    ws = args[0]
    workspace.validate_workspace_name(ws)
    rest = args[1:]

    force = False
    harness_flag = ""
    storage_flag = ""
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg == "--force":
            force = True
            i += 1
            continue
        if arg == "--harness":
            if i + 1 >= len(rest):
                die("usage: --harness <opencode|claude|pi>")
            harness_flag = rest[i + 1]
            i += 2
            continue
        if arg == "--storage":
            if i + 1 >= len(rest):
                die("usage: --storage <s3|session>")
            storage_flag = rest[i + 1]
            if storage_flag not in ("s3", "session"):
                die("--storage must be one of: s3, session")
            i += 2
            continue
        if arg == "--":
            break
        if arg == "--branch":
            die(
                "--branch initializes git-native mode only with 'sch run' or "
                "'sch task'; run 'sch run {} --harness opencode --branch <branch>', "
                "then 'sch attach {} --harness opencode'".format(ws, ws)
            )
        if arg.startswith("-"):
            die("unknown option '{}'".format(arg))
        die("unexpected positional argument '{}'".format(arg))

    return ws, harness_flag, storage_flag, force


def cmd_attach(cfg, args):
    ws, harness_flag, storage_flag, force = _parse_args(args)

    # Reject before dependency checks or state mutation: only a harness with a
    # client/server split can serve the local OpenCode attach client. Stated as
    # a RULE rather than per-harness (add-pi-harness design D11), so a fourth
    # harness does not reopen this code path.
    if harness_flag in _NO_CLIENT_SERVER_SPLIT:
        die(
            "'sch attach' requires harness='opencode' ({} has no client/server "
            "split) — try {}".format(harness_flag, _alternatives(harness_flag, "<workspace>"))
        )

    if not deps.which_node():
        die(
            "'node' is required for 'sch attach' (see tunnel/package.json); "
            "install Node >= 18"
        )
    if not deps.which_opencode():
        die(
            "'opencode' is required locally for 'sch attach' (the TUI runs "
            "on your laptop, connected to the remote server)"
        )
    tunnel_dir = repo.tunnel_dir()
    if tunnel_dir is None:
        die(repo.missing_message("attach"))
    if not (tunnel_dir / "attach.js").is_file():
        die("cannot find {}/attach.js (repo layout unexpected)".format(tunnel_dir))
    if not (tunnel_dir / "node_modules").is_dir():
        die(
            "tunnel/ dependencies not installed — run: (cd '{}' && npm install)".format(
                tunnel_dir
            )
        )

    # `attach` is intrinsically an OpenCode client. Its implicit harness for a
    # new workspace must not inherit SCH_DEFAULT_HARNESS (which is claude).
    resolved = harness_mod.resolve_harness(
        cfg, ws, harness_flag or "opencode", storage_flag, provisioning=True
    )
    sid = resolved.sid
    harness = resolved.harness
    storage_backend = resolved.storage
    session_epoch = resolved.epoch
    runtime_workspace = resolved.identity or ws
    if harness != "opencode":
        die(
            "workspace '{}' is bound to harness='{}'; 'sch attach' requires "
            "opencode (no client/server split exists for {}) — try {} "
            "instead".format(ws, harness, harness, _alternatives(harness, ws))
        )
    arn = runtime_arn(cfg)

    # NOTE: intentionally mirrors the bash reference's exact ordering (see
    # commands/acp.py): the hint is computed AFTER resolve_harness, so it
    # is always "resumed" here — preserved for behavioral parity.
    hint = "fresh" if resolved.was_created else "resumed"

    print(
        "sch: provisioning microVM (storage hint: {}, harness: {})...".format(
            hint, harness
        ),
        file=sys.stderr,
    )
    warmup = runtime.invoke_verified(
        cfg, sid, runtime.payload_noop(
            runtime_workspace, harness, hint, storage_backend, session_epoch
        ), "attach-warmup"
    )
    storage_error = runtime.storage_verification_error(warmup, storage_backend)
    if storage_error:
        die(storage_error)

    # serve-ensure: ensure opencode serve is running remotely, get its port
    # + installed version. Retry a few times — a cold/fresh workspace may
    # report "starting" the first time.
    remote_port = ""
    remote_opencode_version = ""
    serve_status = ""
    attempt = 0
    while attempt < _SERVE_ENSURE_ATTEMPTS:
        attempt += 1
        result = runtime.invoke_verified(
            cfg, sid, runtime.payload_serve_ensure(
                runtime_workspace, harness, storage_backend, session_epoch
            ), "serve-ensure"
        )
        if result.ok:
            serve_status = result.get("status", "unknown")
            if serve_status == "ok":
                remote_port = str(result.get("port", "") or "")
                remote_opencode_version = str(
                    result.get("opencode_version", "") or ""
                )
                break
        else:
            serve_status = "invoke-failed"
        if attempt < _SERVE_ENSURE_ATTEMPTS:
            time.sleep(_SERVE_ENSURE_RETRY_DELAY_S)

    if not (serve_status == "ok" and remote_port):
        die(
            "serve-ensure did not report status=ok with a port after {} "
            "attempts (last status: {})".format(attempt, serve_status)
        )

    # Version-parity check between the local TUI and the remote server.
    try:
        version_result = subprocess.run(
            ["opencode", "--version"], capture_output=True, text=True
        )
        local_version = version_result.stdout.strip() or "unknown"
    except (OSError, FileNotFoundError):
        local_version = "unknown"

    if not force and local_version != remote_opencode_version:
        die(
            "local opencode version '{}' differs from remote '{}' — "
            "re-run with --force to proceed anyway".format(
                local_version, remote_opencode_version
            )
        )
    if local_version != remote_opencode_version:
        print(
            "sch: WARNING opencode version mismatch (local={}, remote={}) "
            "— proceeding due to --force".format(local_version, remote_opencode_version),
            file=sys.stderr,
        )

    runtime.invoke_best_effort(
        cfg, sid, runtime.payload_mark_interactive(
            runtime_workspace, harness, True, storage_backend, session_epoch
        )
    )
    workspace.mark_status(cfg, ws, "attach-opened")

    rc = procs.run_foreground(
        [
            "node",
            str(tunnel_dir / "attach.js"),
            "--region",
            cfg.region,
            "--runtime-arn",
            arn,
            "--session-id",
            sid,
            "--workspace",
            runtime_workspace,
            "--storage",
            storage_backend,
            "--session-epoch",
            str(session_epoch),
            "--remote-port",
            remote_port,
        ]
    )

    runtime.invoke_best_effort(
        cfg, sid, runtime.payload_mark_interactive(
            runtime_workspace, harness, False, storage_backend, session_epoch
        )
    )
    workspace.mark_status(cfg, ws, "attach-closed")
    return rc
