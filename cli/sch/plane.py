"""Per-principal isolation plane on the client side (spec:
per-principal-isolation R40-R43).

With isolation on, the registry answers every request with
``"isolation": true`` and the caller's plane: the owner's runtime ARN, the
read-only access role ARN and the owner prefix. This module validates that
plane strictly (R41), makes it the only runtime of the command (no fallback
to the shared runtime, ``SCH_RUNTIME_ARN`` or the cached runtime ARN, R40),
and reads owner checkpoint objects through the access role with credentials
kept in memory and refreshed before they expire.

With isolation off nothing here changes a request: :func:`active_plane`
returns ``None`` and the helpers return their isolation-off values.
"""

import datetime
import json
import os
import re
import subprocess
import threading
import time

OWNER_PREFIX_RE = re.compile(r"^o\.([0-9a-f]{16})$")
RUNTIME_ARN_RE = re.compile(
    r"^arn:aws[a-z-]*:bedrock-agentcore:[a-z0-9-]+:\d{12}:runtime/"
    r"(?P<name>[a-zA-Z][a-zA-Z0-9_]{0,47})-[a-zA-Z0-9]{10}$"
)
ROLE_ARN_RE = re.compile(
    r"^arn:aws[a-z-]*:iam::\d{12}:role/(?:[\w+=,.@-]+/)*(?P<name>[\w+=,.@-]{1,64})$"
)

# R22: the access role allows sessions of at most one hour. Credentials are
# renewed this long before they expire, so a request never carries
# credentials that lapse mid-call.
ACCESS_SESSION_SECONDS = 3600
REFRESH_MARGIN_S = 300

# Variables that could make an ``aws`` subprocess pick other credentials than
# the access-role session passed in its environment.
_CREDENTIAL_VARS = (
    "AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN",
    "AWS_CREDENTIAL_EXPIRATION", "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_ROLE_ARN",
    "AWS_ROLE_SESSION_NAME", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI", "AWS_CONTAINER_AUTHORIZATION_TOKEN",
)


class PlaneError(RuntimeError):
    pass


class Plane:
    __slots__ = ("runtime_arn", "access_role_arn", "owner_prefix", "owner_key")

    def __init__(self, runtime_arn, access_role_arn, owner_prefix, owner_key):
        self.runtime_arn = runtime_arn
        self.access_role_arn = access_role_arn
        self.owner_prefix = owner_prefix
        self.owner_key = owner_key

    def __eq__(self, other):
        return isinstance(other, Plane) and all(
            getattr(self, name) == getattr(other, name) for name in self.__slots__
        )

    def __ne__(self, other):
        return not self == other

    def __repr__(self):
        return "Plane({!r}, {!r}, {!r})".format(
            self.runtime_arn, self.access_role_arn, self.owner_prefix
        )


def parse_plane(cfg, data):
    """R41: a registry ``plane`` object, validated against the deployment."""
    if not isinstance(data, dict):
        raise ValueError("registry returned no valid plane for an isolated deployment")
    runtime_arn = data.get("runtimeArn")
    role_arn = data.get("accessRoleArn")
    prefix = data.get("ownerPrefix")
    prefix_match = OWNER_PREFIX_RE.fullmatch(prefix) if isinstance(prefix, str) else None
    if prefix_match is None:
        raise ValueError("registry returned an invalid plane owner prefix")
    owner_key = prefix_match.group(1)
    runtime_match = RUNTIME_ARN_RE.fullmatch(runtime_arn) if isinstance(runtime_arn, str) else None
    expected_runtime = "{}_{}_o_{}".format(cfg.project, cfg.env, owner_key)
    if runtime_match is None or runtime_match.group("name") != expected_runtime:
        raise ValueError(
            "registry returned a plane runtime ARN that is not runtime {} of this "
            "deployment".format(expected_runtime)
        )
    role_match = ROLE_ARN_RE.fullmatch(role_arn) if isinstance(role_arn, str) else None
    expected_role = "{}-{}-o-{}-access".format(cfg.project, cfg.env, owner_key)
    if role_match is None or role_match.group("name") != expected_role:
        raise ValueError(
            "registry returned a plane access role ARN that is not role {} of this "
            "deployment".format(expected_role)
        )
    return Plane(runtime_arn, role_arn, prefix, owner_key)


def adopt(cfg, data):
    """Record what a registry response says about isolation on ``cfg``.

    ``data`` is the whole response object. Returns the plane (or ``None`` with
    isolation off). Raises ``ValueError`` on a missing or malformed plane, and
    on a response that contradicts what an earlier response of the same
    command said.
    """
    if not isinstance(data, dict):
        raise ValueError("registry returned an invalid response")
    flag = data.get("isolation", False)
    if flag is not True and flag is not False:
        raise ValueError("registry returned an invalid isolation flag")
    known = getattr(cfg, "isolation", None)
    if known is not None and known != flag:
        raise ValueError("registry changed its isolation mode during the command")
    if not flag:
        if "plane" in data:
            raise ValueError("registry returned a plane without isolation")
        cfg.isolation = False
        cfg.plane = None
        return None
    plane = parse_plane(cfg, data.get("plane"))
    current = getattr(cfg, "plane", None)
    if current is not None and current.owner_key != plane.owner_key:
        raise ValueError("registry returned the plane of another owner during the command")
    cfg.isolation = True
    cfg.plane = plane
    return plane


def record_plane(cfg, record, plane):
    """R38/R41: a record of an isolated response carries the same plane."""
    if plane is None:
        if isinstance(record, dict) and "plane" in record:
            raise ValueError("registry returned a plane without isolation")
        return None
    if not isinstance(record, dict) or "plane" not in record:
        raise ValueError("registry returned a workspace record without its plane")
    parsed = parse_plane(cfg, record["plane"])
    if parsed != plane:
        raise ValueError("registry returned a workspace record on another plane")
    return parsed


def active_plane(cfg):
    """The plane of this command, or ``None`` with isolation off.

    With the registry enabled and no registry response seen yet, asks the
    registry first (``GET /workspaces``), so no command ever falls back to
    the shared runtime of an isolated deployment (R40). Dies when the
    registry cannot tell.
    """
    plane = getattr(cfg, "plane", None)
    if plane is not None:
        return plane
    if not getattr(cfg, "workspace_registry_url", ""):
        return None
    if getattr(cfg, "isolation", None) is None:
        from . import workspace_registry
        from .config import die
        try:
            workspace_registry.list_workspaces(cfg)
        except (RuntimeError, ValueError) as exc:
            die(str(exc))
    plane = getattr(cfg, "plane", None)
    if getattr(cfg, "isolation", None) and plane is None:
        from .config import die
        die("the registry reported isolation without a plane; refusing the shared runtime")
    return plane


def tree_prefix(cfg, tree):
    """``<tree>/`` with isolation off, ``<tree>/<ownerPrefix>/`` with it on (R26)."""
    plane = active_plane(cfg)
    if plane is None:
        return "{}/".format(tree)
    return "{}/{}/".format(tree, plane.owner_prefix)


def checkpoint_key(cfg, runtime_workspace, name):
    """``checkpoints/[<ownerPrefix>/]<ws>/<name>`` (R26)."""
    return "{}{}/{}".format(tree_prefix(cfg, "checkpoints"), runtime_workspace, name)


# --- access-role credentials ---------------------------------------------------

_LOCK = threading.Lock()
_SESSIONS = {}


def _parse_expiration(value):
    if not isinstance(value, str) or not value:
        raise PlaneError("the access role returned no credential expiration")
    text = value.strip().replace("Z", "+00:00")
    try:
        stamp = datetime.datetime.fromisoformat(text)
    except ValueError as exc:
        raise PlaneError("the access role returned an invalid credential expiration") from exc
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=datetime.timezone.utc)
    return stamp.timestamp()


def assume_role_argv(cfg, plane):
    return [
        "aws", "sts", "assume-role",
        "--role-arn", plane.access_role_arn,
        "--role-session-name", "sch-cli-{}".format(plane.owner_key),
        "--duration-seconds", str(ACCESS_SESSION_SECONDS),
        "--region", cfg.region,
        "--output", "json",
    ]


def _assume(cfg, plane):
    try:
        result = subprocess.run(assume_role_argv(cfg, plane), capture_output=True, text=True)
    except OSError as exc:
        raise PlaneError("cannot execute the AWS CLI to assume the access role") from exc
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        raise PlaneError(
            "cannot assume the access role {} ({}); your identity policy needs "
            "sts:AssumeRole on it".format(plane.access_role_arn,
                                          detail[-1] if detail else "unknown error")
        )
    try:
        creds = json.loads(result.stdout or "{}")["Credentials"]
        values = (creds["AccessKeyId"], creds["SecretAccessKey"], creds["SessionToken"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PlaneError("the access role returned invalid credentials") from exc
    if not all(isinstance(v, str) and v for v in values):
        raise PlaneError("the access role returned invalid credentials")
    return values, _parse_expiration(creds.get("Expiration"))


def access_env(cfg, plane, clock=None):
    """Environment for an ``aws s3api`` subprocess that reads as the access role.

    The credentials live only in memory (never on disk or in argv) and are
    renewed :data:`REFRESH_MARGIN_S` seconds before they expire, so a
    long-running ``sch dashboard`` keeps working past the first expiry.
    """
    now = (clock or time.time)()
    with _LOCK:
        cached = _SESSIONS.get(plane.access_role_arn)
        if cached is None or cached[1] - REFRESH_MARGIN_S <= now:
            cached = _assume(cfg, plane)
            _SESSIONS[plane.access_role_arn] = cached
        (key_id, secret, token), _expiry = cached
    env = {k: v for k, v in os.environ.items() if k not in _CREDENTIAL_VARS}
    env["AWS_ACCESS_KEY_ID"] = key_id
    env["AWS_SECRET_ACCESS_KEY"] = secret
    env["AWS_SESSION_TOKEN"] = token
    return env


def s3_run_options(cfg):
    """``subprocess.run`` keyword arguments for an owner checkpoint read.

    ``{}`` with isolation off (the caller's own credentials, unchanged), else
    ``{"env": ...}`` with the access-role session.
    """
    plane = active_plane(cfg)
    if plane is None:
        return {}
    return {"env": access_env(cfg, plane)}


def reset_sessions():
    """Forget every cached access-role session (tests)."""
    with _LOCK:
        _SESSIONS.clear()
