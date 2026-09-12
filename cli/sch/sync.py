"""Local-only workspace sync bindings and common command-line options."""

import hashlib
import json
import os
import tempfile
from pathlib import Path

from . import deps, repo
from .config import die

BOOTSTRAP_POLICIES = ("abort", "local-wins", "remote-wins", "union")
CONFLICT_POLICIES = ("abort", "local-wins", "remote-wins", "keep-both")


def sync_dir(cfg):
    return cfg.config_dir / "sync"


def _key(workspace):
    return hashlib.sha256(workspace.encode()).hexdigest()


def binding_path(cfg, workspace):
    return sync_dir(cfg) / "bindings" / "{}.json".format(_key(workspace))


def baseline_path(cfg, workspace):
    return sync_dir(cfg) / "baselines" / "{}.json".format(_key(workspace))


def canonical_root(value):
    path = Path(value).expanduser()
    try:
        return path.resolve(strict=True)
    except OSError:
        die("sync directory '{}' does not exist or is not accessible".format(value))
    return path


def _atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".sch-sync-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle)
            handle.write("\n")
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def save_binding(cfg, workspace, root):
    root = canonical_root(root)
    _atomic_json(binding_path(cfg, workspace), {
        "version": 1,
        "workspace": workspace,
        "localRoot": str(root),
        "rootFingerprint": str(root),
        "baselinePath": str(baseline_path(cfg, workspace)),
    })
    return root


def load_binding(cfg, workspace):
    try:
        data = json.loads(binding_path(cfg, workspace).read_text())
        root = Path(data["localRoot"])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if data.get("version") != 1 or data.get("workspace") != workspace or not root.is_dir():
        die("saved sync binding for '{}' is unavailable; use --sync <dir> to rebind or --no-sync to continue without sync".format(workspace))
    return root.resolve()


def read_binding_root(cfg, workspace):
    """Return the saved local-sync root path for ``workspace``, or ``None``.

    Display-only helper: reads the stored ``localRoot`` without checking
    whether the directory still exists.  Unlike :func:`load_binding` this
    function never calls ``die()`` and is safe to use in non-mutating
    observation commands (``list``, ``status``).
    """
    try:
        data = json.loads(binding_path(cfg, workspace).read_text())
        root_str = data.get("localRoot", "")
    except (AttributeError, OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if data.get("version") != 1 or data.get("workspace") != workspace or not root_str:
        return None
    return Path(root_str)


def parse_options(args, usage_msg, allow_prompt=False):
    """Return remaining args plus invocation-only sync configuration."""
    result = {
        "sync": "", "no_sync": False, "bootstrap": "abort",
        "conflict": "abort", "storage": "",
    }
    remaining = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            # Flags after `--` are prompt/shell arguments, not SCH options.
            remaining.extend(args[i:])
            break
        if arg in ("--sync", "--bootstrap", "--conflict", "--storage"):
            if i + 1 >= len(args):
                die("usage: {} <value>".format(arg))
            value = args[i + 1]
            key = arg[2:].replace("-", "_")
            if key == "bootstrap" and value not in BOOTSTRAP_POLICIES:
                die("--bootstrap must be one of: {}".format(", ".join(BOOTSTRAP_POLICIES)))
            if key == "conflict" and value not in CONFLICT_POLICIES:
                die("--conflict must be one of: {}".format(", ".join(CONFLICT_POLICIES)))
            if key == "storage" and value not in ("s3", "session"):
                die("--storage must be one of: s3, session")
            result[key] = value
            i += 2
        elif arg == "--no-sync":
            result["no_sync"] = True
            i += 1
        else:
            remaining.append(arg)
            i += 1
    if result["no_sync"] and (result["sync"] or result["bootstrap"] != "abort" or result["conflict"] != "abort"):
        die("--no-sync cannot be combined with --sync, --bootstrap, or --conflict")
    return remaining, result


def resolve_binding(cfg, workspace, options):
    """Apply --no-sync > --sync > saved binding > disabled precedence."""
    if options["no_sync"]:
        return None
    if options["sync"]:
        return save_binding(cfg, workspace, options["sync"])
    return load_binding(cfg, workspace)


def helper_argv(
    cfg, runtime_arn, session_id, workspace, root, baseline,
    bootstrap, conflict, mode, storage="session", session_epoch=0,
):
    """Resolve all local Node/AWS prerequisites before starting a workload."""
    node_bin = deps.resolve_node_bin(cfg)
    aws_bin = deps.resolve_aws_bin(cfg)
    deps.prepend_path(str(Path(node_bin).parent), str(Path(aws_bin).parent))
    tunnel_dir = repo.tunnel_dir()
    if tunnel_dir is None:
        die(repo.missing_message("sync"))
    helper = tunnel_dir / "sync.js"
    if not helper.is_file():
        die("cannot find {}/sync.js (repo layout unexpected)".format(tunnel_dir))
    if not (tunnel_dir / "node_modules").is_dir():
        die("tunnel/ dependencies not installed — run: (cd '{}' && npm install)".format(tunnel_dir))
    return [
        node_bin,
        str(helper),
        "--{}".format(mode),
        "--region", cfg.region,
        "--runtime-arn", runtime_arn,
        "--session-id", session_id,
        "--workspace", workspace,
        "--storage", storage,
        "--session-epoch", str(session_epoch),
        "--root", str(root),
        "--baseline", str(baseline),
        "--bootstrap", bootstrap,
        "--conflict", conflict,
    ]
