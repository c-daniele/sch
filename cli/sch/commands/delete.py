"""`sch delete`: confirmed, retryable workspace destruction."""

import subprocess
import sys
import os

from .. import cli as cli_mod
from .. import deletion, workspace, workspace_registry
from ..config import die, runtime_arn


def _parse(args):
    yes = False
    all_flag = False
    name = None
    for arg in args:
        if arg == "--yes":
            yes = True
        elif arg == "--all" and name is None:
            all_flag = True
        elif arg.startswith("-"):
            die("usage: sch delete <workspace> [--yes] | sch delete --all [--yes]")
        elif name is None and not all_flag:
            name = arg
        else:
            die("usage: sch delete <workspace> [--yes] | sch delete --all [--yes]")
    if (name is None) == (not all_flag):
        die("usage: sch delete <workspace> [--yes] | sch delete --all [--yes]")
    if name:
        workspace.validate_workspace_name(name)
    return name, all_flag, yes


def _confirm(name, targets, yes):
    if yes:
        return True
    if name:
        print("sch: permanently delete workspace '{}' and its SCH checkpoints, history, writer claim and local metadata?".format(name), file=sys.stderr)
        expected = name
    else:
        print("sch: permanently delete these workspaces: {}".format(", ".join(targets)), file=sys.stderr)
        expected = "DELETE ALL WORKSPACES"
    if cli_mod.confirm_phrase(expected, yes=False):
        return True
    print("sch: deletion cancelled", file=sys.stderr)
    return False


def _stop(cfg, sid):
    if not sid:
        raise deletion.DeletionError("quiesce", "workspace has no runtime session ID")
    try:
        result = subprocess.run(["aws", "bedrock-agentcore", "stop-runtime-session",
                                 "--agent-runtime-arn", runtime_arn(cfg),
                                 "--runtime-session-id", sid, "--region", cfg.region,
                                 "--output", "json"], capture_output=True, text=True)
    except OSError as exc:
        raise deletion.DeletionError("quiesce", "cannot invoke runtime stop") from exc
    if result.returncode and not any(word in (result.stderr or "").lower()
                                     for word in ("not found", "does not exist", "already stopped")):
        raise deletion.DeletionError("quiesce", "runtime session could not be stopped")


def _local_target(cfg, ws):
    marker = deletion.read_deletion_marker(cfg, ws)
    if marker:
        return marker
    state = workspace.read_workspace_state(cfg, ws)
    if state is None:
        raise deletion.DeletionError("local cleanup", "unknown workspace")
    return {"workspace": ws, "sessionId": state.sid,
            "identity": state.identity or ws, "storage": workspace.validate_storage_state(state, ws),
            "sessionEpoch": workspace.validate_epoch_state(state, ws)}


def _delete_local(cfg, ws):
    target = _local_target(cfg, ws)
    deletion.write_deletion_marker(cfg, ws, target)
    _stop(cfg, target.get("sessionId", ""))
    deletion.purge_workspace(cfg, target["identity"])
    deletion.cleanup_local(cfg, ws)
    if target.get("storage") == "session":
        print("sch: deleted '{}' (SCH storage purged; managed session storage may remain until AgentCore retention expires)".format(ws), file=sys.stderr)
    else:
        print("sch: deleted '{}'".format(ws), file=sys.stderr)


def _delete_registry(cfg, ws):
    try:
        result = workspace_registry.delete(cfg, ws)
    except (RuntimeError, ValueError) as exc:
        raise deletion.DeletionError("registry finalize", str(exc)) from exc
    # The registry is authoritative and has completed remote deletion before
    # returning. Local cache cleanup is deliberately last.
    deletion.cleanup_local(cfg, ws)
    if result.get("storage") == "session":
        print("sch: deleted '{}' (SCH storage purged; managed session storage may remain until AgentCore retention expires)".format(ws), file=sys.stderr)
    else:
        print("sch: deleted '{}'".format(ws), file=sys.stderr)


def _targets(cfg):
    if workspace_registry.enabled(cfg):
        try:
            return sorted((item.name for item in workspace_registry.list_workspaces(cfg)), key=str)
        except (RuntimeError, ValueError) as exc:
            die(str(exc))
    names = set(workspace.list_workspace_names(cfg))
    if cfg.ws_dir.is_dir():
        names.update(p.name[len(".deleting."):] for p in cfg.ws_dir.iterdir()
                     if p.name.startswith(".deleting.") and len(p.name) > 10)
    return sorted(names)


def cmd_delete(cfg, args):
    name, bulk, yes = _parse(args)
    targets = [name] if name else _targets(cfg)
    if not targets:
        print("sch: no workspaces to delete", file=sys.stderr)
        return 0
    if not _confirm(name, targets, yes):
        return 1
    if bulk and workspace_registry.enabled(cfg):
        try:
            result = workspace_registry.delete_all(cfg)
        except (RuntimeError, ValueError) as exc:
            print("sch: registry finalize failed: {}".format(exc), file=sys.stderr)
            return 1
        entries = result.get("results", [])
        failures = [entry.get("workspace", "unknown") for entry in entries
                    if entry.get("status") != "deleted"]
        for entry in sorted(entries, key=lambda item: item.get("workspace", "")):
            target = entry.get("workspace", "unknown")
            status = entry.get("status", "failed")
            if status == "deleted":
                try:
                    deletion.cleanup_local(cfg, target)
                except deletion.DeletionError as exc:
                    failures.append(target)
                    status = "failed ({})".format(exc.phase)
            print("sch: {}: {}".format(target, status), file=sys.stderr)
        print("sch: summary: {} deleted, {} failed".format(result.get("deleted", 0), result.get("failed", len(failures))), file=sys.stderr)
        return 1 if failures else 0
    failures = []
    for target in sorted(targets):
        try:
            if workspace_registry.enabled(cfg):
                _delete_registry(cfg, target)
            else:
                _delete_local(cfg, target)
            if bulk:
                print("sch: {}: deleted".format(target), file=sys.stderr)
        except deletion.DeletionError as exc:
            failures.append(target)
            print("sch: {}: {} failed: {}".format(target, exc.phase, exc), file=sys.stderr)
    if bulk:
        print("sch: summary: {} deleted, {} failed".format(len(targets) - len(failures), len(failures)), file=sys.stderr)
        if failures:
            print("sch: retry failed targets: {}".format(", ".join(failures)), file=sys.stderr)
    return 1 if failures else 0
