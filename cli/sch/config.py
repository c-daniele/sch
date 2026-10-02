"""SCH_* environment variables, the XDG config dir, and the runtime stack
outputs (runtime ARN, checkpoint bucket) resolved via `aws cloudformation
describe-stacks` and cached per AWS account, region and stack
(cli-cross-platform R9a).
"""

import contextlib
import os
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlparse

from . import plane as plane_mod
from . import procs
from . import userenv

DEFAULT_REGION = "eu-west-1"
DEFAULT_PROJECT = "sch"
DEFAULT_ENV = "dev"
DEFAULT_HARNESS = "opencode"
DEFAULT_STORAGE = "s3"
DEFAULT_TUNNEL_MAX_CHANNELS = "32"

# cli-cross-platform R9a (TASK-25): stack outputs are cached under
# stack-outputs/<account>/<region>/<stack>/, so a value resolved with one
# account's credentials is never used with another's. The flat files of the
# earlier layout held whichever deployment resolved first: on a laptop with
# two accounts, `sch status` read the task status from the other account's
# bucket and reported `none` for a running task.
STACK_OUTPUTS_DIR = "stack-outputs"
LEGACY_CACHE_FILES = ("runtime-arn", "checkpoint-bucket")
_ACCOUNT_RE = re.compile(r"[0-9]{12}")
_PATH_SEGMENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,127}")
_ACCOUNT_LOCK = threading.Lock()


def die(message):
    """Print ``sch: <message>`` on stderr and terminate with exit code 1.

    Mirrors the bash ``die()`` helper. Never returns.
    """
    print("sch: {}".format(message), file=sys.stderr)
    raise SystemExit(1)


def _env(name, default=""):
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


class Config:
    """Resolved configuration for a single CLI invocation.

    All values are read once from the environment at construction time,
    matching the bash script's top-level variable assignments.
    """

    def __init__(self):
        self.region = _env("SCH_REGION", DEFAULT_REGION)
        self.project = _env("SCH_PROJECT", DEFAULT_PROJECT)
        self.env = _env("SCH_ENV", DEFAULT_ENV)
        self.default_harness = _env("SCH_DEFAULT_HARNESS", DEFAULT_HARNESS)
        self.default_storage = _env("SCH_DEFAULT_STORAGE", DEFAULT_STORAGE)
        # Not consumed directly by sch (only by tunnel/attach.js, which
        # reads it from its own environment), but documented/plumbed here
        # per task 1.2 for completeness and so `sch info`-style tooling
        # could surface it later.
        self.tunnel_max_channels = _env(
            "SCH_TUNNEL_MAX_CHANNELS", DEFAULT_TUNNEL_MAX_CHANNELS
        )

        self.runtime_arn_override = os.environ.get("SCH_RUNTIME_ARN") or ""
        self.checkpoint_bucket_override = (
            os.environ.get("SCH_CHECKPOINT_BUCKET") or ""
        )
        self.acp_mirror_root_override = os.environ.get("SCH_ACP_MIRROR_ROOT") or ""
        self.node_bin_override = os.environ.get("SCH_NODE_BIN") or ""
        self.aws_bin_override = os.environ.get("SCH_AWS_BIN") or ""
        self.workspace_registry_url = os.environ.get("SCH_WORKSPACE_REGISTRY_URL", "").rstrip("/")
        if self.workspace_registry_url:
            parsed = urlparse(self.workspace_registry_url)
            if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
                die("SCH_WORKSPACE_REGISTRY_URL must be an HTTPS endpoint URL without query or fragment")

        xdg = os.environ.get("XDG_CONFIG_HOME") or ""
        if xdg:
            base = xdg
        else:
            home = os.environ.get("HOME") or os.environ.get("USERPROFILE") or "/tmp"
            base = os.path.join(home, ".config")
        self.config_dir = Path(base) / "sch"
        self.ws_dir = self.config_dir / "workspaces"
        self._provider_keys = None
        # cli-cross-platform R9a: the account of the current credentials,
        # looked up at most once per command by caller_account().
        self.caller_account_id = None
        self.caller_account_error = None
        # per-principal-isolation R40: learned from the first registry
        # response of the command (None = not known yet).
        self.isolation = None
        self.plane = None

    @property
    def provider_keys(self):
        """Provider API keys configured by this user in ``~/.sch/env``.

        Resolved lazily and cached for the whole command (add-user-provider-keys
        design D2): a single `sch` command builds several payloads and they must
        all carry the same set. ``{}`` when the file is absent, empty or too
        permissive — never an error (see :mod:`sch.userenv`).
        """
        if self._provider_keys is None:
            self._provider_keys = userenv.load_provider_keys()
        return dict(self._provider_keys)

    @property
    def acp_mirror_root(self):
        if self.acp_mirror_root_override:
            return Path(self.acp_mirror_root_override)
        return self.config_dir / "mirrors"

    def stack_name(self):
        return "{}-{}-runtime".format(self.project, self.env)


def _lookup_account(region):
    """``(account, "")`` for the current credentials, or ``("", message)``."""
    try:
        result = subprocess.run(
            [
                "aws", "sts", "get-caller-identity", "--query", "Account",
                "--output", "text", "--region", region,
            ],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return "", "cannot determine the AWS account: the aws CLI is not installed or not on PATH"
    account = (result.stdout or "").strip()
    if result.returncode == 0 and _ACCOUNT_RE.fullmatch(account):
        return account, ""
    detail = (
        procs.aws_error_detail(result.stderr) if result.returncode != 0
        else "unexpected answer {!r}".format(account)
    )
    return "", (
        "cannot determine the AWS account of the current credentials ({}); "
        "refresh them, or set SCH_RUNTIME_ARN and SCH_CHECKPOINT_BUCKET".format(detail)
    )


def caller_account(cfg):
    """The AWS account of the current credentials (``sts get-caller-identity``).

    The stack-output cache is keyed by it (cli-cross-platform R9a). Looked up
    at most once per configuration object, failure included, so the callers
    that degrade on ``SystemExit`` (dashboard rows, ``runtime_id``) never
    repeat the call. Dies when STS cannot answer: every later AWS call of the
    command would fail the same way.
    """
    with _ACCOUNT_LOCK:
        account = getattr(cfg, "caller_account_id", None)
        if account:
            return account
        error = getattr(cfg, "caller_account_error", None)
        if not error:
            account, error = _lookup_account(cfg.region)
            if account:
                cfg.caller_account_id = account
                return account
            cfg.caller_account_error = error
    die(error)


def stack_outputs_dir(cfg, account, region, stack):
    """``<config>/stack-outputs/<account>/<region>/<stack>``, or ``None`` when
    a component is not a plain path segment (that value is then never cached).
    """
    if not isinstance(account, str) or not _ACCOUNT_RE.fullmatch(account):
        return None
    for part in (region, stack):
        if not isinstance(part, str) or not _PATH_SEGMENT_RE.fullmatch(part):
            return None
    return Path(cfg.config_dir) / STACK_OUTPUTS_DIR / account / region / stack


def legacy_cache_paths(cfg):
    """The flat cache files of the earlier layout, never read any more."""
    return [Path(cfg.config_dir) / name for name in LEGACY_CACHE_FILES]


def _read_cached(path):
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _write_cached(cfg, path, value):
    """Best effort: a cache that cannot be written costs one more lookup."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".{}-".format(path.name), dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(value + "\n")
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.remove(tmp)
            raise
    except OSError:
        return
    for legacy in legacy_cache_paths(cfg):
        with contextlib.suppress(OSError):
            legacy.unlink()


def _describe_stack_output(cfg, output_key, cache_name, override, missing_env_hint):
    if override:
        return override
    stack = cfg.stack_name()
    cache_dir = stack_outputs_dir(cfg, caller_account(cfg), cfg.region, stack)
    cache_path = cache_dir / cache_name if cache_dir is not None else None
    if cache_path is not None:
        cached = _read_cached(cache_path)
        if cached:
            return cached
    try:
        result = subprocess.run(
            [
                "aws",
                "cloudformation",
                "describe-stacks",
                "--stack-name",
                stack,
                "--region",
                cfg.region,
                "--query",
                "Stacks[0].Outputs[?OutputKey=='{}'].OutputValue".format(
                    output_key
                ),
                "--output",
                "text",
            ],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        die(
            "cannot resolve {} (stack {} in {}: the aws CLI is not installed or not "
            "on PATH); set {}".format(
                missing_env_hint[1], stack, cfg.region, missing_env_hint[0]
            )
        )
    if result.returncode != 0:
        die(
            "cannot resolve {} (stack {} in {}: {}); set {}".format(
                missing_env_hint[1], stack, cfg.region,
                procs.aws_error_detail(result.stderr), missing_env_hint[0]
            )
        )
    value = (result.stdout or "").strip()
    if not value or value == "None":
        die("runtime stack found but no {} output".format(output_key))
    if cache_path is not None:
        _write_cached(cfg, cache_path, value)
    return value


def runtime_arn(cfg):
    """Resolve the AgentCore runtime ARN: ``SCH_RUNTIME_ARN``, then the
    stack-output cache of the current account, region and stack, then the
    CloudFormation stack output (cached on disk).

    With isolation on (per-principal-isolation R40) it is the owner's plane
    runtime from the registry, and neither ``SCH_RUNTIME_ARN``, the cache nor
    the stack output is ever consulted.
    """
    plane = plane_mod.active_plane(cfg)
    if plane is not None:
        return plane.runtime_arn
    return _describe_stack_output(
        cfg,
        "RuntimeArn",
        "runtime-arn",
        cfg.runtime_arn_override,
        ("SCH_RUNTIME_ARN", "runtime ARN"),
    )


def checkpoint_bucket(cfg):
    """Resolve the L2 checkpoint S3 bucket name, same precedence/caching as
    :func:`runtime_arn`.
    """
    return _describe_stack_output(
        cfg,
        "CheckpointBucketName",
        "checkpoint-bucket",
        cfg.checkpoint_bucket_override,
        ("SCH_CHECKPOINT_BUCKET", "checkpoint bucket"),
    )


def runtime_id(cfg):
    """The AgentCore runtime id, derived from the resolved runtime ARN
    (``arn:...:runtime/<id>``). ``""`` when the ARN cannot be resolved or
    does not carry a resource id.
    """
    try:
        arn = runtime_arn(cfg)
    except SystemExit:
        # Never fatal: the callers of this helper degrade instead of dying.
        return ""
    if not arn or "/" not in arn:
        return ""
    return arn.rsplit("/", 1)[1].strip()


def deployed_runtime_version(cfg):
    """The AgentCore runtime version currently deployed, or ``""``.

    Deliberately never fatal (add-task-liveness-safety design D3, failure
    mode): an unavailable control plane, a missing permission or an absent
    AWS CLI must degrade to "unknown version" — the caller then proceeds on
    the existing session with a warning instead of terminating. Not cached:
    this is one ~100ms control-plane call per provisioning command.
    """
    identifier = runtime_id(cfg)
    if not identifier:
        return ""
    try:
        result = subprocess.run(
            [
                "aws",
                "bedrock-agentcore-control",
                "get-agent-runtime",
                "--agent-runtime-id",
                identifier,
                "--region",
                cfg.region,
                "--query",
                "agentRuntimeVersion",
                "--output",
                "text",
            ],
            capture_output=True,
            text=True,
        )
    except OSError:
        return ""
    if result.returncode != 0:
        return ""
    value = (result.stdout or "").strip()
    if not value or value == "None":
        return ""
    return value
