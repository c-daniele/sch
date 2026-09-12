"""Unit tests for mirror-sync binding visibility in `sch list` and
`sch status` (TASK-36).

Contract under test:
  - _status_cell appends [sync: <path>] when a binding exists and there is no
    git-native branch.
  - _status_cell appends [git-native: <branch>] when a git-native branch is
    recorded (unchanged from before).
  - _status_cell returns the plain status when neither is set.
  - format_status includes a "sync_root" line when sync_root is passed.
  - format_status omits the "sync_root" line when sync_root is None.
  - cmd_status passes sync_root to render_status for mirror-synced workspaces.
  - --json mode is a pure passthrough; sync_root never reaches the output.
"""

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

from sch import sync as sync_mod, workspace as workspace_mod
from sch.commands import list as list_cmd
from sch.commands import status as status_cmd


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


def _record(name, git_branch="", runtime_workspace=None):
    return list_cmd.WorkspaceRecord(
        name=name,
        harness="opencode",
        storage="s3",
        sid="sid-{}".format(name),
        status="run-opened",
        runtime_workspace=runtime_workspace or name,
        git_branch=git_branch,
    )


class StatusCellMirrorSyncTests(unittest.TestCase):
    """_status_cell shows [sync: <path>] for mirror-synced workspaces."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = _cfg(self.tmp.name)
        # Create a real local directory to use as the sync root.
        self.local_dir = Path(self.tmp.name) / "project"
        self.local_dir.mkdir()

    def test_sync_annotation_shown_when_binding_exists(self):
        sync_mod.save_binding(self.cfg, "myws", self.local_dir)
        cell = list_cmd._status_cell(self.cfg, _record("myws"))
        self.assertIn("[sync:", cell)
        self.assertIn(str(self.local_dir), cell)
        self.assertTrue(cell.startswith("run-opened"))

    def test_no_annotation_when_no_binding(self):
        cell = list_cmd._status_cell(self.cfg, _record("myws"))
        self.assertEqual(cell, "run-opened")

    def test_git_native_annotation_takes_precedence(self):
        # git-native and mirror-sync are mutually exclusive at run time, but
        # even if a stale binding somehow coexists, git-native wins in the cell.
        sync_mod.save_binding(self.cfg, "myws", self.local_dir)
        cell = list_cmd._status_cell(self.cfg, _record("myws", git_branch="feat/foo"))
        self.assertIn("[git-native: feat/foo]", cell)
        self.assertNotIn("[sync:", cell)

    def test_git_native_annotation_unchanged_without_binding(self):
        cell = list_cmd._status_cell(self.cfg, _record("myws", git_branch="main"))
        self.assertEqual(cell, "run-opened [git-native: main]")

    def test_path_shown_even_when_dir_no_longer_exists(self):
        """read_binding_root must not die when the bound directory is gone."""
        sync_mod.save_binding(self.cfg, "vanished", self.local_dir)
        # Remove the directory after binding is saved.
        self.local_dir.rmdir()
        cell = list_cmd._status_cell(self.cfg, _record("vanished"))
        # The path from the saved JSON is still shown (display-only).
        self.assertIn("[sync:", cell)


class FormatStatusSyncRootTests(unittest.TestCase):
    """format_status emits a sync_root line only when passed."""

    def test_sync_root_line_present_when_given(self):
        out = status_cmd.format_status(
            {"state": "none"},
            sync_root=Path("/home/user/project"),
        )
        self.assertIn("sync_root    : /home/user/project", out)

    def test_sync_root_line_absent_when_none(self):
        out = status_cmd.format_status({"state": "none"}, sync_root=None)
        self.assertNotIn("sync_root", out)

    def test_sync_root_appears_after_branch_before_workspace_root(self):
        out = status_cmd.format_status(
            {"state": "none", "workspace_root": "/remote/path"},
            sync_root=Path("/local/path"),
        )
        lines = out.splitlines()
        sync_line = next(i for i, l in enumerate(lines) if "sync_root" in l)
        root_line = next(i for i, l in enumerate(lines) if "workspace_root" in l)
        self.assertLess(sync_line, root_line)

    def test_existing_fields_unaffected_by_sync_root(self):
        data = {
            "state": "succeeded",
            "exit_code": 0,
            "harness": "opencode",
        }
        base_out = status_cmd.format_status(data)
        with_root = status_cmd.format_status(data, sync_root=Path("/p"))
        for line in base_out.splitlines():
            self.assertIn(line, with_root)


class CmdStatusSyncRootTests(unittest.TestCase):
    """cmd_status passes sync_root to render_status; --json stays pure."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = _cfg(self.tmp.name)
        self.local_dir = Path(self.tmp.name) / "project"
        self.local_dir.mkdir()
        self.resolved = SimpleNamespace(
            sid="sid-1", harness="opencode", identity="", storage="s3", epoch=1,
            was_created=False,
        )

    def _run(self, ws, args=None, binding_root=None):
        args = args or [ws]
        raw = json.dumps({"state": "none"})
        ws_state = workspace_mod.WorkspaceState(
            sid="sid-1", harness="opencode", storage="s3",
            storage_present=True, epoch=1,
        )
        buf = io.StringIO()
        with patch.object(
            status_cmd.harness_mod, "resolve_harness", return_value=self.resolved
        ), patch.object(
            status_cmd, "read_offline_status", return_value=raw
        ), patch.object(
            status_cmd.workspace, "read_workspace_state", return_value=ws_state
        ), patch.object(
            sync_mod, "read_binding_root", return_value=binding_root
        ), contextlib.redirect_stdout(buf):
            rc = status_cmd.cmd_status(self.cfg, args)
        return rc, buf.getvalue()

    def test_sync_root_shown_when_binding_present(self):
        _, out = self._run("myws", binding_root=Path("/home/me/project"))
        self.assertIn("sync_root    : /home/me/project", out)

    def test_sync_root_absent_when_no_binding(self):
        _, out = self._run("myws", binding_root=None)
        self.assertNotIn("sync_root", out)

    def test_json_mode_pure_passthrough_no_sync_root(self):
        _, out = self._run("myws", args=["myws", "--json"],
                           binding_root=Path("/home/me/project"))
        # --json is a pure passthrough: sync_root must not appear.
        data = json.loads(out)
        self.assertNotIn("sync_root", data)
        self.assertNotIn("sync_root", out)


if __name__ == "__main__":
    unittest.main()
