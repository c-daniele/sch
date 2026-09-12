"""Offline tests for the opencode.db backup WAL regression fix.

Regression (sch-fix-db-backup-wal): opencode >= 1.18 keeps its sqlite DB in
WAL journal mode; the sqlite backup API copies the WAL header into the
destination file, and re-opening an existing WAL-headered file through the
unix-dotfile VFS fails ("unable to open database file"). The old
_backup_db_once backed up in place, so it worked exactly once per file
lifetime and then failed forever — freezing the day-0 (empty) backup and
losing every session on the next microVM recycle.
"""

import importlib.util
import sqlite3
import sys
import tempfile
import types
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

spec = importlib.util.spec_from_file_location("sch_db_backup_main", app_dir / "main.py")
main = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = main
with patch("threading.Thread"):
    spec.loader.exec_module(main)


def make_wal_db(path: Path, rows: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS session (id TEXT PRIMARY KEY)")
    existing = conn.execute("SELECT COUNT(*) FROM session").fetchone()[0]
    for i in range(existing, rows):
        conn.execute("INSERT INTO session VALUES (?)", (f"ses_{i}",))
    conn.commit()
    conn.close()


def count_sessions(path: Path) -> int:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT COUNT(*) FROM session").fetchone()[0]
    finally:
        conn.close()


def setup(root: Path, backend: str) -> None:
    main.WORKSPACE_ROOT = root / "workspace"
    main.REPO_DIR = main.WORKSPACE_ROOT / "repo"
    main.STATE_DIR = main.WORKSPACE_ROOT / "state"
    main.DB_BACKUP_PATH = main.STATE_DIR / "data" / "opencode" / "opencode.db.backup"
    main.MOUNT_MARKER = main.STATE_DIR / ".sch-initialized"
    main.OPENCODE_DB_LOCAL = root / "local" / "opencode.db"
    main._STORAGE_BACKEND["value"] = backend
    main.BOOT_STATE["phase"] = "booting"
    main.BOOT_STATE.pop("restore_result", None)
    main.BOOT_STATE.pop("late_backup_handled", None)


# Guard the regression premise itself: sqlite cannot re-open an existing
# WAL-headered file through the unix-dotfile VFS (this is exactly why the old
# in-place backup froze after the first pass). If a future sqlite build lifts
# this restriction, this test tells us the workaround can be retired.
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    src_path = root / "src.db"
    make_wal_db(src_path, rows=1)
    backup_path = root / "backup.db"
    src = sqlite3.connect(str(src_path))
    dst = sqlite3.connect(f"file:{backup_path}?vfs=unix-dotfile", uri=True)
    src.backup(dst)
    dst.commit()
    dst.close()
    src.close()
    try:
        sqlite3.connect(f"file:{backup_path}?vfs=unix-dotfile", uri=True).execute(
            "SELECT 1"
        )
    except sqlite3.OperationalError:
        pass
    else:
        raise AssertionError(
            "expected re-open of WAL-headered file via unix-dotfile VFS to fail; "
            "regression premise no longer holds"
        )

print("Test 1 (premise: WAL header breaks dotfile-VFS re-open): PASS")


# The actual regression: repeated backups of a WAL-mode DB must ALL succeed
# and each must capture the latest content (storage=session, dotfile VFS).
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    setup(root, "session")
    make_wal_db(main.OPENCODE_DB_LOCAL, rows=2)

    assert main._backup_db_once() == "ok"
    assert count_sessions(main.DB_BACKUP_PATH) == 2

    make_wal_db(main.OPENCODE_DB_LOCAL, rows=6)
    assert main._backup_db_once() == "ok", "second pass must not fail (WAL regression)"
    assert count_sessions(main.DB_BACKUP_PATH) == 6

    make_wal_db(main.OPENCODE_DB_LOCAL, rows=9)
    assert main._backup_db_once() == "ok"
    assert count_sessions(main.DB_BACKUP_PATH) == 9
    assert not main._db_backup_tmp_path().exists()

print("Test 2 (repeated WAL backups all succeed, session backend): PASS")


# storage=s3: backup destination is local disk — default VFS, same guarantees.
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    setup(root, "s3")
    make_wal_db(main.OPENCODE_DB_LOCAL, rows=3)

    assert main._backup_db_once() == "ok"
    make_wal_db(main.OPENCODE_DB_LOCAL, rows=5)
    assert main._backup_db_once() == "ok"
    assert count_sessions(main.DB_BACKUP_PATH) == 5
    assert not main._db_backup_tmp_path().exists()

print("Test 3 (repeated WAL backups all succeed, s3 backend): PASS")


# A failed backup pass must leave the previous good backup intact (the temp
# staging + atomic rename must never expose a half-written file).
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    setup(root, "s3")
    make_wal_db(main.OPENCODE_DB_LOCAL, rows=4)
    assert main._backup_db_once() == "ok"
    good_bytes = main.DB_BACKUP_PATH.read_bytes()

    with patch.object(main.os, "replace", side_effect=OSError("disk full")):
        result = main._backup_db_once()
    assert result.startswith("error")
    assert main.DB_BACKUP_PATH.read_bytes() == good_bytes
    assert count_sessions(main.DB_BACKUP_PATH) == 4

print("Test 4 (failed pass preserves previous good backup): PASS")


# _do_checkpoint must surface a failed db backup as a degraded status
# (previously it reported "ok" while sessions were silently frozen), without
# vetoing the repo/state manifest commit.
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    setup(root, "s3")
    main.CHECKPOINT_BUCKET = "bucket"
    main.CHECKPOINT_TMP_DIR = root / "tmp"
    main.REPO_DIR.mkdir(parents=True)
    (main.REPO_DIR / "file.txt").write_text("repo")
    (main.STATE_DIR / "config").mkdir(parents=True)
    main._WORKSPACE_NAME["value"] = "ws"
    main._HARNESS["value"] = "opencode"
    main.CHECKPOINT_STATE["last_fingerprints"] = {}
    main.CHECKPOINT_STATE["last_sizes"] = {}
    main._WRITER_CLAIMED["workspace"] = None
    main._SESSION_EPOCH["value"] = 1
    published = []

    with patch.object(main, "_backup_db_once", return_value="error: unable to open database file"), \
            patch.object(main, "_download_manifest", return_value=None), \
            patch.object(main, "_upload_file_key", return_value=True), \
            patch.object(main, "_s3", return_value=type("S3", (), {"put_object_tagging": lambda *a, **k: None})()), \
            patch.object(main, "_upload_manifest", side_effect=lambda _ws, manifest, _etag=None: published.append(manifest) or True):
        result = main._do_checkpoint(force=True)

    assert result["status"] == "partial", result
    assert any(e.startswith("db-backup:") for e in result["errors"])
    assert result["manifest_written"] is True, "db failure must not veto repo/state commit"
    assert len(published) == 1

print("Test 5 (db backup failure degrades checkpoint status to partial): PASS")
print("test_db_backup_wal.py: ALL PASS")
