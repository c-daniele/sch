"""`sch reset-session`: regenerate the local sessionId (confirms)."""

import sys

from .. import workspace, workspace_registry
from ..config import die

_CONFIRM_VALUES = ("y", "Y", "yes", "YES")


def cmd_reset_session(cfg, args):
    if not args or not args[0]:
        die("usage: sch reset-session <workspace>")
    ws = args[0]

    state = workspace.read_workspace_state(cfg, ws)
    if workspace_registry.enabled(cfg):
        try:
            current = workspace_registry.resolve(cfg, ws)
        except (RuntimeError, ValueError) as exc:
            die(str(exc))
        old_sid = current.sid
        harness = current.harness
        storage_backend = current.storage
        epoch = current.epoch
    else:
        if state is None:
            die("unknown workspace '{}' (see: sch list)".format(ws))
        storage_backend = workspace.validate_storage_state(state, ws)
        epoch = workspace.validate_epoch_state(state, ws)
        old_sid = state.sid
        harness = state.harness or "opencode"

    print("sch: workspace '{}' is currently mapped to session:".format(ws), file=sys.stderr)
    print("sch:   {}".format(old_sid), file=sys.stderr)
    print("sch: this regenerates a NEW local sessionId for '{}'. The old".format(ws), file=sys.stderr)
    if storage_backend == "session":
        print("sch: the old session's managed storage will stop being addressed", file=sys.stderr)
        print("sch: (it is not deleted). The new session starts with empty L1", file=sys.stderr)
    else:
        print("sch: the old microVM local workdir will stop being addressed", file=sys.stderr)
        print("sch: (it is ephemeral). The new microVM starts with an empty workdir", file=sys.stderr)
    print("sch: and restores the latest published S3 checkpoint automatically.", file=sys.stderr)
    print("sch: intended for testing restore or recovering an unusable session.", file=sys.stderr)

    print("sch: proceed? [y/N] ", end="", file=sys.stderr)
    sys.stderr.flush()
    try:
        confirm = input()
    except EOFError:
        confirm = ""

    if confirm not in _CONFIRM_VALUES:
        print("sch: aborted", file=sys.stderr)
        raise SystemExit(1)

    if workspace_registry.enabled(cfg):
        try:
            remote = workspace_registry.rotate(cfg, ws)
        except (RuntimeError, ValueError) as exc:
            die(str(exc))
        new_sid = remote.sid
        harness = remote.harness
        storage_backend = remote.storage
        epoch = remote.epoch
        identity = remote.identity
    else:
        new_sid = workspace.generate_session_id(ws)
        identity = state.identity
        epoch += 1
    # Preserve the persisted harness across reset (the harness is workspace
    # identity, not session identity — switching it requires a new
    # workspace name).
    #
    # The recorded runtime version is dropped (runtime_version=""): it
    # described the session just abandoned, and the new session id has not
    # been provisioned yet — its version is recorded by the first
    # provisioning command that boots it (add-task-liveness-safety design
    # D3), which therefore adopts-current instead of rotating again.
    workspace.save_workspace_state(
        cfg, ws, new_sid, harness, identity, storage_backend, epoch,
        runtime_version="",
    )
    workspace.mark_status(cfg, ws, "session-reset")
    print(
        "sch: workspace '{}' now mapped to session {} "
        "(harness {}, storage {} preserved)".format(
            ws, new_sid, harness, storage_backend
        ),
        file=sys.stderr,
    )
    return 0
