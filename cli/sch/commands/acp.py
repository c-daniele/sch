"""`sch acp`: expose the workspace as an ACP (Agent Client Protocol) agent
over stdio, for editors (Zed, JetBrains) to spawn directly.

Unlike ``shell``/``run``, this does NOT exec the Node bridge process: it
runs it as a normal foreground child so cleanup (clearing the
mark-interactive advisory) still runs after it exits, whatever the reason.

Editor-spawn robustness: node and the AWS CLI are resolved explicitly
(``deps.resolve_node_bin`` / ``deps.resolve_aws_bin``) BEFORE any state
mutation; failures are explicit on stderr with empty stdout.
"""

import sys
from pathlib import Path

from .. import deps, harness as harness_mod
from .. import procs, repo, runtime, workspace
from ..config import die, runtime_arn

# add-pi-harness (design D11): harnesses for which an ACP agent exists in the
# microVM — `opencode` natively (`opencode acp`) and `claude` through the pinned
# official adapter (`claude-agent-acp`). `pi` has neither, so `sch acp` refuses
# it up front. Expressed as the RULE "harness with an ACP agent" so a future
# harness is one list entry, not a new code path.
_HAS_ACP_AGENT = ("opencode", "claude")


def _parse_args(args):
    usage_msg = (
        "usage: sch acp <workspace> [--harness <opencode|claude|pi>] "
        "[--storage <s3|session>] [--mirror <dir>]"
    )
    if not args or not args[0]:
        die(usage_msg)
    ws = args[0]
    workspace.validate_workspace_name(ws)
    rest = args[1:]

    harness_flag = ""
    storage_flag = ""
    mirror_flag = ""
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg == "--harness":
            if i + 1 >= len(rest):
                die("usage: --harness <opencode|claude|pi>")
            harness_flag = rest[i + 1]
            i += 2
            continue
        if arg == "--mirror":
            if i + 1 >= len(rest):
                die("usage: --mirror <dir>")
            mirror_flag = rest[i + 1]
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
        if arg.startswith("-"):
            die("unknown option '{}'".format(arg))
        die("unexpected positional argument '{}'".format(arg))

    return ws, harness_flag, storage_flag, mirror_flag


def _reject_without_acp_agent(ws, harness, explicit):
    """Refuse a harness that has no ACP agent, with actionable alternatives and
    WITHOUT any runtime call (spec: remote-ui-tunnel, "ACP unavailable for
    harnesses without an ACP agent")."""
    prefix = (
        "'sch acp' is not available for harness='{}'".format(harness)
        if explicit
        else "workspace '{}' is bound to harness='{}'".format(ws, harness)
    )
    die(
        "{}: no ACP agent exists for {} in the microVM — try 'sch shell {}', "
        "'sch run {}', or 'sch task {} \"<prompt>\"'".format(
            prefix, harness, ws, ws, ws
        )
    )


def cmd_acp(cfg, args):
    ws, harness_flag, storage_flag, mirror_flag = _parse_args(args)

    # Reject BEFORE dependency resolution, mirror creation or any state
    # mutation: an editor spawning this must get an immediate, explicit stderr
    # error with empty stdout, and no microVM must be woken up.
    if harness_flag and harness_flag not in _HAS_ACP_AGENT:
        _reject_without_acp_agent(ws, harness_flag, explicit=True)

    # Resolve local dependencies explicitly, fail fast with a clear stderr
    # message + non-zero exit + EMPTY stdout, before any state mutation.
    # Prepend their directories to PATH so every downstream aws/node call
    # in this process works unchanged.
    node_bin = deps.resolve_node_bin(cfg)
    aws_bin = deps.resolve_aws_bin(cfg)
    deps.prepend_path(str(Path(node_bin).parent), str(Path(aws_bin).parent))

    tunnel_dir = repo.tunnel_dir()
    if tunnel_dir is None:
        die(repo.missing_message("acp"))
    if not (tunnel_dir / "acp.js").is_file():
        die("cannot find {}/acp.js (repo layout unexpected)".format(tunnel_dir))
    if not (tunnel_dir / "node_modules").is_dir():
        die(
            "tunnel/ dependencies not installed — run: (cd '{}' && npm install)".format(
                tunnel_dir
            )
        )

    # Local mirror of the remote worktree: the directory the operator opens
    # as a project in the editor. Precedence: --mirror > SCH_ACP_MIRROR_ROOT
    # /<ws>/repo > default root.
    mirror_dir = Path(mirror_flag) if mirror_flag else cfg.acp_mirror_root / ws / "repo"
    try:
        mirror_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        die("cannot create mirror directory '{}'".format(mirror_dir))

    # opencode and claude admitted — resolve_harness applies the same
    # persisted-harness / mutual-exclusivity rules as every other command.
    resolved = harness_mod.resolve_harness(
        cfg, ws, harness_flag, storage_flag, provisioning=True
    )
    sid = resolved.sid
    harness = resolved.harness
    storage_backend = resolved.storage
    session_epoch = resolved.epoch
    runtime_workspace = resolved.identity or ws
    # A workspace already bound to a harness without an ACP agent (the flag was
    # omitted, so the binding came from the index): refuse here, still before the
    # first runtime invocation below.
    if harness not in _HAS_ACP_AGENT:
        _reject_without_acp_agent(ws, harness, explicit=False)
    arn = runtime_arn(cfg)

    # NOTE: intentionally mirrors the bash reference's exact (and slightly
    # quirky) ordering: the fresh/resumed hint is computed AFTER
    # resolve_harness has already created the index file for a brand-new
    # workspace, so `hint` is always "resumed" here — unlike shell/run,
    # which check file existence BEFORE resolving the harness. Preserved
    # for bit-for-bit behavioral parity (design D5).
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
        ), "acp-warmup"
    )
    storage_error = runtime.storage_verification_error(warmup, storage_backend)
    if storage_error:
        die(storage_error)
    # Advisory: an ACP client counts as an interactive writer for the
    # existing dual-writer warning logic.
    runtime.invoke_best_effort(
        cfg, sid, runtime.payload_mark_interactive(
            runtime_workspace, harness, True, storage_backend, session_epoch
        )
    )
    workspace.mark_status(cfg, ws, "acp-opened")

    rc = procs.run_foreground(
        [
            node_bin,
            str(tunnel_dir / "acp.js"),
            "--region",
            cfg.region,
            "--runtime-arn",
            arn,
            "--session-id",
            sid,
            "--workspace",
            runtime_workspace,
            "--harness",
            harness,
            "--storage",
            storage_backend,
            "--session-epoch",
            str(session_epoch),
            "--mirror",
            str(mirror_dir),
        ]
    )

    runtime.invoke_best_effort(
        cfg, sid, runtime.payload_mark_interactive(
            runtime_workspace, harness, False, storage_backend, session_epoch
        )
    )
    workspace.mark_status(cfg, ws, "acp-closed")
    return rc
