"""Shared upload/download plumbing for files staged through tunnel/bundle.js."""

import json
import subprocess
from pathlib import Path

from . import deps, repo
from .config import die

_STAGING_ROOTS = {
    "s3": "/home/sch/workspace/state/bundles",
    "session": "/mnt/workspace/state/bundles",
}


def staging_root(storage):
    return _STAGING_ROOTS.get(storage or "session", _STAGING_ROOTS["session"])


def bundle_helper_argv(
    cfg, runtime_arn, session_id, workspace_identity, storage, session_epoch,
    name, upload=None, download=None,
):
    node_bin = deps.resolve_node_bin(cfg)
    aws_bin = deps.resolve_aws_bin(cfg)
    deps.prepend_path(str(Path(node_bin).parent), str(Path(aws_bin).parent))
    tunnel_dir = repo.tunnel_dir()
    if tunnel_dir is None:
        die(repo.missing_message("bundle transfer"))
    helper = tunnel_dir / "bundle.js"
    if not helper.is_file():
        die("cannot find {}/bundle.js (repo layout unexpected)".format(tunnel_dir))
    if not (tunnel_dir / "node_modules").is_dir():
        die("tunnel/ dependencies not installed — run: (cd '{}' && npm install)".format(tunnel_dir))
    argv = [
        node_bin, str(helper), "--region", cfg.region,
        "--runtime-arn", runtime_arn, "--session-id", session_id,
        "--workspace", workspace_identity, "--storage", storage or "session",
        "--session-epoch", str(session_epoch), "--staging-root", staging_root(storage),
        "--name", name,
    ]
    if upload:
        argv += ["--upload", str(upload)]
    if download:
        argv += ["--download", str(download)]
    return argv


def run_bundle_helper(argv, doing):
    try:
        proc = subprocess.run(argv, stdout=subprocess.PIPE, text=True)
    except OSError as exc:
        die("cannot start the bundle transfer helper: {}".format(exc))
    control = None
    for line in (proc.stdout or "").splitlines():
        try:
            control = json.loads(line.strip())
        except (json.JSONDecodeError, ValueError):
            continue
    if proc.returncode != 0 or not isinstance(control, dict) or control.get("type") != "done":
        message = control.get("message", "") if isinstance(control, dict) else ""
        die("{} failed{}".format(doing, ": {}".format(message) if message else ""))
    return control
