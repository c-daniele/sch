"""Push a local OpenCode session into a remote SCH workspace."""

import json
import os
import subprocess
import sys
import tempfile

from .. import bundlexfer, deps, harness as harness_mod, runtime, workspace
from ..config import die, runtime_arn

HANDOFF_FILE_NAME = "handoff.json"
_USAGE = "usage: sch handoff <workspace> [--harness <opencode|claude|pi>] [--session <id>] [--sanitize] [--storage <s3|session>]"
_FORBIDDEN = {"--branch", "--sync", "--no-sync", "--bootstrap", "--conflict"}


def _parse_args(args):
    if args and args[0] in ("-h", "--help"):
        print(_USAGE)
        raise SystemExit(0)
    if not args or not args[0]:
        die(_USAGE)
    ws = args[0]
    workspace.validate_workspace_name(ws)
    session_id = ""
    sanitize = False
    storage = ""
    harness_flag = ""
    i = 1
    while i < len(args):
        arg = args[i]
        if arg in _FORBIDDEN:
            die("{} is not accepted by handoff; session transfer is independent of repo sync/seed".format(arg))
        if arg in ("--session", "--storage", "--harness"):
            if i + 1 >= len(args) or args[i + 1].startswith("--"):
                die("{} requires a value".format(arg))
            value = args[i + 1]
            if arg == "--session":
                session_id = value
            elif arg == "--storage":
                storage = value
            else:
                harness_flag = value
            i += 2
        elif arg == "--sanitize":
            sanitize = True
            i += 1
        else:
            die("unknown option '{}'".format(arg))
    if storage and storage not in ("s3", "session"):
        die("invalid storage '{}' (expected: s3|session)".format(storage))
    if harness_flag and harness_flag not in harness_mod.VALID_HARNESSES:
        die(
            "invalid harness '{}' (expected: {})".format(
                harness_flag, "|".join(harness_mod.VALID_HARNESSES)
            )
        )
    return ws, session_id, sanitize, storage, harness_flag


def _run_opencode(argv, cwd=None, stdout_path=None):
    """Run a local opencode command. With ``stdout_path``, stdout is redirected
    straight to that file instead of a pipe: opencode (bun) exits without
    flushing pending stdout writes to a pipe, silently truncating payloads
    beyond the ~64KB pipe buffer — large `export` outputs were cut mid-JSON."""
    try:
        if stdout_path is None:
            return subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
        with open(stdout_path, "w", encoding="utf-8") as stream:
            return subprocess.run(argv, cwd=cwd, stdout=stream, stderr=subprocess.PIPE, text=True)
    except OSError as exc:
        die("cannot run local opencode: {}".format(exc))


def _latest_session(opencode_bin, cwd=None):
    cwd = os.path.realpath(cwd or os.getcwd())
    result = _run_opencode([opencode_bin, "session", "list", "--format", "json"], cwd=cwd)
    if result.returncode != 0:
        die("cannot list local OpenCode sessions: {}".format((result.stderr or result.stdout).strip()))
    try:
        records = json.loads(result.stdout)
        matches = [r for r in records if os.path.realpath(str(r.get("directory", ""))) == cwd]
        selected = max(matches, key=lambda r: int(r.get("updated", 0)))
        session_id = str(selected["id"])
    except (ValueError, TypeError, KeyError):
        die("no OpenCode sessions found for the current project; use --session <id>")
    print("sch: selected local session '{}' ({})".format(selected.get("title", "untitled"), session_id), file=sys.stderr)
    return session_id


def _version(opencode_bin):
    result = _run_opencode([opencode_bin, "--version"])
    return (result.stdout or result.stderr).strip() if result.returncode == 0 else "unknown"


def _reject_non_opencode_harness_flag(ws, harness_flag):
    """Die when ``--harness claude|pi`` is passed with a handoff transfer.

    Shared by ``sch handoff`` and ``sch task --handoff``: the transfer moves
    an OpenCode session, so only ``opencode`` (or omitting the flag) proceeds.
    Must run before any local or remote mutation (spec: session-handoff I1).
    """
    if harness_flag and harness_flag != "opencode":
        die(
            "'sch handoff' supports only harness='opencode' (it transfers an "
            "OpenCode session) — requested '{}'; use a workspace bound to "
            "opencode, or 'sch task {} --harness {} \"<prompt>\"' to work in "
            "that harness".format(harness_flag, ws, harness_flag)
        )


def export_local_session(opencode_bin, session_id, sanitize):
    """Resolve (when empty) and export the local OpenCode session.

    Returns ``(effective_session_id, export_path)`` where ``export_path`` is
    a temp file holding the export payload. The caller owns the file and
    MUST remove it. Dies on resolution failure, export failure, or an
    invalid/dangling payload — before any workspace or remote mutation when
    called before ``resolve_harness``.
    """
    if not session_id:
        session_id = _latest_session(opencode_bin)
    local_version = _version(opencode_bin)
    fd, export_path = tempfile.mkstemp(prefix="sch-handoff-", suffix=".json")
    os.close(fd)
    try:
        argv = [opencode_bin, "export", session_id]
        if sanitize:
            argv.append("--sanitize")
        exported = _run_opencode(argv, cwd=os.getcwd(), stdout_path=export_path)
        with open(export_path, "r", encoding="utf-8") as stream:
            payload = stream.read()
        if exported.returncode != 0 or not payload.strip():
            die("local OpenCode export failed: {}".format((exported.stderr or "unknown error").strip()))
        try:
            data = json.loads(payload)
            if str((data.get("info") or {}).get("id")) != session_id:
                die("local OpenCode export returned a different session id")
        except (ValueError, AttributeError):
            die("local OpenCode export returned invalid JSON")
    except BaseException:
        try:
            os.remove(export_path)
        except OSError:
            pass
        raise
    return session_id, export_path, local_version


def upload_and_import(cfg, ws, export_path, resolved, runtime_workspace, local_version="unknown"):
    """Upload the export bundle and request the ``session-import`` shim action.

    Assumes the microVM is already warmed up. Prints the reimport/repoEmpty
    warnings, returns the imported sessionID. Dies with the version-skew and
    unknown-action diagnostics of ``sch handoff``.
    """
    bundlexfer.run_bundle_helper(
        bundlexfer.bundle_helper_argv(
            cfg, runtime_arn(cfg), resolved.sid, runtime_workspace, resolved.storage,
            resolved.epoch, HANDOFF_FILE_NAME, upload=export_path,
        ),
        "session handoff upload",
    )
    result = runtime.invoke_verified(
        cfg, resolved.sid,
        runtime.payload_session_import(runtime_workspace, "opencode", resolved.storage, resolved.epoch),
        "session-import",
    )
    if not result.ok:
        die("session-import invocation failed")
    message = str(result.get("error", "") or result.get("message", ""))
    if "unknown action" in message:
        die("the runtime image does not support session handoff; rebuild/update the runtime image")
    if result.get("status") != "ok":
        die("remote session import failed: {} (local opencode {}, remote {})".format(
            message or "unknown error", local_version, result.get("opencodeVersion", "unknown")
        ))
    imported_id = result.get("sessionID", "")
    if not imported_id:
        die("remote session import succeeded without a sessionID")
    if result.get("reimported", False):
        print("sch: WARNING session already existed remotely; remote work may have been overwritten", file=sys.stderr)
    if result.get("repoEmpty", False):
        print("sch: WARNING remote worktree is empty/unseeded; use --branch or --sync on other commands to align files", file=sys.stderr)
    return imported_id


def _reject_non_opencode_binding(cfg, ws):
    """Die with the opencode-only constraint spelled out when ``ws`` is already
    bound to another harness. Best-effort by design: the enforcing rejection is
    resolve_harness's, so an unreadable index here just falls through to it."""
    try:
        if not workspace.workspace_exists(cfg, ws):
            return
        state = workspace.read_workspace_state(cfg, ws)
        persisted = getattr(state, "harness", "") if state is not None else ""
    except Exception:  # noqa: BLE001 — never turn a message nicety into a failure
        return
    if not persisted or persisted == "opencode":
        return
    die(
        "workspace '{}' is bound to harness='{}'; 'sch handoff' supports only "
        "harness='opencode' (it transfers an OpenCode session) — use a workspace "
        "bound to opencode, or 'sch task {} \"<prompt>\"' to work in this "
        "one".format(ws, persisted, ws)
    )


def cmd_handoff(cfg, args):
    ws, session_id, sanitize, storage_flag, harness_flag = _parse_args(args)
    # `sch handoff` transfers an OpenCode session, so it is opencode-only by
    # nature (spec: session-handoff R6/I5). The flag exists only so scripts
    # that uniformly pass --harness keep working and so first-shot creation
    # with an explicit congruent value behaves like the default: reject a
    # divergent value here, before any local or remote mutation (I1), mirroring
    # the `sch attach`/`sch web` opencode-only precedent.
    _reject_non_opencode_harness_flag(ws, harness_flag)
    opencode_bin = deps.which_opencode()
    if not opencode_bin:
        die("'opencode' is required locally for 'sch handoff'; install it and ensure it is on PATH")
    # Export before resolve: an export failure dies before the workspace is
    # created or the runtime is touched (I1).
    session_id, export_path, local_version = export_local_session(
        opencode_bin, session_id, sanitize
    )
    try:
        # `sch handoff` transfers an OpenCode session, so it is opencode-only by
        # nature. resolve_harness below is the ENFORCING gate (it refuses a
        # divergent persisted harness without mutating anything), but its message
        # is the generic mutual-exclusivity one; this read-only pre-check exists
        # only so the error names the actual constraint (spec: session-handoff,
        # "Binding harness implicito a opencode" — extended to pi by
        # add-pi-harness task 5.4). Purely additive: if the binding cannot be read
        # here, resolve_harness still refuses.
        _reject_non_opencode_binding(cfg, ws)
        resolved = harness_mod.resolve_harness(cfg, ws, harness_flag or "opencode", storage_flag)
        runtime_workspace = resolved.identity or ws
        warmup = runtime.invoke_verified(
            cfg, resolved.sid,
            runtime.payload_noop(runtime_workspace, "opencode", "fresh" if resolved.was_created else "resumed", resolved.storage, resolved.epoch),
            "handoff-warmup",
        )
        storage_error = runtime.storage_verification_error(warmup, resolved.storage)
        if storage_error:
            die(storage_error)
        imported_id = upload_and_import(
            cfg, ws, export_path, resolved, runtime_workspace, local_version
        )
        print(imported_id)
        return 0
    finally:
        try:
            os.remove(export_path)
        except OSError:
            pass
