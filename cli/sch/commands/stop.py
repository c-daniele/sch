"""`sch stop`: stop the runtime session (forced, non-blocking L2 checkpoint
first).
"""

import subprocess
import sys

from .. import harness as harness_mod
from .. import runtime, workspace, workspace_registry
from ..config import die, runtime_arn


def cmd_stop(cfg, args):
    if not args or not args[0]:
        die("usage: sch stop <workspace>")
    ws = args[0]
    if workspace_registry.enabled(cfg):
        resolved = harness_mod.resolve_harness(cfg, ws, "")
        sid, harness, runtime_workspace = resolved.sid, resolved.harness, resolved.identity
    else:
        state = workspace.read_workspace_state(cfg, ws)
        if state is None:
            die("unknown workspace '{}' (see: sch list)".format(ws))
        resolved = harness_mod.resolve_harness(cfg, ws, "")
        sid, harness = resolved.sid, resolved.harness
        storage_backend, runtime_workspace = resolved.storage, ws
    if workspace_registry.enabled(cfg):
        storage_backend = resolved.storage
    session_epoch = resolved.epoch
    arn = runtime_arn(cfg)

    # Synchronous, forced checkpoint before stopping. A checkpoint failure
    # MUST NOT block the stop — warn explicitly and proceed regardless,
    # both on invocation failure and on a non-"ok" status in the shim's own
    # JSON response.
    print(
        "sch: checkpointing harness state + workspace (L2, harness={})...".format(
            harness
        ),
        file=sys.stderr,
    )
    result = runtime.invoke_verified(
        cfg, sid, runtime.payload_checkpoint(
            runtime_workspace, harness, storage_backend, session_epoch
        ), "checkpoint"
    )
    if result.ok:
        status = result.get("status", "unknown")
        if status != "ok":
            print(
                "sch: WARNING checkpoint reported status='{}' (continuing with stop)".format(
                    status
                ),
                file=sys.stderr,
            )
            print("sch:   details: {}".format(result.raw_text), file=sys.stderr)
    else:
        print(
            "sch: WARNING checkpoint invocation failed (continuing with stop)",
            file=sys.stderr,
        )

    # Advisory: clear the interactive flag so a subsequent `sch task
    # --continue` is clean of the dual-writer warning.
    runtime.invoke_best_effort(
        cfg, sid, runtime.payload_mark_interactive(
            runtime_workspace, harness, False, storage_backend, session_epoch
        )
    )

    print(
        "sch: stopping session {} (workspace '{}', harness {})".format(
            sid, ws, harness
        ),
        file=sys.stderr,
    )
    # Not suppressed: this call's JSON response is the command's own
    # machine-consumable stdout output, matching the bash reference.
    subprocess.run(
        [
            "aws",
            "bedrock-agentcore",
            "stop-runtime-session",
            "--agent-runtime-arn",
            arn,
            "--runtime-session-id",
            sid,
            "--region",
            cfg.region,
            "--output",
            "json",
        ]
    )
    workspace.mark_status(cfg, ws, "stopped")
    print(
        "sch: microVM stopped; workspace durability follows storage='{}'".format(
            storage_backend
        ),
        file=sys.stderr,
    )
    return 0
