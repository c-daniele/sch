import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sch import deletion
from sch.commands import delete as delete_cmd


class DeletionPrimitiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = SimpleNamespace(
            config_dir=root / "config", ws_dir=root / "config" / "workspaces",
            acp_mirror_root=root / "managed-mirrors", region="eu-west-1",
            checkpoint_bucket_override="bucket",
            checkpoint_bucket_cache=root / "bucket-cache",
        )
        self.addCleanup(self.tmp.cleanup)

    def test_marker_is_atomic_and_cleanup_preserves_sync_source(self):
        self.cfg.ws_dir.mkdir(parents=True)
        source = Path(self.tmp.name) / "source"
        source.mkdir()
        deletion.write_deletion_marker(self.cfg, "ws", {
            "workspace": "ws", "sessionId": "sid", "identity": "ws",
            "storage": "session", "sessionEpoch": 4,
        })
        (self.cfg.ws_dir / "ws").write_text("index")
        (self.cfg.ws_dir / ".status.ws").write_text("stopped")
        (self.cfg.acp_mirror_root / "ws" / "repo").mkdir(parents=True)
        deletion.cleanup_local(self.cfg, "ws")
        self.assertFalse((self.cfg.ws_dir / "ws").exists())
        self.assertFalse((self.cfg.acp_mirror_root / "ws").exists())
        self.assertTrue(source.exists())

    @mock.patch("sch.deletion.checkpoint_bucket", return_value="bucket")
    @mock.patch("sch.deletion._aws")
    def test_purge_deletes_versions_markers_and_exact_writer(self, aws, _bucket):
        listing = json.dumps({
            "Versions": [{"Key": "checkpoints/ws/a", "VersionId": "1"},
                         {"Key": "checkpoints/ws-other/a", "VersionId": "2"}],
            "DeleteMarkers": [{"Key": "workspace-writers/ws.json", "VersionId": "3"}],
            "IsTruncated": False,
        })
        aws.side_effect = [
            SimpleNamespace(returncode=0, stdout=listing),
            SimpleNamespace(returncode=0, stdout=json.dumps({})),
            SimpleNamespace(returncode=0, stdout=json.dumps({
                "DeleteMarkers": [{"Key": "workspace-writers/ws.json", "VersionId": "3"}],
                "IsTruncated": False,
            })),
            SimpleNamespace(returncode=0, stdout=json.dumps({"Errors": []})),
            SimpleNamespace(returncode=0, stdout=json.dumps({})),
            SimpleNamespace(returncode=0, stdout=json.dumps({})),
            SimpleNamespace(returncode=0, stdout=json.dumps({})),
        ]
        deletion.purge_workspace(self.cfg, "ws")
        delete_call = aws.call_args_list[3]
        payload = json.loads(delete_call.args[1][delete_call.args[1].index("--delete") + 1])
        self.assertEqual(payload["Objects"], [
            {"Key": "checkpoints/ws/a", "VersionId": "1"},
            {"Key": "workspace-writers/ws.json", "VersionId": "3"},
        ])


class DeleteCliTests(unittest.TestCase):
    def test_confirmation_cancel_has_no_output_on_stdout(self):
        with mock.patch("builtins.input", return_value="no"), contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(delete_cmd._confirm("ws", ["ws"], False), False)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("cancelled", err.getvalue())

    def test_forms_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            delete_cmd._parse(["ws", "--all"])
        with self.assertRaises(SystemExit):
            delete_cmd._parse([])

    def test_yes_single_deletion_keeps_stdout_empty(self):
        cfg = SimpleNamespace(workspace_registry_url="", ws_dir=Path("/tmp/no-such-sch-delete"))
        with mock.patch("sch.commands.delete._delete_local") as run, \
                mock.patch("sch.commands.delete._targets", return_value=["ws"]), \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(delete_cmd.cmd_delete(cfg, ["ws", "--yes"]), 0)
        run.assert_called_once_with(cfg, "ws")
        self.assertEqual(out.getvalue(), "")

    def test_bulk_continues_after_phase_failure_and_sorts_targets(self):
        cfg = SimpleNamespace(workspace_registry_url="", ws_dir=Path("/tmp/no-such-sch-delete"))
        def delete_one(_cfg, name):
            if name == "a":
                raise deletion.DeletionError("S3 purge", "injected failure")
        with mock.patch("sch.commands.delete._delete_local", side_effect=delete_one), \
                mock.patch("sch.commands.delete._targets", return_value=["b", "a"]), \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(delete_cmd.cmd_delete(cfg, ["--all", "--yes"]), 1)
        self.assertEqual(out.getvalue(), "")
        text = err.getvalue()
        self.assertLess(text.index("a: S3 purge"), text.index("b: deleted"))
        self.assertIn("retry failed targets: a", text)


if __name__ == "__main__":
    unittest.main()
