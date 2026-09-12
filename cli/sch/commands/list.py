"""`sch list`: known workspaces + status.

Column widths (``%-20s %-12s %-55s %s``) are load-bearing: scripts and
humans alike rely on them matching the bash reference byte-for-byte.

``--remote-check`` is an opt-in S3 comparison (writer claims under
``workspace-writers/`` and checkpoint prefixes under ``checkpoints/``)
against the local/registry records. The default path stays offline-first
and never touches S3; remote failures degrade to a stderr warning.
"""

import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass

from .. import sync as sync_mod, workspace, workspace_registry

_ROW_FORMAT = "{:<20} {:<12} {:<9} {:<55} {}"


@dataclass(frozen=True)
class WorkspaceRecord:
    name: str
    harness: str
    storage: str
    sid: str
    status: str
    runtime_workspace: str
    identity: str = ""
    epoch: int = 0
    # Git-native session mode (add-git-native-workflow, task 5.1): the work
    # branch name when the workspace was seeded with --branch, else "".
    git_branch: str = ""


def read_workspace_records(cfg):
    """Return immutable records from the same index source as ``sch list``.

    Unlike the command renderer, this function never updates the local cache.
    """
    if workspace_registry.enabled(cfg):
        records = []
        for record in workspace_registry.list_workspaces(cfg):
            cached = workspace.read_workspace_state(cfg, record.name)
            status = "created"
            git_branch = ""
            if cached is not None and cached.identity == record.identity:
                status = workspace.read_status(cfg, record.name, default="created")
                if cached.git_native:
                    git_branch = cached.git_native.get("branch", "")
            records.append(WorkspaceRecord(
                record.name, record.harness, record.storage, record.sid, status,
                record.identity, record.identity, record.epoch, git_branch,
            ))
        return tuple(records)

    records = []
    for name in workspace.list_workspace_names(cfg):
        state = workspace.read_workspace_state(cfg, name)
        if state is None:
            continue
        git_branch = state.git_native.get("branch", "") if state.git_native else ""
        records.append(WorkspaceRecord(
            name, state.harness or "?", workspace.validate_storage_state(state, name),
            state.sid, workspace.read_status(cfg, name, default="created"),
            name, state.identity, state.epoch, git_branch,
        ))
    return tuple(records)


def _status_cell(cfg, record):
    """LAST ACTION cell: the local status, annotated with the git-native
    branch for seeded workspaces (task 5.1), or the mirror-sync local root
    for workspaces with a saved --sync binding (TASK-36). Appended to the
    final column so the load-bearing widths of the preceding columns stay
    untouched.

    The two modes are mutually exclusive by design (gitnative.py checks this
    at run time), so at most one annotation is ever appended.
    """
    if record.git_branch:
        return "{} [git-native: {}]".format(record.status, record.git_branch)
    sync_root = sync_mod.read_binding_root(cfg, record.runtime_workspace)
    if sync_root is not None:
        return "{} [sync: {}]".format(record.status, sync_root)
    return record.status


def cmd_list(cfg, args):
    remote_check = False
    for arg in args or []:
        if arg == "--remote-check":
            remote_check = True
        else:
            from ..config import die
            die("unknown option '{}'".format(arg))

    print(_ROW_FORMAT.format(
        "WORKSPACE", "HARNESS", "STORAGE", "SESSION ID", "LAST ACTION (local)"
    ))

    if workspace_registry.enabled(cfg):
        try:
            records = read_workspace_records(cfg)
        except (RuntimeError, ValueError) as exc:
            from ..config import die
            die(str(exc))
        if not records:
            print("(no workspaces yet — create one with: sch shell <name>)")
            if remote_check:
                _run_remote_check(cfg, records)
            return 0
        for record in records:
            workspace.save_workspace_state(
                cfg, record.name, record.sid, record.harness,
                record.identity, record.storage, record.epoch,
            )
            print(_ROW_FORMAT.format(
                record.name, record.harness, record.storage, record.sid,
                _status_cell(cfg, record)
            ))
        print()
        print("note: AgentCore Preview has no ListRuntimeSessions API; the status")
        print("shown is the last LOCAL action. Idle sessions stop automatically")
        print("after the configured idle timeout (default 900s).")
        if remote_check:
            _run_remote_check(cfg, records)
        return 0

    records = read_workspace_records(cfg)
    if not records:
        print("(no workspaces yet — create one with: sch shell <name>)")
        if remote_check:
            _run_remote_check(cfg, records)
        return 0

    for record in records:
        print(_ROW_FORMAT.format(
            record.name, record.harness, record.storage, record.sid,
            _status_cell(cfg, record)
        ))

    print()
    print("note: AgentCore Preview has no ListRuntimeSessions API; the status")
    print("shown is the last LOCAL action. Idle sessions stop automatically")
    print("after the configured idle timeout (default 900s).")
    if remote_check:
        _run_remote_check(cfg, records)
    return 0


class RemoteCheckError(RuntimeError):
    pass


def _s3(cfg, args):
    try:
        return subprocess.run(["aws"] + args + ["--region", cfg.region],
                              capture_output=True, text=True)
    except OSError as exc:
        raise RemoteCheckError("cannot execute AWS CLI") from exc


def _list_writer_identities(cfg, bucket):
    """Identities with an S3 writer claim (``workspace-writers/<id>.json``)."""
    identities = set()
    token = None
    while True:
        args = ["s3api", "list-objects-v2", "--bucket", bucket,
                "--prefix", "workspace-writers/", "--output", "json"]
        if token:
            args += ["--continuation-token", token]
        result = _s3(cfg, args)
        if result.returncode:
            raise RemoteCheckError("cannot list S3 writer claims")
        try:
            data = json.loads(result.stdout or "{}")
        except (TypeError, ValueError) as exc:
            raise RemoteCheckError("invalid S3 writer listing") from exc
        contents = data.get("Contents", [])
        if not isinstance(contents, list):
            raise RemoteCheckError("invalid S3 writer listing")
        for item in contents:
            key = item.get("Key", "") if isinstance(item, dict) else ""
            if key.startswith("workspace-writers/") and key.endswith(".json"):
                identity = key[len("workspace-writers/"):-len(".json")]
                if identity and "/" not in identity:
                    identities.add(identity)
        if not data.get("IsTruncated"):
            return identities
        token = data.get("NextContinuationToken")
        if not token:
            return identities


def _list_checkpoint_identities(cfg, bucket):
    """Identities with an S3 checkpoint prefix (``checkpoints/<id>/``)."""
    identities = set()
    token = None
    while True:
        args = ["s3api", "list-objects-v2", "--bucket", bucket,
                "--prefix", "checkpoints/", "--delimiter", "/",
                "--output", "json"]
        if token:
            args += ["--continuation-token", token]
        result = _s3(cfg, args)
        if result.returncode:
            raise RemoteCheckError("cannot list S3 checkpoints")
        try:
            data = json.loads(result.stdout or "{}")
        except (TypeError, ValueError) as exc:
            raise RemoteCheckError("invalid S3 checkpoint listing") from exc
        prefixes = data.get("CommonPrefixes", [])
        if not isinstance(prefixes, list):
            raise RemoteCheckError("invalid S3 checkpoint listing")
        for entry in prefixes:
            prefix = entry.get("Prefix", "") if isinstance(entry, dict) else ""
            if prefix.startswith("checkpoints/") and prefix.endswith("/"):
                identity = prefix[len("checkpoints/"):-1]
                if identity and "/" not in identity:
                    identities.add(identity)
        if not data.get("IsTruncated"):
            return identities
        token = data.get("NextContinuationToken")
        if not token:
            return identities


def _read_writer_claim(cfg, bucket, identity):
    """The parsed S3 writer claim for ``identity``, or ``None`` when absent."""
    key = "workspace-writers/{}.json".format(identity)
    with tempfile.NamedTemporaryFile(prefix="sch-list-", suffix=".json",
                                     delete=False) as handle:
        tmp_path = handle.name
    try:
        result = _s3(cfg, ["s3api", "get-object", "--bucket", bucket,
                           "--key", key, tmp_path])
        if result.returncode:
            return None
        try:
            with open(tmp_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            raise RemoteCheckError(
                "invalid S3 writer claim for '{}'".format(identity))
        return data if isinstance(data, dict) else {}
    finally:
        try:
            import os
            os.unlink(tmp_path)
        except OSError:
            pass


def _record_identity(record):
    return record.identity or record.runtime_workspace or record.name


def remote_orphan_report(cfg, records):
    """Compare local/registry records against S3 and return report lines.

    Raises :class:`RemoteCheckError` (or ``SystemExit`` from bucket
    resolution) when S3 cannot be read; the caller degrades to a warning.
    Never provisions and never touches a ListRuntimeSessions API.
    """
    from ..config import checkpoint_bucket
    bucket = checkpoint_bucket(cfg)
    writers = _list_writer_identities(cfg, bucket)
    checkpoints = _list_checkpoint_identities(cfg, bucket)
    known = {}
    for record in records:
        known.setdefault(_record_identity(record), record)
    lines = []
    for identity in sorted(writers - set(known)):
        lines.append(
            "remote orphan: S3 writer claim for '{}' has no local workspace "
            "record (deleted without 'sch delete'?); to purge S3 state run "
            "'sch delete {} --yes' after recreating it, "
            "or 'sch delete <name> --yes' for the recreated name".format(
                identity, identity))
    for identity in sorted(checkpoints - set(known) - writers):
        lines.append(
            "remote orphan: S3 checkpoints for '{}' have no local workspace "
            "record and no writer claim; to purge S3 state run "
            "'sch delete {} --yes' after recreating it".format(
                identity, identity))
    for identity in sorted(set(known) & writers):
        record = known[identity]
        claim = _read_writer_claim(cfg, bucket, identity)
        if not claim:
            continue
        remote_epoch = claim.get("session_epoch", claim.get("sessionEpoch"))
        remote_sid = claim.get("session_id", claim.get("sessionId", ""))
        try:
            remote_epoch = int(remote_epoch)
        except (TypeError, ValueError):
            remote_epoch = None
        if ((remote_epoch is not None and remote_epoch != record.epoch) or
                (remote_sid and remote_sid != record.sid)):
            lines.append(
                "collision risk: workspace '{}' has an S3 writer claim from "
                "another session (local epoch {}, S3 epoch {}); "
                "to adopt with a new epoch run 'sch reset-session {}', "
                "or to purge S3 state run 'sch delete {} --yes'".format(
                    record.name, record.epoch,
                    remote_epoch if remote_epoch is not None else "?",
                    record.name, record.name))
    return lines


def _run_remote_check(cfg, records):
    try:
        lines = remote_orphan_report(cfg, records)
    except SystemExit as exc:
        print("sch: remote check unavailable ({}); "
              "showing local list only".format(exc), file=sys.stderr)
        return 0
    except (RemoteCheckError, RuntimeError, ValueError, OSError) as exc:
        print("sch: remote check unavailable ({}); "
              "showing local list only".format(exc), file=sys.stderr)
        return 0
    print()
    if not lines:
        print("remote check: no orphans or collisions found in S3.")
    else:
        print("remote check: found {} remote issue(s):".format(len(lines)))
        for line in lines:
            print(line)
    return 0
