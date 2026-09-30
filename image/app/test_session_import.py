"""Offline tests for the OpenCode session-import shim action."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_git_native import load_main

main = load_main()


SESSION_TABLE = main.OPENCODE_SESSION_TABLE


def create_db(path, rows=()):
    # OpenCode 2 schema subset: `session_v2` with the columns the shim reads.
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(path)) as conn:
        conn.execute(
            f"CREATE TABLE {SESSION_TABLE} (id TEXT PRIMARY KEY, directory TEXT, "
            "parent_id TEXT, time_updated INTEGER, title TEXT, model TEXT)"
        )
        conn.executemany(
            f"INSERT INTO {SESSION_TABLE} (id, directory, time_updated, title) VALUES (?, ?, ?, ?)",
            rows,
        )


class SessionImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        main.ACTIVE_WORKSPACE_FILE = root / "active-workspace.json"
        main.SESSION_WORKSPACE_ROOT = root / "session-workspace"
        main.S3_WORKSPACE_ROOT = root / "s3-workspace"
        self.repo = root / "repo"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()
        self.staging = root / "state" / "bundles"
        self.staging.mkdir(parents=True)
        main.REPO_DIR = self.repo
        main.BUNDLE_STAGING_DIR = self.staging
        main.OPENCODE_DB_LOCAL = root / "opencode.db"
        main.DB_BACKUP_PATH = root / "backup.db"
        main._WORKSPACE_READY.set()
        main._WORKSPACE_NAME["value"] = "ws"
        main._HARNESS["value"] = "opencode"
        main._STORAGE_BACKEND["value"] = "session"
        main._SESSION_EPOCH["value"] = 0
        main._SESSION_ID["value"] = "sid"
        main.CHECKPOINT_BUCKET = ""
        create_db(main.OPENCODE_DB_LOCAL)

    def stage(self, session_id="ses_test"):
        path = self.staging / main.HANDOFF_FILE_NAME
        path.write_text(json.dumps({"info": {"id": session_id}, "messages": []}))
        return path

    def fake_import(self, session_id="ses_test", updated=10):
        calls = []

        def run(argv, **_kwargs):
            calls.append(argv)
            with sqlite3.connect(str(main.OPENCODE_DB_LOCAL)) as conn:
                conn.execute(
                    f"INSERT OR REPLACE INTO {SESSION_TABLE} (id, directory, time_updated, title) VALUES (?, ?, ?, ?)",
                    (session_id, str(self.repo), updated, "imported"),
                )
            return type("P", (), {"returncode": 0, "stdout": "Imported session: {}\n".format(session_id), "stderr": ""})()
        run.calls = calls
        return run

    def test_missing_staged_file(self):
        response = main._handle_session_import({})
        self.assertEqual(response["status"], "error")
        self.assertIn("not found", response["error"])

    def test_success_imports_backs_up_and_cleans(self):
        staged = self.stage()
        fake = self.fake_import()
        with patch.object(main, "_opencode_version", return_value="2.0.18"), \
             patch.object(main.shutil, "which", return_value="/bin/opencode"), \
             patch.object(main.subprocess, "run", side_effect=fake), \
             patch.object(main, "_repo_empty_or_unseeded", return_value=False):
            response = main._handle_session_import({})
        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["sessionID"], "ses_test")
        self.assertEqual(response["opencodeVersion"], "2.0.18")
        self.assertFalse(response["reimported"])
        self.assertFalse(staged.exists())
        # OpenCode 2 argv: `session import`, standalone, bound to the worktree.
        self.assertEqual(
            fake.calls[0],
            ["/bin/opencode", "session", "import", "--standalone",
             "--directory", str(self.repo), str(staged)],
        )
        with sqlite3.connect(str(main.DB_BACKUP_PATH)) as conn:
            self.assertEqual(
                conn.execute(f"SELECT id FROM {SESSION_TABLE}").fetchone()[0], "ses_test"
            )

    def test_reimport_and_recency(self):
        with sqlite3.connect(str(main.OPENCODE_DB_LOCAL)) as conn:
            conn.executemany(
                f"INSERT INTO {SESSION_TABLE} (id, directory, time_updated, title) VALUES (?, ?, ?, ?)",
                [
                    ("ses_test", str(self.repo), 1, "old"),
                    ("ses_other", str(self.repo), 100, "other"),
                ],
            )
        self.stage()
        with patch.object(main, "_opencode_version", return_value="2.0.18"), \
             patch.object(main.shutil, "which", return_value="/bin/opencode"), \
             patch.object(main.subprocess, "run", side_effect=self.fake_import(updated=2)), \
             patch.object(main, "_repo_empty_or_unseeded", return_value=False):
            response = main._handle_session_import({})
        self.assertTrue(response["reimported"])
        self.assertEqual(main._resolve_latest_opencode_session(), "ses_test")

    def test_latest_session_skips_child_sessions(self):
        # Subagent sessions carry parent_id: never resumed as "the latest".
        with sqlite3.connect(str(main.OPENCODE_DB_LOCAL)) as conn:
            conn.execute(
                f"INSERT INTO {SESSION_TABLE} (id, directory, parent_id, time_updated, title) VALUES (?, ?, ?, ?, ?)",
                ("ses_parent", str(self.repo), None, 10, "parent"),
            )
            conn.execute(
                f"INSERT INTO {SESSION_TABLE} (id, directory, parent_id, time_updated, title) VALUES (?, ?, ?, ?, ?)",
                ("ses_child", str(self.repo), "ses_parent", 999, "child"),
            )
        self.assertEqual(main._resolve_latest_opencode_session(), "ses_parent")

    def test_import_failure_preserves_file_and_version(self):
        staged = self.stage()
        failed = type("P", (), {"returncode": 2, "stdout": "", "stderr": "bad format"})()
        with patch.object(main, "_opencode_version", return_value="2.0.18"), \
             patch.object(main.shutil, "which", return_value="/bin/opencode"), \
             patch.object(main.subprocess, "run", return_value=failed):
            response = main._handle_session_import({})
        self.assertEqual(response["opencodeVersion"], "2.0.18")
        self.assertIn("bad format", response["error"])
        self.assertTrue(staged.exists())

    def test_backup_failure_preserves_file(self):
        staged = self.stage()
        with patch.object(main, "_opencode_version", return_value="2.0.18"), \
             patch.object(main.shutil, "which", return_value="/bin/opencode"), \
             patch.object(main.subprocess, "run", side_effect=self.fake_import()), \
             patch.object(main, "_backup_db_durable", return_value={"status": "error", "db_backup": "boom"}):
            response = main._handle_session_import({})
        self.assertEqual(response["status"], "error")
        self.assertTrue(staged.exists())

    def test_fresh_s3_workspace_bootstraps_durable_manifest(self):
        main._STORAGE_BACKEND["value"] = "s3"
        main.CHECKPOINT_BUCKET = "bucket"
        initial = {"status": "ok", "manifest_written": True}
        with patch.object(main, "_backup_db_once", return_value="ok"), \
             patch.object(main, "_download_manifest", return_value={"published": False}), \
             patch.object(main, "_do_checkpoint", return_value=initial) as checkpoint:
            response = main._backup_db_durable()
        self.assertEqual(response, {"status": "ok", "db_backup": "ok", "bootstrap": True})
        checkpoint.assert_called_once_with(force=True)

    def test_fresh_s3_workspace_rejects_partial_base_checkpoint(self):
        main._STORAGE_BACKEND["value"] = "s3"
        main.CHECKPOINT_BUCKET = "bucket"
        initial = {"status": "partial", "manifest_written": True, "errors": ["state-upload"]}
        with patch.object(main, "_backup_db_once", return_value="ok"), \
             patch.object(main, "_download_manifest", return_value=None), \
             patch.object(main, "_do_checkpoint", return_value=initial):
            response = main._backup_db_durable()
        self.assertEqual(response["status"], "error")
        self.assertIn("state-upload", response["db_backup"])

    def test_existing_s3_manifest_keeps_db_only_path(self):
        main._STORAGE_BACKEND["value"] = "s3"
        main.CHECKPOINT_BUCKET = "bucket"
        previous = {
            "published": True,
            "_etag": "etag",
            "artifacts": {
                "repo.tar.gz": "repo-key",
                "state.tar.gz": "state-key",
            },
        }
        s3 = type("S3", (), {"put_object_tagging": lambda *args, **kwargs: None})()
        def backup():
            main.DB_BACKUP_PATH.write_bytes(b"db")
            return "ok"

        with patch.object(main, "_backup_db_once", side_effect=backup), \
             patch.object(main, "_download_manifest", return_value=previous), \
             patch.object(main, "_upload_file_key", return_value=True), \
             patch.object(main, "_upload_manifest", return_value=True), \
             patch.object(main, "_s3", return_value=s3), \
             patch.object(main, "_do_checkpoint") as checkpoint:
            response = main._backup_db_durable()
        self.assertEqual(response["status"], "ok")
        self.assertIn("artifact", response)
        checkpoint.assert_not_called()

    def test_imported_provider_uses_runtime_default_model_on_resume(self):
        main.OPENCODE_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        main.OPENCODE_CONFIG_FILE.write_text(json.dumps({
            "model": "amazon-bedrock/remote-default",
            "provider": {"amazon-bedrock": {"options": {}}},
        }))
        with sqlite3.connect(str(main.OPENCODE_DB_LOCAL)) as conn:
            conn.execute(
                f"INSERT INTO {SESSION_TABLE} (id, directory, time_updated, title, model) VALUES (?, ?, ?, ?, ?)",
                ("ses_imported", str(self.repo), 1, "imported", json.dumps({
                    "id": "gpt-5.6-sol", "providerID": "github-copilot",
                })),
            )
        self.assertEqual(
            main._opencode_resume_model("ses_imported"),
            "amazon-bedrock/remote-default",
        )

    def test_remote_provider_keeps_session_model_implicit(self):
        main.OPENCODE_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        main.OPENCODE_CONFIG_FILE.write_text(json.dumps({
            "model": "amazon-bedrock/remote-default",
            "provider": {"amazon-bedrock": {"options": {}}},
        }))
        with sqlite3.connect(str(main.OPENCODE_DB_LOCAL)) as conn:
            conn.execute(
                f"INSERT INTO {SESSION_TABLE} (id, directory, time_updated, title, model) VALUES (?, ?, ?, ?, ?)",
                ("ses_remote", str(self.repo), 1, "remote", json.dumps({
                    "id": "another-model", "providerID": "amazon-bedrock",
                })),
            )
        self.assertIsNone(main._opencode_resume_model("ses_remote"))

    def test_dispatch_and_supported_list(self):
        main._STORAGE_BACKEND["value"] = None
        with patch.object(main, "_handle_session_import", return_value={"status": "ok"}) as handler:
            self.assertEqual(main.invoke({"action": "session-import", "workspace": "ws", "storage_backend": "session", "session_epoch": 0})["status"], "ok")
            handler.assert_called_once()
        response = main.invoke({"action": "unknown-thing", "workspace": "ws"})
        self.assertIn("session-import", response["message"])


if __name__ == "__main__":
    unittest.main()
