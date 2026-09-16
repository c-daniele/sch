"""Offline tests for selectable workspace roots and S3 generations."""

import importlib.util
import json
import shutil
import sys
import tarfile
import tempfile
import types
from io import BytesIO
from pathlib import Path
from unittest.mock import patch


class FakeApp:
    def entrypoint(self, func):
        return func

    def websocket(self, func):
        return func

    def add_async_task(self, *_args, **_kwargs):
        return object()

    def complete_async_task(self, *_args, **_kwargs):
        return None

    def run(self, *_args, **_kwargs):
        return None


app_dir = Path(__file__).parent
sys.path.insert(0, str(app_dir))
bedrock = types.ModuleType("bedrock_agentcore")
bedrock.BedrockAgentCoreApp = FakeApp
sys.modules["bedrock_agentcore"] = bedrock
boto3 = types.ModuleType("boto3")
boto3.client = lambda *_args, **_kwargs: None
sys.modules["boto3"] = boto3

spec = importlib.util.spec_from_file_location("sch_storage_main", app_dir / "main.py")
main = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = main
with patch("threading.Thread"):
    spec.loader.exec_module(main)


class MissingObject(Exception):
    def __init__(self):
        self.response = {"Error": {"Code": "NoSuchKey"}}


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.etags = {}
        self.counter = 0
        self.tags = {}

    def get_object(self, *, Key, **_kwargs):
        if Key not in self.objects:
            raise MissingObject()
        return {"Body": BytesIO(self.objects[Key]), "ETag": self.etags[Key]}

    def put_object(self, *, Key, Body, IfMatch=None, IfNoneMatch=None, **_kwargs):
        if IfNoneMatch == "*" and Key in self.objects:
            raise RuntimeError("precondition failed")
        if IfMatch is not None and self.etags.get(Key) != IfMatch:
            raise RuntimeError("precondition failed")
        self.counter += 1
        self.objects[Key] = Body
        self.etags[Key] = f'"etag-{self.counter}"'
        return {"ETag": self.etags[Key]}

    def put_object_tagging(self, *, Key, Tagging, **_kwargs):
        self.tags[Key] = Tagging["TagSet"]


with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    main.ACTIVE_WORKSPACE_FILE = root / "active.json"
    main.S3_WORKSPACE_ROOT = root / "s3-root"
    main.SESSION_WORKSPACE_ROOT = root / "session-root"
    main._STORAGE_BACKEND["value"] = None
    assert main._set_storage_backend("s3")
    assert main.WORKSPACE_ROOT == main.S3_WORKSPACE_ROOT
    assert main.REPO_DIR == main.S3_WORKSPACE_ROOT / "repo"
    assert main.OPENCODE_AUTH_FILE == (
        main.S3_WORKSPACE_ROOT / "state" / "data" / "opencode" / "auth.json"
    )
    assert json.loads(main.ACTIVE_WORKSPACE_FILE.read_text())["storage"] == "s3"
    assert not main._set_storage_backend("session")

print("Test 1 (backend root selection + immutability): PASS")


with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    main.CHECKPOINT_BUCKET = "bucket"
    main.CHECKPOINT_TMP_DIR = root / "tmp"
    main.WORKSPACE_ROOT = root / "workspace"
    main.REPO_DIR = main.WORKSPACE_ROOT / "repo"
    main.STATE_DIR = main.WORKSPACE_ROOT / "state"
    main.DB_BACKUP_PATH = main.STATE_DIR / "data" / "opencode" / "opencode.db.backup"
    main.REPO_DIR.mkdir(parents=True)
    (main.REPO_DIR / "file.txt").write_text("repo")
    (main.STATE_DIR / "config").mkdir(parents=True)
    (main.STATE_DIR / "config" / "settings.json").write_text("{}")
    main.DB_BACKUP_PATH.parent.mkdir(parents=True, exist_ok=True)
    main.DB_BACKUP_PATH.write_bytes(b"db")
    main._WORKSPACE_NAME["value"] = "ws"
    main._HARNESS["value"] = "opencode"
    main._STORAGE_BACKEND["value"] = "s3"
    main.CHECKPOINT_STATE["last_fingerprints"] = {}
    main.CHECKPOINT_STATE["last_sizes"] = {}
    uploaded = []
    published = []
    fake_s3 = FakeS3()
    main._WRITER_CLAIMED["workspace"] = None
    main._SESSION_EPOCH["value"] = 1

    def upload(path, key):
        uploaded.append((Path(path).name, key))
        return True

    with patch.object(main, "_backup_db_once", return_value="ok"), \
            patch.object(main, "_download_manifest", return_value=None), \
            patch.object(main, "_upload_file_key", side_effect=upload), \
            patch.object(main, "_s3", return_value=fake_s3), \
            patch.object(main, "_upload_manifest", side_effect=lambda _ws, manifest, _etag=None: published.append(manifest) or True):
        result = main._do_checkpoint(force=True)

    assert result["status"] == "ok"
    assert result["manifest_written"] is True
    assert len(published) == 1
    manifest = published[0]
    assert manifest["storage"] == "s3"
    assert set(manifest["artifacts"]) == {"repo.tar.gz", "state.tar.gz", "opencode.db.backup"}
    assert all(key.startswith("checkpoint-generations/ws/") for _, key in uploaded)

print("Test 2 (generation uploaded before manifest publication): PASS")


with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    main.CHECKPOINT_TMP_DIR = root / "tmp"
    main.WORKSPACE_ROOT = root / "workspace"
    main.REPO_DIR = main.WORKSPACE_ROOT / "repo"
    main.STATE_DIR = main.WORKSPACE_ROOT / "state"
    main.DB_BACKUP_PATH = main.STATE_DIR / "data" / "opencode" / "opencode.db.backup"
    main.REPO_DIR.mkdir(parents=True)
    (main.REPO_DIR / "file.txt").write_text("repo")
    (main.STATE_DIR / "config").mkdir(parents=True)
    main.DB_BACKUP_PATH.parent.mkdir(parents=True, exist_ok=True)
    main.DB_BACKUP_PATH.write_bytes(b"db")
    main.CHECKPOINT_STATE["last_fingerprints"] = {}
    main.CHECKPOINT_STATE["last_sizes"] = {}
    published = []
    attempts = [0]
    fake_s3 = FakeS3()
    main._WRITER_CLAIMED["workspace"] = None

    def fail_second(_path, _key):
        attempts[0] += 1
        return attempts[0] != 2

    with patch.object(main, "_backup_db_once", return_value="ok"), \
            patch.object(main, "_download_manifest", return_value=None), \
            patch.object(main, "_upload_file_key", side_effect=fail_second), \
            patch.object(main, "_s3", return_value=fake_s3), \
            patch.object(main, "_upload_manifest", side_effect=lambda _ws, manifest, _etag=None: published.append(manifest) or True):
        result = main._do_checkpoint(force=True)

    assert result["status"] == "partial"
    assert result["manifest_written"] is False
    assert published == []

print("Test 3 (failed artifact does not advance manifest): PASS")


with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    source = root / "source"
    destination = root / "destination"
    (source / "repo").mkdir(parents=True)
    (source / "repo" / "remote.txt").write_text("remote")
    (source / "state" / "config").mkdir(parents=True)
    (source / "state" / "config" / "settings.json").write_text("{}")
    archives = {}
    for name, folder in (("repo.tar.gz", "repo"), ("state.tar.gz", "state")):
        archive = root / name
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(source / folder, arcname=folder)
        archives[f"key/{name}"] = archive
    db_source = root / "db.backup"
    db_source.write_bytes(b"db")
    archives["key/opencode.db.backup"] = db_source
    manifest = {
        "storage": "s3",
        "harness": "opencode",
        "artifacts": {
            "repo.tar.gz": "key/repo.tar.gz",
            "state.tar.gz": "key/state.tar.gz",
            "opencode.db.backup": "key/opencode.db.backup",
        },
    }
    main.WORKSPACE_ROOT = destination
    main.REPO_DIR = destination / "repo"
    main.STATE_DIR = destination / "state"
    main.DB_BACKUP_PATH = destination / "state" / "data" / "opencode" / "opencode.db.backup"
    main.CHECKPOINT_TMP_DIR = root / "restore-tmp"
    main._HARNESS["value"] = "opencode"
    main._STORAGE_BACKEND["value"] = "s3"

    def download(key, dest):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(archives[key], dest)
        return True

    with patch.object(main, "_download_manifest", return_value=manifest), \
            patch.object(main, "_download_key", side_effect=download):
        result = main._restore_l2("ws")

    assert result["result"] == "downloaded"
    assert (main.REPO_DIR / "remote.txt").read_text() == "remote"
    assert (main.STATE_DIR / "config" / "settings.json").is_file()
    assert main.DB_BACKUP_PATH.read_bytes() == b"db"

print("Test 4 (published generation restores directly to active root): PASS")


with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    tree = root / "tree"
    tree.mkdir()
    target = tree / "same.txt"
    target.write_text("before")
    old_mtime = target.stat().st_mtime_ns
    before = main._tree_fingerprint(tree)
    target.write_text("after!")
    target.touch()
    import os
    os.utime(target, ns=(old_mtime, old_mtime))
    after = main._tree_fingerprint(tree)
    assert before != after
    target.unlink()
    deleted = main._tree_fingerprint(tree)
    assert deleted != after

print("Test 5 (content/deletion fingerprint ignores preserved mtimes): PASS")


with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    main.WORKSPACE_ROOT = root / "active"
    main.REPO_DIR = main.WORKSPACE_ROOT / "repo"
    main.STATE_DIR = main.WORKSPACE_ROOT / "state"
    main.DB_BACKUP_PATH = main.STATE_DIR / "data" / "opencode" / "opencode.db.backup"
    main.CHECKPOINT_TMP_DIR = root / "tmp"
    main._STORAGE_BACKEND["value"] = "s3"
    manifest = {
        "storage": "s3", "harness": "opencode",
        "artifacts": {
            "repo.tar.gz": "key/repo.tar.gz",
            "state.tar.gz": "key/state.tar.gz",
        },
    }
    with patch.object(main, "_download_manifest", return_value=manifest), \
            patch.object(main, "_download_key", return_value=False):
        result = main._restore_l2("ws")
    assert result["result"] == "error"
    assert not main.WORKSPACE_ROOT.exists()

print("Test 6 (failed restore never promotes partial active root): PASS")


fake_s3 = FakeS3()
main.CHECKPOINT_BUCKET = "bucket"
main._SESSION_EPOCH["value"] = 2
main._WRITER_CLAIMED["workspace"] = None
with patch.object(main, "_s3", return_value=fake_s3):
    main._claim_writer("fenced")
    main._WRITER_CLAIMED["workspace"] = None
    original_token = main._WRITER_TOKEN
    try:
        main._WRITER_TOKEN = "older-writer"
        main._SESSION_EPOCH["value"] = 1
        try:
            main._claim_writer("fenced")
        except main.WriterFenceError:
            pass
        else:
            raise AssertionError("older writer epoch must be fenced")
    finally:
        main._WRITER_TOKEN = original_token

print("Test 7 (newer session epoch fences stale microVM): PASS")


with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    main.REPO_DIR = root / "repo"
    main.STATE_DIR = root / "state"
    main.DB_BACKUP_PATH = main.STATE_DIR / "data" / "opencode" / "opencode.db.backup"
    main.REPO_DIR.mkdir()
    (main.REPO_DIR / "file.txt").write_text("data")
    (main.STATE_DIR / "config").mkdir(parents=True)
    main.DB_BACKUP_PATH.parent.mkdir(parents=True, exist_ok=True)
    main.DB_BACKUP_PATH.write_bytes(b"db")
    main.CHECKPOINT_TMP_DIR = root / "tmp"
    with patch.object(main, "_backup_db_once", return_value="ok"), \
            patch.object(main, "_download_manifest", side_effect=main.ManifestReadError("denied")):
        result = main._do_checkpoint(force=True)
    assert result["status"] == "error"
    assert result["manifest_written"] is False

print("Test 8 (manifest read failure aborts checkpoint publication): PASS")


with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    main.CHECKPOINT_BUCKET = "bucket"
    main.CHECKPOINT_TMP_DIR = root / "tmp"
    main.WORKSPACE_ROOT = root / "workspace"
    main.REPO_DIR = main.WORKSPACE_ROOT / "repo"
    main.STATE_DIR = main.WORKSPACE_ROOT / "state"
    main.DB_BACKUP_PATH = main.STATE_DIR / "data" / "opencode" / "opencode.db.backup"
    main.PI_CONFIG_DIR_LOCAL = root / "pi-local"
    main.PI_READY_MARKER = main.PI_CONFIG_DIR_LOCAL / ".ready"
    main.PI_STATE_REPLICA = main.STATE_DIR / "pi"
    main.REPO_DIR.mkdir(parents=True)
    (main.REPO_DIR / "file.txt").write_text("repo")
    (main.STATE_DIR / "config").mkdir(parents=True)
    main.DB_BACKUP_PATH.parent.mkdir(parents=True, exist_ok=True)
    main.DB_BACKUP_PATH.write_bytes(b"db")
    (main.PI_CONFIG_DIR_LOCAL / "sessions").mkdir(parents=True)
    (main.PI_CONFIG_DIR_LOCAL / "sessions" / "session.jsonl").write_text("session")
    main.PI_READY_MARKER.touch()
    main._WORKSPACE_NAME["value"] = "pi-ws"
    main._HARNESS["value"] = "pi"
    main._WRITER_TOKEN = "pi-writer"
    main._STORAGE_BACKEND["value"] = "s3"
    main.CHECKPOINT_STATE["last_fingerprints"] = {}
    main.CHECKPOINT_STATE["last_sizes"] = {}
    main._WRITER_CLAIMED["workspace"] = None
    main._SESSION_EPOCH["value"] = 1
    published = []
    fake_s3 = FakeS3()

    with patch.object(main, "_backup_db_once", return_value="ok"), \
            patch.object(main, "_download_manifest", return_value=None), \
            patch.object(main, "_upload_file_key", return_value=True), \
            patch.object(main, "_s3", return_value=fake_s3), \
            patch.object(main, "_upload_manifest", side_effect=lambda _ws, manifest, _etag=None: published.append(manifest) or True):
        result = main._do_checkpoint(force=True)

    assert result["status"] == "ok"
    assert set(published[0]["artifacts"]) == {
        "repo.tar.gz", "state.tar.gz", "opencode.db.backup", "pi.tar.gz",
    }
    assert "claude.tar.gz" not in published[0]["artifacts"]
    assert not (main.PI_STATE_REPLICA / ".ready").exists()
    assert (main.PI_STATE_REPLICA / "sessions" / "session.jsonl").is_file()

print("Test 9 (pi checkpoint includes pi state but never readiness): PASS")


with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    source = root / "source"
    destination = root / "destination"
    (source / "repo").mkdir(parents=True)
    (source / "repo" / "remote.txt").write_text("remote")
    (source / "state" / "config").mkdir(parents=True)
    (source / "state" / "config" / "settings.json").write_text("{}")
    (source / "state" / "pi" / "sessions").mkdir(parents=True)
    (source / "state" / "pi" / "settings.json").write_text('{"defaultProvider":"amazon-bedrock"}')
    (source / "state" / "pi" / "sessions" / "restored.jsonl").write_text("session")
    # Simulate a checkpoint produced before readiness was excluded.
    (source / "state" / "pi" / ".ready").touch()
    archives = {}
    for name, folder in (
        ("repo.tar.gz", "repo"),
        ("state.tar.gz", "state"),
        ("pi.tar.gz", "state/pi"),
    ):
        archive = root / name
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(source / folder, arcname=Path(folder).name)
        archives[f"key/{name}"] = archive
    manifest = {
        "storage": "s3",
        "harness": "pi",
        "artifacts": {
            "repo.tar.gz": "key/repo.tar.gz",
            "state.tar.gz": "key/state.tar.gz",
            "pi.tar.gz": "key/pi.tar.gz",
        },
    }
    main.WORKSPACE_ROOT = destination
    main.REPO_DIR = destination / "repo"
    main.STATE_DIR = destination / "state"
    main.DB_BACKUP_PATH = destination / "state" / "data" / "opencode" / "opencode.db.backup"
    main.PI_STATE_REPLICA = destination / "state" / "pi"
    main.PI_CONFIG_DIR_LOCAL = root / "pi-local"
    main.PI_READY_MARKER = main.PI_CONFIG_DIR_LOCAL / ".ready"
    main.CHECKPOINT_TMP_DIR = root / "restore-tmp"
    main._HARNESS["value"] = "pi"
    main._STORAGE_BACKEND["value"] = "s3"

    def download_pi(key, dest):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(archives[key], dest)
        return True

    with patch.object(main, "_download_manifest", return_value=manifest), \
            patch.object(main, "_download_key", side_effect=download_pi):
        result = main._restore_l2("pi-ws")

    assert result["result"] == "downloaded"
    assert (main.PI_CONFIG_DIR_LOCAL / "sessions" / "restored.jsonl").is_file()
    assert not main.PI_READY_MARKER.exists()
    assert not (main.PI_STATE_REPLICA / ".ready").exists()

print("Test 10 (pi restore restores sessions without premature readiness): PASS")
print("test_workspace_storage_backend.py: ALL PASS")
