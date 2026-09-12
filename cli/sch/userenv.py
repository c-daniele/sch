"""``~/.sch/env``: the per-user source of the provider API keys
(add-user-provider-keys, design D2).

The provider keys are a property of the USER, not of the deployment: the CLI
reads them from a dotenv file in the user's home (mode ``0600``, outside any
repository — same posture as ``~/.aws/credentials``) and forwards them in the
payload of every ``invoke-agent-runtime`` call. The deployment injects no
provider secret at all any more; Bedrock via execution role stays the only
global provider.

Three properties this module owns (spec: user-provider-keys):

* **Allowlist, not passthrough.** Only the names in
  :data:`PROVIDER_KEY_NAMES` ever leave this machine. The file belongs to the
  user and may one day hold anything (AWS credentials, tokens, shell exports);
  "forward everything" would turn a convenience file into an exfiltration
  channel.
* **Never fatal.** A missing, empty, unreadable or syntactically broken file
  is exactly equivalent to "no provider keys configured" — the same behaviour
  as a deployment without external providers. `sch` must never fail because
  of this file.
* **Never leak.** Nothing in this module ever puts a key VALUE in a log, a
  warning or an exception; only variable NAMES may appear.
"""

import os
import re
import stat
import sys
from pathlib import Path

# The only variables that are ever forwarded to the runtime. Kept in the
# canonical (unprefixed) spelling the user knows from each provider's docs; the
# ``SCH_`` prefix is added inside the microVM, where the dispatcher maps them
# back onto the canonical names per harness.
#
# GITHUB_TOKEN is the opt-in forge credential (TASK-26), not a model provider:
# a GitHub token (fine-grained PAT) that lets the remote harness push to
# ``origin`` and drive ``gh`` itself. It rides the same staging, transport and
# secrecy rules as the provider keys; the shim applies it to git/``gh`` only
# on git-native workspaces and never persists it.
#
# BEDROCK_API_KEY is the cross-account escape hatch (TASK-19): an Amazon
# Bedrock API key (bearer token) issued by ANOTHER account — the runtime stays
# deployed in the operator's account while inference (model access, quotas,
# billing) happens in the key's account. The dispatcher maps it onto
# AWS_BEARER_TOKEN_BEDROCK, which every harness's Bedrock client consumes and
# every non-Bedrock AWS call ignores (the execution role keeps covering S3
# checkpoints, DynamoDB, AgentCore and the aws-mcp MCP server).
PROVIDER_KEY_NAMES = (
    "ANTHROPIC_API_KEY",
    "OPENCODE_API_KEY",
    "OPENROUTER_API_KEY",
    "KILO_API_KEY",
    "BEDROCK_API_KEY",
    "GITHUB_TOKEN",
)

# Relative to the user's home, so it survives repository moves and never lands
# in a worktree by accident.
USER_ENV_RELPATH = (".sch", "env")

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# One read per process: every `sch` command builds several payloads (warmup +
# action + advisory) and they must all carry the same set — and any warning
# must be printed once, not once per invoke.
_CACHE = {"keys": None}


def user_env_path():
    """Absolute path of the user env file (``~/.sch/env``).

    Home resolution mirrors :mod:`sch.config` (``HOME`` then ``USERPROFILE``)
    so the Windows twin behaves like the POSIX one.
    """
    home = os.environ.get("HOME") or os.environ.get("USERPROFILE") or ""
    return Path(home).joinpath(*USER_ENV_RELPATH)


def parse_dotenv(text):
    """Tolerant dotenv parse: ``KEY=VALUE`` lines, ``#`` comments.

    Deliberately permissive (spec: "Righe malformate non bloccano il
    comando"): an uninterpretable line is skipped, never an error. Accepts the
    ``export KEY=VALUE`` spelling (the file is often sourced by hand too) and
    strips one layer of matching quotes. Last assignment of a name wins, as in
    a shell.
    """
    values = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export ") or line.startswith("export\t"):
            line = line[len("export"):].strip()
        name, sep, value = line.partition("=")
        if not sep:
            continue
        name = name.strip()
        if not _KEY_RE.match(name):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[name] = value
    return values


def is_world_readable(mode):
    """True when a mode grants any group/other permission.

    "Readable by others" is checked as "not exclusively the owner's": a file
    another user can write or execute is no better than one they can read.
    """
    return bool(stat.S_IMODE(mode) & 0o077)


def _warn(message, warn):
    if warn is None:
        return
    warn("sch: {}".format(message))


def _default_warn(message):
    print(message, file=sys.stderr)


def load_provider_keys(path=None, warn=_default_warn, use_cache=True):
    """The provider keys configured by the user, as ``{NAME: value}``.

    Returns an empty dict — never raises — when the file is absent, empty,
    unreadable, configures none of the allowlisted names, or is readable by other
    users. In the last case a warning naming the remedy (``chmod 600``) is
    emitted and the keys are NOT forwarded: a lax file is a real credential
    exposure and silently using it would hide it, while failing the command
    would punish the user for a file they may not even know is there.
    """
    if path is None and use_cache and _CACHE["keys"] is not None:
        return dict(_CACHE["keys"])
    resolved = Path(path) if path is not None else user_env_path()
    keys = _load(resolved, warn)
    if path is None and use_cache:
        _CACHE["keys"] = dict(keys)
    return keys


def _load(resolved, warn):
    try:
        info = resolved.stat()
    except OSError:
        # Absent (the common case) or unreachable: no keys, no warning, no
        # failure — identical to a deployment with no external providers.
        return {}
    if not stat.S_ISREG(info.st_mode):
        _warn("{} is not a regular file; ignoring it".format(resolved), warn)
        return {}
    # Permission bits are meaningless on Windows (and os.stat reports them
    # synthetically), so the check is POSIX-only: enforcing it there would
    # reject every file on a platform that cannot express 0600 this way.
    if os.name == "posix" and is_world_readable(info.st_mode):
        _warn(
            "{} is readable by other users (mode {:04o}); provider keys NOT "
            "forwarded. Fix with: chmod 600 {}".format(
                resolved, stat.S_IMODE(info.st_mode), resolved
            ),
            warn,
        )
        return {}
    try:
        text = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        # Only the errno/strerror is reported: an OSError on this path can
        # never carry a key value, but the message is kept name-only anyway.
        _warn("cannot read {} ({}); provider keys not forwarded".format(
            resolved, exc.strerror or "unreadable"
        ), warn)
        return {}
    return select_provider_keys(parse_dotenv(text))


def select_provider_keys(values):
    """Allowlist filter: keep only the allowlisted names, non-empty.

    An empty value is dropped rather than forwarded: an empty credential makes
    a harness fail in a confusing way, so "configured but empty" must behave
    exactly like "not configured" (same rule the dispatcher applies inside the
    microVM). Values carrying a newline or NUL are dropped too — they cannot
    survive the one-line-per-key staging file and can only come from a
    malformed edit.
    """
    keys = {}
    for name in PROVIDER_KEY_NAMES:
        value = values.get(name)
        if not isinstance(value, str):
            continue
        value = value.strip()
        if not value or "\n" in value or "\r" in value or "\0" in value:
            continue
        keys[name] = value
    return keys


def reset_cache():
    """Drop the per-process cache (tests; never needed in a real CLI run)."""
    _CACHE["keys"] = None
