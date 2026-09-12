"""`sch status`: show task status (offline-first; reads S3), with optional
``--json`` (raw passthrough) and ``--live`` (best-effort enrichment from the
running microVM, if any).

Liveness (add-task-liveness-safety design D1): a persisted ``running`` state
is only ever rewritten by a live shim, so a microVM killed by the platform
leaves ``running`` behind forever. This command therefore judges liveness
from ``heartbeat_utc`` (rewritten every ~30s while a task really runs) and
degrades the *rendering* — it never rewrites the persisted state, and
``--json`` stays a raw passthrough of the S3 object.
"""

import datetime
import json
import subprocess

from .. import harness as harness_mod
from .. import procs, runtime, sync as sync_mod, workspace, workspace_registry
from ..config import checkpoint_bucket, die

# 150s == 5 missed beats (design D1): `sch status` is an observation tool, a
# false "suspect" for a minute costs nothing while a false "alive" cost the
# operator 30+ minutes during the 2026-08-21 incident.
STALE_AFTER_S = 150

# Dedicated exit code for the running-stale case, distinct from success (0)
# and from the pre-existing error path (1), so scripts and `bin/sch-watch`
# branch on a single client-side definition of staleness.
EXIT_RUNNING_STALE = 3

_HEARTBEAT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def read_offline_status(cfg, runtime_workspace, require_success=False):
    """Read task status from S3 without contacting the AgentCore runtime."""
    bucket = checkpoint_bucket(cfg)
    key = "checkpoints/{}/task-status.json".format(runtime_workspace)
    raw = '{"state":"none"}'
    with procs.temp_json_file("status") as tmp_path:
        result = subprocess.run(
            [
                "aws", "s3api", "get-object", "--bucket", bucket, "--key", key,
                "--region", cfg.region, tmp_path,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            if require_success:
                raise RuntimeError("task status is unavailable")
            return raw
        try:
            with open(tmp_path, "r", encoding="utf-8") as fh:
                return fh.read()
        except OSError as exc:
            if require_success:
                raise RuntimeError("cannot read task status") from exc
    return raw


def heartbeat_age_seconds(value, now=None):
    """Age in seconds of a shim-written ``heartbeat_utc`` stamp.

    Returns ``None`` when the value is absent, not a string, or not
    parsable: the caller treats that as "suspect", never as an error.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        stamp = datetime.datetime.strptime(value.strip(), _HEARTBEAT_FORMAT)
    except (ValueError, TypeError):
        return None
    reference = now or datetime.datetime.now(datetime.timezone.utc)
    return (reference - stamp.replace(tzinfo=datetime.timezone.utc)).total_seconds()


def humanize_age(seconds):
    """Compact human rendering of an age in seconds (``45s``, ``31m 12s``,
    ``2h 05m``). Negative values (clock skew) render as ``0s``."""
    total = int(seconds) if seconds and seconds > 0 else 0
    if total < 60:
        return "{}s".format(total)
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return "{}m {}s".format(minutes, secs)
    hours, minutes = divmod(minutes, 60)
    return "{}h {:02d}m".format(hours, minutes)


def running_stale_note(data, now=None):
    """Parenthetical qualifier for a suspect ``running`` state, or ``""``.

    Empty for every terminal state and for a ``running`` state whose
    heartbeat is fresher than :data:`STALE_AFTER_S` — those renderings stay
    byte-for-byte identical to the pre-change output.
    """
    if not isinstance(data, dict) or data.get("state") != "running":
        return ""
    age = heartbeat_age_seconds(data.get("heartbeat_utc"), now)
    if age is None:
        return "STALE: no heartbeat"
    if age > STALE_AFTER_S:
        return "STALE: ultimo heartbeat {} fa".format(humanize_age(age))
    return ""


def _exit_code_for_raw(raw, now=None):
    """Exit code for a status JSON string, without rendering it.

    Used by the ``--json`` path, which must stay a pure passthrough: the
    staleness classification only reaches the caller through the exit code.
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        return 0
    return EXIT_RUNNING_STALE if running_stale_note(data, now) else 0


def cmd_status(cfg, args):
    if not args or not args[0]:
        die("usage: sch status <workspace> [--json] [--live]")
    ws = args[0]
    # No `provisioning=True` here, ever: `status` is an observation command,
    # and a session rotation would steal the writer claim from a session that
    # may well be alive (add-task-liveness-safety task 2.5).
    if workspace_registry.enabled(cfg):
        resolved = harness_mod.resolve_harness(cfg, ws, "")
        sid = resolved.sid
        runtime_workspace = resolved.identity
        storage_backend = resolved.storage
        session_epoch = resolved.epoch
    else:
        state = workspace.read_workspace_state(cfg, ws)
        if state is None:
            die("unknown workspace '{}' (see: sch list)".format(ws))
        resolved = harness_mod.resolve_harness(cfg, ws, "")
        sid = resolved.sid
        runtime_workspace = ws
        storage_backend = resolved.storage
        harness = resolved.harness
        session_epoch = resolved.epoch
    if workspace_registry.enabled(cfg):
        harness = resolved.harness

    json_mode = False
    live_mode = False
    for arg in args[1:]:
        if arg == "--json":
            json_mode = True
        elif arg == "--live":
            live_mode = True
        else:
            die("unknown option '{}'".format(arg))

    raw = read_offline_status(cfg, runtime_workspace)

    # Git-native mode visibility (add-git-native-workflow, task 5.1): local
    # metadata, merged into the offline status without waking the runtime.
    local_state = workspace.read_workspace_state(cfg, ws)
    if local_state is not None and local_state.git_native:
        raw = merge_live_status(raw, {
            "session_mode": "git-native",
            "branch": local_state.git_native.get("branch", ""),
        })

    # Mirror-sync binding visibility (TASK-36): local-only, never in S3.
    # read_binding_root never calls die(), so it is safe for observation.
    sync_root = sync_mod.read_binding_root(cfg, runtime_workspace)

    # Best-effort live enrichment: merge live task fields when --live is
    # passed. A failed/empty live probe leaves the offline `raw` untouched.
    if live_mode:
        live_result = runtime.invoke_verified(
            cfg, sid, runtime.payload_info(
                runtime_workspace, harness, storage_backend, session_epoch
            ), "status-live"
        )
        if live_result.ok:
            live = dict(live_result.get("task", {}) or {})
            storage = live_result.get("storage")
            workspace_info = live_result.get("workspace", {})
            if storage:
                live["storage"] = storage
            if isinstance(workspace_info, dict):
                root_info = workspace_info.get("workspace_root")
                if isinstance(root_info, dict) and root_info.get("path"):
                    live["workspace_root"] = root_info["path"]
            raw = merge_live_status(raw, live)

    # One clock reading shared by the rendering and the exit code, so the
    # two can never disagree across the threshold boundary.
    now = datetime.datetime.now(datetime.timezone.utc)

    if json_mode:
        # Pure passthrough of the S3 object (design D1): staleness is a
        # presentation concern and MUST NOT add synthetic fields.
        print(raw)
        return _exit_code_for_raw(raw, now)

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        print("sch: status: invalid JSON from S3")
        return 1

    stale_note = running_stale_note(data, now)
    render_status(data, stale_note=stale_note, sync_root=sync_root)
    return EXIT_RUNNING_STALE if stale_note else 0


def merge_live_status(raw, live_task):
    """Merge best-effort live ``info`` task fields into the offline status
    JSON string ``raw``; live fields win on conflict.

    Returns a new JSON string, or ``raw`` unchanged if ``live_task`` is
    empty/not a dict, or if ``raw`` does not decode to a JSON object.
    """
    if not isinstance(live_task, dict) or not live_task:
        return raw
    try:
        base = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw
    if not isinstance(base, dict):
        return raw
    base.update(live_task)  # live wins
    return json.dumps(base)


def format_status(d, stale_note=None, sync_root=None):
    """Render the ``key : value`` text form of a status dict, matching the
    bash reference's field order, labels, and 500-char error truncation.

    ``stale_note`` is the pre-computed :func:`running_stale_note` qualifier;
    when omitted it is computed against the current clock. An empty note
    leaves the output byte-for-byte identical to the pre-change rendering.

    ``sync_root`` is the local mirror-sync binding path (TASK-36), read from
    the local config and passed in by :func:`cmd_status`.  It is never part
    of the S3 payload, so ``--json`` mode stays a pure passthrough.
    """
    lines = []
    state = d.get("state", "none")
    if stale_note is None:
        stale_note = running_stale_note(d)
    if stale_note:
        lines.append("state        : {} ({})".format(state, stale_note))
    else:
        lines.append("state        : {}".format(state))
    if "exit_code" in d and d["exit_code"] is not None:
        lines.append("exit_code    : {}".format(d["exit_code"]))
    if d.get("harness"):
        lines.append("harness      : {}".format(d["harness"]))
    if d.get("model"):
        # Present only when the task was submitted with an explicit --model;
        # absence means "harness default" and renders no line (spec:
        # headless-task-execution, "Task model observability").
        lines.append("model        : {}".format(d["model"]))
    if d.get("storage"):
        lines.append("storage      : {}".format(d["storage"]))
    if d.get("session_mode"):
        # Present only for git-native workspaces (add-git-native-workflow,
        # task 5.1); mirror-synced workspaces render no mode line.
        lines.append("mode         : {}".format(d["session_mode"]))
    if d.get("branch"):
        lines.append("branch       : {}".format(d["branch"]))
    if sync_root is not None:
        # Mirror-sync local binding path (TASK-36): local-only, never in S3.
        lines.append("sync_root    : {}".format(sync_root))
    if d.get("workspace_root"):
        lines.append("workspace_root: {}".format(d["workspace_root"]))
    if d.get("task_id"):
        lines.append("task_id      : {}".format(d["task_id"]))
    if d.get("prompt"):
        lines.append("prompt       : {}".format(d["prompt"]))
    if d.get("started_utc"):
        lines.append("started      : {}".format(d["started_utc"]))
    if d.get("finished_utc"):
        lines.append("finished     : {}".format(d["finished_utc"]))
    if d.get("duration_s") is not None:
        lines.append("duration_s   : {}".format(d["duration_s"]))
    if d.get("heartbeat_utc"):
        lines.append("heartbeat    : {}".format(d["heartbeat_utc"]))
    if d.get("harness_session_id"):
        lines.append("session_id   : {}".format(d["harness_session_id"]))
    if d.get("continue_requested"):
        # Continuation provenance (TASK-27): rendered only when a task was
        # submitted with --continue; absence means the flag was not used.
        # continue_resolved is absent while the worker has not resolved yet
        # (submit-to-ready window) or on paths that never resolve.
        if d.get("continue_resolved") is True:
            lines.append("continuation : resumed prior session")
        elif d.get("continue_resolved") is False:
            lines.append(
                "continuation : requested but no session found (started fresh)"
            )
        else:
            lines.append("continuation : requested (resolving)")
    if d.get("error"):
        err = str(d["error"])
        suffix = "..." if len(err) > 500 else ""
        lines.append("error        : {}{}".format(err[:500], suffix))
    if d.get("output"):
        lines.append("output:")
        lines.append(str(d["output"]))
        if d.get("output_truncated"):
            lines.append("(output truncated; showing the final 12000 characters)")
    if d.get("checkpoint_status"):
        lines.append("checkpoint   : {}".format(d["checkpoint_status"]))
    if d.get("checkpoint_warning"):
        lines.append("checkpoint_warning: {}".format(d["checkpoint_warning"]))
    return "\n".join(lines) + "\n"


def render_status(d, stale_note=None, sync_root=None):
    print(format_status(d, stale_note=stale_note, sync_root=sync_root), end="")
