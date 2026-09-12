"""Workspace index: `~/.config/sch/workspaces/<ws>`.

File format: JSON object with ``runtimeSessionId``, ``harness`` and ``storage``
(plus the optional ``gitNative`` binding and the ``runtimeVersion`` the session
id was generated on).
Legacy files (written before sch-multi-harness) may be:
  - a bare session-id string (not valid JSON), or
  - a JSON scalar (e.g. a quoted JSON string), or
  - a JSON object using the older ``sessionId`` key instead of
    ``runtimeSessionId``.
All of these are tolerated on read with no migration (spec:
cli-cross-platform, "Workspace index compatibility").
"""

import json
import re
import uuid
from datetime import datetime, timezone

from .config import die

WORKSPACE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")


class WorkspaceState:
    """Resolved (sid, harness) pair for a workspace index file.

    ``harness`` is ``""`` when the file is legacy (no harness field).
    ``git_native`` is the persisted git-native session mode
    (``{"branch": ..., "baseSha": ..., "localRepo": ...}``,
    add-git-native-workflow design D5) or ``None`` when the workspace has
    never been seeded.
    ``runtime_version`` is the AgentCore runtime version the session id was
    generated on (add-task-liveness-safety design D3), or ``""`` for an
    index written before that feature — which provisioning commands treat as
    "adopt the current version without rotating".
    """

    __slots__ = (
        "sid", "harness", "identity", "storage", "storage_present", "epoch",
        "git_native", "runtime_version",
    )

    def __init__(self, sid, harness, identity="", storage="", storage_present=False, epoch=0, git_native=None, runtime_version=""):
        self.sid = sid
        self.harness = harness
        self.identity = identity
        self.storage = storage
        self.storage_present = storage_present
        self.epoch = epoch
        self.git_native = git_native
        self.runtime_version = runtime_version


def validate_workspace_name(name):
    """Die with the standard message if ``name`` is not a valid workspace
    name. Returns the name unchanged otherwise (for chaining).
    """
    if not name or not WORKSPACE_NAME_RE.match(name):
        die(
            "invalid workspace name '{}' (use letters, digits, - and _)".format(
                name
            )
        )
    return name


def _index_path(cfg, ws):
    return cfg.ws_dir / ws


def workspace_exists(cfg, ws):
    return _index_path(cfg, ws).is_file()


def read_workspace_state(cfg, ws):
    """Read and tolerantly parse the index file for ``ws``.

    Returns a :class:`WorkspaceState`, or ``None`` if the file does not
    exist or cannot be read at all.
    """
    path = _index_path(cfg, ws)
    try:
        raw = path.read_text()
    except OSError:
        return None

    sid = ""
    harness = ""
    identity = ""
    storage = ""
    storage_present = False
    epoch = 0
    git_native = None
    runtime_version = ""
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        # Legacy bare-sid format (no harness field, not valid JSON at all).
        sid = raw.strip()
    else:
        if isinstance(parsed, dict):
            sid = parsed.get("runtimeSessionId") or parsed.get("sessionId") or ""
            harness = parsed.get("harness") or ""
            identity = parsed.get("workspaceIdentity") or ""
            storage_present = "storage" in parsed
            storage = parsed.get("storage") if storage_present else ""
            epoch = parsed.get("sessionEpoch", 0)
            candidate = parsed.get("gitNative")
            if isinstance(candidate, dict) and candidate.get("branch"):
                git_native = candidate
            version = parsed.get("runtimeVersion")
            if isinstance(version, (str, int)) and not isinstance(version, bool):
                runtime_version = str(version).strip()
        else:
            # JSON scalar (e.g. a bare quoted string): use it as the sid.
            sid = str(parsed)
    return WorkspaceState(
        sid=sid, harness=harness, identity=identity,
        storage=storage, storage_present=storage_present, epoch=epoch,
        git_native=git_native, runtime_version=runtime_version,
    )


def validate_storage_state(state, ws):
    """Return persisted storage, or legacy ``session`` when absent."""
    if not state.storage_present:
        return "session"
    if not isinstance(state.storage, str) or state.storage not in ("s3", "session"):
        die("workspace '{}' index has invalid storage metadata".format(ws))
    return state.storage


def validate_epoch_state(state, ws):
    if not isinstance(state.epoch, int) or isinstance(state.epoch, bool) or state.epoch < 0:
        die("workspace '{}' index has invalid sessionEpoch metadata".format(ws))
    return state.epoch


def save_workspace_state(cfg, ws, sid, harness, identity="", storage="", epoch=0, runtime_version=None):
    """Persist ``{"runtimeSessionId": sid[, "harness": harness]}`` for
    ``ws``, creating the workspaces directory if needed.

    An existing ``gitNative`` binding is preserved: this function is called
    by every command's harness-resolution path, none of which own the
    git-native mode (only :func:`save_git_native_state` sets it).

    ``runtime_version`` follows the same read-modify-write discipline
    (add-task-liveness-safety design D3): a non-empty value records the
    AgentCore runtime version this ``sid`` was generated on, the default
    (``None``) preserves whatever was already recorded — so the commands that
    merely re-persist a resolved session cannot erase it — and an explicit
    ``""`` drops it, which is what a manual session rotation wants: the new
    session id has not been provisioned yet, so its version is recorded at
    its first provisioning command instead of being guessed here.
    """
    cfg.ws_dir.mkdir(parents=True, exist_ok=True)
    previous = read_workspace_state(cfg, ws)
    data = {"runtimeSessionId": sid}
    if harness:
        data["harness"] = harness
    if identity:
        data["workspaceIdentity"] = identity
    if storage:
        data["storage"] = storage
    data["sessionEpoch"] = epoch
    if previous is not None and previous.git_native:
        data["gitNative"] = previous.git_native
    if runtime_version:
        data["runtimeVersion"] = str(runtime_version)
    elif (
        runtime_version is None
        and previous is not None
        and previous.runtime_version
    ):
        data["runtimeVersion"] = previous.runtime_version
    _index_path(cfg, ws).write_text(json.dumps(data) + "\n")


def save_git_native_state(cfg, ws, branch, base_sha, local_repo):
    """Record the git-native session mode for ``ws`` after a completed seed
    (add-git-native-workflow design D5): branch name, seeded base sha and
    the local repository root the seed came from (used by `sch fetch` to
    import the delivery bundle into the right repo)."""
    state = read_workspace_state(cfg, ws)
    if state is None:
        die("cannot read workspace index for '{}'".format(ws))
    try:
        data = json.loads(_index_path(cfg, ws).read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        die("cannot update workspace index for '{}'".format(ws))
    if not isinstance(data, dict):
        die("workspace '{}' index has a legacy format; recreate it before using --branch".format(ws))
    data["gitNative"] = {
        "mode": "git-native",
        "branch": branch,
        "baseSha": base_sha,
        "localRepo": str(local_repo),
    }
    _index_path(cfg, ws).write_text(json.dumps(data) + "\n")


def generate_session_id(ws):
    """Generate a new runtime session id: ``sch-<ws>-<uuid4>``.

    ``uuid.uuid4()`` already renders lowercase, matching the bash
    ``uuidgen | tr '[:upper:]' '[:lower:]'`` pipeline.
    """
    return "sch-{}-{}".format(ws, uuid.uuid4())


def mark_status(cfg, ws, status):
    """Write ``<status> <ISO-8601 UTC timestamp>`` to ``.status.<ws>``."""
    cfg.ws_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    status_path = cfg.ws_dir / ".status.{}".format(ws)
    status_path.write_text("{} {}\n".format(status, timestamp))


def read_status(cfg, ws, default="created"):
    """Read the last recorded local status line for ``ws`` (without the
    trailing newline), or ``default`` if none was ever recorded.
    """
    status_path = cfg.ws_dir / ".status.{}".format(ws)
    try:
        return status_path.read_text().strip()
    except OSError:
        return default


def list_workspace_names(cfg):
    """Return the sorted list of workspace names with an index file,
    excluding the ``.status.*`` bookkeeping files.
    """
    if not cfg.ws_dir.is_dir():
        return []
    names = [
        entry.name
        for entry in cfg.ws_dir.iterdir()
        if entry.is_file() and not entry.name.startswith(".")
    ]
    return sorted(names, key=_collation_key())


def _collation_key():
    """Sort key matching the bash reference's `for f in "${WS_DIR}"/*` glob
    order, which bash expands using the process's locale collation
    (``strcoll``) rather than raw byte order — e.g. under a typical
    ``en_US.UTF-8`` locale, ``-``/``_`` are treated as weak separators, so
    ``myws_oc`` sorts next to ``myws2`` rather than after ``myws3``. Falls
    back to plain lexicographic order if the locale module/data is
    unavailable (e.g. a minimal container image).
    """
    try:
        import locale

        locale.setlocale(locale.LC_COLLATE, "")
        return locale.strxfrm
    except (locale.Error, ImportError, ValueError):
        return lambda s: s
