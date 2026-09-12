"""Tests for `sch list --remote-check` (TASK-37)."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_CLI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _CLI_DIR not in sys.path:
    sys.path.insert(0, _CLI_DIR)

from sch import workspace
from sch.commands import list as list_cmd


def _cfg(tmp_dir):
    root = Path(tmp_dir)
    return SimpleNamespace(
        ws_dir=root / "workspaces",
        config_dir=root,
        checkpoint_bucket_cache=root / "bucket",
        checkpoint_bucket_override="test-bucket",
        region="eu-west-1",
        workspace_registry_url="",
    )


def _record(name, sid="sid-x", epoch=1, identity=""):
    return list_cmd.WorkspaceRecord(
        name=name, harness="opencode", storage="s3", sid=sid,
        status="created", runtime_workspace=name,
        identity=identity or name, epoch=epoch,
    )


class RemoteCheckFlagTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = _cfg(self.tmp.name)

    def _run(self, args, records):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(list_cmd, "read_workspace_records",
                          return_value=tuple(records)), \
             patch.object(list_cmd, "_run_remote_check",
                          return_value=0) as remote, \
             patch.object(list_cmd.subprocess, "run",
                          side_effect=AssertionError("S3 touched")):
            with contextlib.redirect_stdout(out), \
                 contextlib.redirect_stderr(err):
                code = list_cmd.cmd_list(self.cfg, args)
        return code, out.getvalue(), err.getvalue(), remote

    def test_default_output_has_no_remote_section_and_no_s3(self):
        records = [_record("alpha")]
        code, default_out, _, remote = self._run([], records)
        self.assertEqual(code, 0)
        remote.assert_not_called()
        self.assertNotIn("remote check", default_out)
        self.assertIn("alpha", default_out)

    def test_remote_flag_runs_check_after_default_rendering(self):
        records = [_record("alpha")]
        code, out, _, remote = self._run(["--remote-check"], records)
        self.assertEqual(code, 0)
        remote.assert_called_once()

    def test_unknown_option_dies(self):
        with patch.object(list_cmd, "read_workspace_records",
                          return_value=()):
            with self.assertRaises(SystemExit):
                with contextlib.redirect_stderr(io.StringIO()):
                    list_cmd.cmd_list(self.cfg, ["--all"])

    def test_remote_failure_degrades_to_warning(self):
        workspace.save_workspace_state(
            self.cfg, "alpha", "sid-a", "opencode", "alpha", "s3", 1)
        out, err = io.StringIO(), io.StringIO()
        with patch.object(list_cmd, "remote_orphan_report",
                          side_effect=list_cmd.RemoteCheckError("boom")):
            with contextlib.redirect_stdout(out), \
                 contextlib.redirect_stderr(err):
                code = list_cmd.cmd_list(self.cfg, ["--remote-check"])
        self.assertEqual(code, 0)
        self.assertIn("alpha", out.getvalue())
        self.assertIn("remote check unavailable", err.getvalue())

    def test_bucket_resolution_failure_degrades(self):
        workspace.save_workspace_state(
            self.cfg, "alpha", "sid-a", "opencode", "alpha", "s3", 1)
        out, err = io.StringIO(), io.StringIO()
        with patch.object(list_cmd, "remote_orphan_report",
                          side_effect=SystemExit(1)):
            with contextlib.redirect_stdout(out), \
                 contextlib.redirect_stderr(err):
                code = list_cmd.cmd_list(self.cfg, ["--remote-check"])
        self.assertEqual(code, 0)
        self.assertIn("remote check unavailable", err.getvalue())


def _s3_list(payload):
    return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")


class RemoteOrphanReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = _cfg(self.tmp.name)

    def _report(self, records, writers, checkpoints, claims=None):
        claims = claims or {}

        def fake_s3(cfg, args):
            if "--continuation-token" in args:
                raise AssertionError("unexpected pagination")
            if "workspace-writers/" in args:
                return _s3_list({
                    "Contents": [{"Key": "workspace-writers/{}.json".format(w)}
                                 for w in writers],
                    "IsTruncated": False,
                })
            return _s3_list({
                "CommonPrefixes": [{"Prefix": "checkpoints/{}/".format(c)}
                                   for c in checkpoints],
                "IsTruncated": False,
            })

        with patch.object(list_cmd, "_s3", side_effect=fake_s3), \
             patch.object(list_cmd, "_read_writer_claim",
                          side_effect=lambda cfg, bucket, ident:
                          claims.get(ident)):
            return list_cmd.remote_orphan_report(self.cfg, records)

    def test_clean_remote_reports_no_lines(self):
        lines = self._report(
            [_record("alpha", sid="sid-a", epoch=1)],
            ["alpha"], ["alpha"],
            {"alpha": {"session_epoch": 1, "session_id": "sid-a"}})
        self.assertEqual(lines, [])

    def test_writer_orphan_names_delete_remedy(self):
        lines = self._report([_record("alpha")], ["alpha", "ghost"],
                             ["alpha", "ghost"])
        self.assertEqual(len(lines), 1)
        self.assertIn("remote orphan", lines[0])
        self.assertIn("ghost", lines[0])
        self.assertIn("sch delete", lines[0])

    def test_checkpoint_only_orphan_reported(self):
        lines = self._report([_record("alpha")], ["alpha"],
                             ["alpha", "stale"])
        self.assertEqual(len(lines), 1)
        self.assertIn("remote orphan", lines[0])
        self.assertIn("stale", lines[0])

    def test_epoch_collision_names_both_remedies(self):
        lines = self._report(
            [_record("alpha", sid="sid-a", epoch=1)],
            ["alpha"], ["alpha"],
            {"alpha": {"session_epoch": 2, "session_id": "sid-other"}})
        self.assertEqual(len(lines), 1)
        self.assertIn("collision risk", lines[0])
        self.assertIn("sch reset-session alpha", lines[0])
        self.assertIn("sch delete alpha --yes", lines[0])

    def test_s3_failure_raises(self):
        with patch.object(list_cmd, "_s3",
                          side_effect=list_cmd.RemoteCheckError("down")):
            with self.assertRaises(list_cmd.RemoteCheckError):
                list_cmd.remote_orphan_report(self.cfg, [_record("a")])


if __name__ == "__main__":
    unittest.main()
