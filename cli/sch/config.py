"""SCH_* environment variables, XDG config dir, and cached lookups
(runtime ARN, checkpoint bucket) resolved via `aws cloudformation
describe-stacks`.

Mirrors the configuration section of the bash reference implementation
(``bin/sch``, lines ~61-125) and its PowerShell twin (``bin/sch.ps1``,
lines ~56-182). No new defaults, no new environment variables.
"""

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from . import userenv

DEFAULT_REGION = "eu-west-1"
DEFAULT_PROJECT = "sch"
DEFAULT_ENV = "dev"
DEFAULT_HARNESS = "opencode"
DEFAULT_STORAGE = "s3"
DEFAULT_TUNNEL_MAX_CHANNELS = "32"


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
        self.runtime_arn_cache = self.config_dir / "runtime-arn"
        self.checkpoint_bucket_cache = self.config_dir / "checkpoint-bucket"
        self._provider_keys = None

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


def _describe_stack_output(cfg, output_key, cache_path, override, missing_env_hint):
    if override:
        return override
    if cache_path.is_file():
        return cache_path.read_text().strip()
    try:
        result = subprocess.run(
            [
                "aws",
                "cloudformation",
                "describe-stacks",
                "--stack-name",
                cfg.stack_name(),
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
            "cannot resolve {} (stack {} in {}); set {}".format(
                missing_env_hint[1], cfg.stack_name(), cfg.region, missing_env_hint[0]
            )
        )
    if result.returncode != 0:
        die(
            "cannot resolve {} (stack {} in {}); set {}".format(
                missing_env_hint[1], cfg.stack_name(), cfg.region, missing_env_hint[0]
            )
        )
    value = (result.stdout or "").strip()
    if not value or value == "None":
        die("runtime stack found but no {} output".format(output_key))
    cfg.config_dir.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(value + "\n")
    return value


def runtime_arn(cfg):
    """Resolve the AgentCore runtime ARN (env override, then cache, then
    CloudFormation stack output), caching the result on disk.
    """
    return _describe_stack_output(
        cfg,
        "RuntimeArn",
        cfg.runtime_arn_cache,
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
        cfg.checkpoint_bucket_cache,
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
