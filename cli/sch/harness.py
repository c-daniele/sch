"""Harness resolution (sch-multi-harness): each workspace is bound to
exactly one coding-agent harness (``opencode`` | ``claude`` | ``pi``),
persisted in the workspace index alongside the runtime session id.

Rules (design D1/D8 of sch-multi-harness; spec: harness-selection):
  - file does not exist (new workspace):
      --harness <x>  -> persist {sid=generated, harness=<x>}
      no --harness   -> persist {sid=generated, harness=SCH_DEFAULT_HARNESS}
  - file exists with harness persisted:
      --harness <x> == persisted  -> accept (redundant-congruent)
      --harness <x> != persisted  -> REJECT (mutual exclusivity, no mutation)
      no --harness                -> reuse persisted
  - file exists but no harness (legacy):
      --harness claude    -> persist harness=claude (upgrade handoff)
      --harness opencode  -> persist harness=opencode
      --harness pi        -> persist harness=pi
      no --harness        -> persist harness=opencode (upgrade_reconcile)

add-pi-harness (design D1): `pi` is purely a third enum value — the resolution,
persistence and mutual-exclusivity rules above are unchanged, including the
legacy reconcile default, which stays `opencode`.
"""

import sys

from . import workspace, workspace_registry
from .config import deployed_runtime_version, die

VALID_HARNESSES = ("opencode", "claude", "pi")
VALID_STORAGE_BACKENDS = ("s3", "session")

# Commands that provision (or re-provision) a microVM, and are therefore the
# only ones allowed to look up the deployed runtime version and rotate the
# session on a mismatch (add-task-liveness-safety design D3): `run`, `task`,
# `shell`, `attach`, `web`, `acp`. Observation commands (`status`, `list`,
# `fetch`) MUST keep the default, because a rotation steals the writer claim
# and a live TUI on the old session would silently lose its checkpointing.


class ResolvedHarness:
    __slots__ = ("sid", "harness", "identity", "storage", "epoch", "was_created")

    def __init__(self, sid, harness, was_created, identity="", storage="session", epoch=0):
        self.sid = sid
        self.harness = harness
        self.was_created = was_created
        self.identity = identity or ""
        self.storage = storage
        self.epoch = epoch


def _validate_harness_value(value):
    if value not in VALID_HARNESSES:
        die(
            "invalid harness '{}' (expected: {})".format(
                value, "|".join(VALID_HARNESSES)
            )
        )
    return value


def _validate_storage_value(value):
    if value not in VALID_STORAGE_BACKENDS:
        die("invalid storage '{}' (expected: s3|session)".format(value))
    return value


def _announce_rotation(ws, resolved, new_sid, new_epoch, old_version, new_version):
    """Explain the rotation on stderr: what changed, what it costs, and what
    the operator gets back (spec: runtime-provisioning, "Runtime version",
    R15d)."""
    lines = [
        "sch: runtime version changed: workspace '{}' was pinned to version {}, "
        "the deployed version is now {}.".format(ws, old_version, new_version),
        "sch: the platform reaps the instances of a superseded version without any "
        "application-level trace, so the session has been rotated:",
        "sch:   {} -> {} (epoch {})".format(resolved.sid, new_sid, new_epoch),
        "sch: the new microVM starts empty and restores the latest published S3 "
        "checkpoint (L2); work never checkpointed on the old session is not "
        "recoverable.",
        "sch: harness={}, storage={} and the git-native binding are preserved.".format(
            resolved.harness, resolved.storage
        ),
    ]
    if resolved.storage == "session":
        lines.append(
            "sch: storage=session: the old session's managed storage stops being "
            "addressed (it is not deleted) — its L1 state is left behind."
        )
    for line in lines:
        print(line, file=sys.stderr)


def _rotate_for_runtime_version(cfg, ws, resolved, old_version, new_version):
    """Rotate the session exactly as `reset-session` does (new sid, epoch+1,
    harness/storage/gitNative preserved), recording the new runtime version.
    """
    if workspace_registry.enabled(cfg):
        # The registry is authoritative for the mapping: rotate there, then
        # mirror the result (plus the version) into the local index.
        try:
            remote = workspace_registry.rotate(cfg, ws)
        except (RuntimeError, ValueError) as exc:
            print(
                "sch: warning: runtime version changed ({} -> {}) but the registry "
                "rotation failed ({}); proceeding on the existing session".format(
                    old_version, new_version, exc
                ),
                file=sys.stderr,
            )
            return resolved
        new_sid = remote.sid
        harness = remote.harness
        identity = remote.identity
        storage = remote.storage
        epoch = remote.epoch
    else:
        new_sid = workspace.generate_session_id(ws)
        harness = resolved.harness
        identity = resolved.identity
        storage = resolved.storage
        epoch = resolved.epoch + 1

    workspace.save_workspace_state(
        cfg, ws, new_sid, harness, identity, storage, epoch,
        runtime_version=new_version,
    )
    workspace.mark_status(cfg, ws, "session-rotated")
    _announce_rotation(ws, resolved, new_sid, epoch, old_version, new_version)
    return ResolvedHarness(
        sid=new_sid, harness=harness, was_created=resolved.was_created,
        identity=identity, storage=storage, epoch=epoch,
    )


def _sync_runtime_version(cfg, ws, resolved, recorded_version, provisioning):
    """Reconcile the recorded runtime version with the deployed one.

    No-op unless ``provisioning`` is true: an observation must never be able
    to abandon a live session (design D3). Three provisioning outcomes:
    adopt-current when nothing was recorded, silence when they match, and a
    loud rotation on a mismatch. A failed lookup degrades to a warning —
    availability over strictness, like the checkpoint-failure path in
    `sch stop`.
    """
    if not provisioning:
        return resolved
    deployed = deployed_runtime_version(cfg)
    if not deployed:
        print(
            "sch: warning: cannot determine the deployed runtime version; "
            "proceeding on session {} without checking whether it was "
            "superseded".format(resolved.sid),
            file=sys.stderr,
        )
        return resolved
    if not recorded_version:
        # Upgrade path (design D3, adopt-current): a workspace that predates
        # this feature records the current version and does NOT rotate — the
        # comparison becomes effective from the next invocation.
        workspace.save_workspace_state(
            cfg, ws, resolved.sid, resolved.harness, resolved.identity,
            resolved.storage, resolved.epoch, runtime_version=deployed,
        )
        return resolved
    if recorded_version == deployed:
        return resolved
    return _rotate_for_runtime_version(cfg, ws, resolved, recorded_version, deployed)


def resolve_harness(cfg, ws, flag_value, storage_flag="", provisioning=False):
    """Resolve (and persist as needed) the harness for ``ws``.

    ``flag_value`` is the raw ``--harness`` argument, or ``""``/``None`` if
    the flag was not passed. Returns a :class:`ResolvedHarness`. Dies
    (mutating nothing) on mutual-exclusivity conflicts or invalid values.

    ``provisioning`` must be true only for the commands that (re-)provision a
    microVM: they — and only they — compare the recorded AgentCore runtime
    version with the deployed one and rotate the session on a mismatch
    (add-task-liveness-safety design D3).
    """
    flag_value = flag_value or ""
    storage_flag = storage_flag or ""
    if workspace_registry.enabled(cfg):
        if flag_value:
            _validate_harness_value(flag_value)
        if storage_flag:
            _validate_storage_value(storage_flag)
        # Read the recorded version BEFORE mirroring the registry record into
        # the local index (the mirror preserves it, but the value must be the
        # pre-save one).
        cached = workspace.read_workspace_state(cfg, ws)
        recorded_version = cached.runtime_version if cached is not None else ""
        remote = workspace_registry.resolve_or_die(
            cfg, ws, flag_value, storage_flag,
            getattr(cfg, "default_storage", "s3"),
        )
        if cached is not None and cached.sid and cached.sid != remote.sid:
            # The registry handed out a different session than the one this
            # client last saw: whatever version the local index recorded does
            # not describe it, so adopt the current one instead of rotating.
            recorded_version = ""
        workspace.save_workspace_state(
            cfg, ws, remote.sid, remote.harness, remote.identity, remote.storage, remote.epoch
        )
        resolved = ResolvedHarness(
            remote.sid, remote.harness, remote.was_created, remote.identity, remote.storage, remote.epoch
        )
        return _sync_runtime_version(
            cfg, ws, resolved, recorded_version, provisioning
        )

    if not workspace.workspace_exists(cfg, ws):
        chosen = _validate_harness_value(flag_value or cfg.default_harness)
        chosen_storage = _validate_storage_value(
            storage_flag or getattr(cfg, "default_storage", "s3")
        )
        sid = workspace.generate_session_id(ws)
        workspace.save_workspace_state(cfg, ws, sid, chosen, storage=chosen_storage, epoch=1)
        print(
            "sch: new workspace '{}' -> session {} (harness={}, storage={})".format(
                ws, sid, chosen, chosen_storage
            ),
            file=sys.stderr,
        )
        resolved = ResolvedHarness(
            sid=sid, harness=chosen, was_created=True, storage=chosen_storage, epoch=1
        )
        # Brand-new session: record the version it is born on (never rotates,
        # since nothing was recorded before).
        return _sync_runtime_version(cfg, ws, resolved, "", provisioning)

    state = workspace.read_workspace_state(cfg, ws)
    if state is None:
        die("cannot read workspace index for '{}'".format(ws))
    sid = state.sid
    persisted = state.harness
    persisted_storage = workspace.validate_storage_state(state, ws)
    persisted_epoch = workspace.validate_epoch_state(state, ws)
    if not sid:
        die("workspace '{}' index has no runtimeSessionId".format(ws))

    if storage_flag:
        _validate_storage_value(storage_flag)

    if not persisted or not state.storage_present:
        # Legacy: reconcile. --harness claude is the explicit upgrade handoff.
        if persisted:
            if flag_value and flag_value != persisted:
                die(
                    "workspace '{}' is bound to harness='{}' (cannot switch to '{}'; "
                    "use a new workspace name or the documented reset path)".format(
                        ws, persisted, flag_value
                    )
                )
            chosen = persisted
        elif flag_value:
            chosen = _validate_harness_value(flag_value)
        else:
            chosen = "opencode"  # upgrade_reconcile default for legacy workspaces
        chosen_storage = persisted_storage
        if storage_flag and storage_flag != chosen_storage:
            die(
                "workspace '{}' is bound to storage='{}' (cannot switch to '{}'; "
                "storage migration is not implemented)".format(
                    ws, chosen_storage, storage_flag
                )
            )
        workspace.save_workspace_state(
            cfg, ws, sid, chosen, state.identity, chosen_storage, persisted_epoch
        )
        print(
            "sch: reconciled legacy workspace '{}' -> harness={}, storage={} "
            "(sessionId unchanged)".format(
                ws, chosen, chosen_storage
            ),
            file=sys.stderr,
        )
        resolved = ResolvedHarness(
            sid=sid, harness=chosen, was_created=False,
            identity=state.identity, storage=chosen_storage, epoch=persisted_epoch,
        )
        return _sync_runtime_version(
            cfg, ws, resolved, state.runtime_version, provisioning
        )

    # Existing workspace with harness persisted.
    if flag_value and flag_value != persisted:
        die(
            "workspace '{}' is bound to harness='{}' (cannot switch to '{}'; "
            "use a new workspace name or the documented reset path)".format(
                ws, persisted, flag_value
            )
        )
    if storage_flag and storage_flag != persisted_storage:
        die(
            "workspace '{}' is bound to storage='{}' (cannot switch to '{}'; "
            "storage migration is not implemented)".format(
                ws, persisted_storage, storage_flag
            )
        )
    resolved = ResolvedHarness(
        sid=sid, harness=persisted, was_created=False,
        identity=state.identity, storage=persisted_storage, epoch=persisted_epoch,
    )
    return _sync_runtime_version(
        cfg, ws, resolved, state.runtime_version, provisioning
    )
