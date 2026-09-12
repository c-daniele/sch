"""Destructive workspace primitives.

This module deliberately uses only the Python standard library.  The CLI is
the operator/control-plane process, so it uses the user's AWS CLI credentials
instead of granting delete permissions to the runtime.
"""

import json
import os
import subprocess
import tempfile
from pathlib import Path

from . import sync
from .config import checkpoint_bucket


class DeletionError(RuntimeError):
    def __init__(self, phase, message):
        super().__init__(message)
        self.phase = phase


def scopes(identity):
    return ("checkpoints/{}/".format(identity),
            "checkpoint-generations/{}/".format(identity),
            "workspace-writers/{}.json".format(identity))


def _aws(cfg, args):
    try:
        return subprocess.run(["aws"] + args + ["--region", cfg.region],
                              capture_output=True, text=True)
    except OSError as exc:
        raise DeletionError("S3 purge", "cannot execute AWS CLI") from exc


def _versions(cfg, bucket, prefix):
    key_marker = version_marker = None
    found = []
    while True:
        args = ["s3api", "list-object-versions", "--bucket", bucket,
                "--output", "json"]
        args += ["--prefix", prefix]
        if key_marker:
            args += ["--key-marker", key_marker]
        if version_marker:
            args += ["--version-id-marker", version_marker]
        result = _aws(cfg, args)
        if result.returncode:
            raise DeletionError("S3 purge", "cannot list S3 versions")
        try:
            data = json.loads(result.stdout or "{}")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise DeletionError("S3 purge", "invalid S3 version listing") from exc
        for key in ("Versions", "DeleteMarkers"):
            for item in data.get(key, []):
                if item.get("Key") == prefix or (prefix.endswith("/") and item.get("Key", "").startswith(prefix)):
                    found.append({"Key": item["Key"], "VersionId": item["VersionId"]})
        if not data.get("IsTruncated"):
            return found
        key_marker = data.get("NextKeyMarker")
        version_marker = data.get("NextVersionIdMarker")
        if not key_marker:
            return found


def purge_workspace(cfg, identity):
    """Delete every version/delete marker in SCH's three exact scopes."""
    bucket = checkpoint_bucket(cfg)
    all_objects = []
    for prefix in scopes(identity):
        all_objects.extend(_versions(cfg, bucket, prefix))
    for start in range(0, len(all_objects), 1000):
        batch = all_objects[start:start + 1000]
        payload = json.dumps({"Objects": batch, "Quiet": True})
        result = _aws(cfg, ["s3api", "delete-objects", "--bucket", bucket,
                            "--delete", payload, "--output", "json"])
        if result.returncode:
            raise DeletionError("S3 purge", "S3 batch deletion failed")
        try:
            errors = json.loads(result.stdout or "{}").get("Errors", [])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise DeletionError("S3 purge", "invalid S3 deletion response") from exc
        if errors:
            detail = ", ".join(str(e.get("Key", "unknown")) for e in errors)
            raise DeletionError("S3 purge", "S3 rejected objects: {}".format(detail))
    for prefix in scopes(identity):
        if _versions(cfg, bucket, prefix):
            raise DeletionError("S3 purge", "S3 verification found remaining objects")


def deletion_marker_path(cfg, workspace):
    return cfg.ws_dir / ".deleting.{}".format(workspace)


def read_deletion_marker(cfg, workspace):
    try:
        return json.loads(deletion_marker_path(cfg, workspace).read_text())
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def write_deletion_marker(cfg, workspace, state):
    path = deletion_marker_path(cfg, workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".sch-delete-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(state, handle, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def cleanup_local(cfg, workspace):
    """Remove only SCH-owned metadata and the default managed ACP mirror."""
    paths = [cfg.ws_dir / workspace, cfg.ws_dir / ".status.{}".format(workspace),
             deletion_marker_path(cfg, workspace), sync.binding_path(cfg, workspace),
             sync.baseline_path(cfg, workspace)]
    managed_root = Path(cfg.acp_mirror_root).expanduser().resolve()
    mirror = (managed_root / workspace).resolve()
    if managed_root == mirror or managed_root not in mirror.parents:
        raise DeletionError("local cleanup", "unsafe managed mirror path")
    paths.append(mirror)
    for path in paths:
        try:
            if path.is_dir() and not path.is_symlink():
                import shutil
                shutil.rmtree(str(path))
            else:
                path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise DeletionError("local cleanup", "cannot remove local metadata") from exc
