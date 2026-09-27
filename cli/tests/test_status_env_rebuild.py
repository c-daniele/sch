"""`sch status --live` renders the post-restore env rebuild outcome (TASK-21).

The shim's info action reports `checkpoint.env_rebuild`; the live status
turns it into one `env_rebuild` line naming what was rebuilt, what was
skipped and why. The offline status and older images render nothing new.
"""

import contextlib
import io
import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

_CLI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _CLI_DIR not in sys.path:
    sys.path.insert(0, _CLI_DIR)

from sch import runtime
from sch import workspace as workspace_mod
from sch.commands import status as status_cmd

REBUILD = {
    "status": "rebuilt-partial",
    "steps": [
        {"env": "node", "label": "npm-ci", "result": "ok", "reason": None},
        {"env": "python", "label": "uv-sync-frozen", "result": "skipped",
         "reason": "no-lockfile: pyproject.toml without uv.lock"},
    ],
    "summary": "node npm-ci ok; python uv-sync-frozen skipped "
               "(no-lockfile: pyproject.toml without uv.lock)",
    "worktree": "unchanged",
}


class FormatEnvRebuildTests(unittest.TestCase):
    def test_full_block(self):
        self.assertEqual(
            status_cmd.format_env_rebuild(REBUILD),
            "rebuilt-partial (node npm-ci ok; python uv-sync-frozen skipped "
            "(no-lockfile: pyproject.toml without uv.lock)); worktree unchanged",
        )

    def test_absent_or_not_run_renders_nothing(self):
        for info in (None, {}, {"status": "not-run"}, "rebuilt"):
            self.assertEqual(status_cmd.format_env_rebuild(info), "")

    def test_status_only(self):
        self.assertEqual(
            status_cmd.format_env_rebuild({"status": "skipped-disabled", "summary": ""}),
            "skipped-disabled",
        )


class LiveStatusTests(unittest.TestCase):
    def run_status(self, live_payload, args=("ws", "--live")):
        cfg = SimpleNamespace(
            region="eu-west-1", ws_dir=None, default_harness="opencode",
            default_storage="s3", workspace_registry_url="",
        )
        resolved = SimpleNamespace(
            sid="sid-1", harness="opencode", identity="", storage="s3", epoch=1,
            was_created=False,
        )
        buf = io.StringIO()
        with patch.object(
            status_cmd.harness_mod, "resolve_harness", return_value=resolved,
        ), patch.object(
            status_cmd, "read_offline_status",
            return_value=json.dumps({"state": "succeeded", "exit_code": 0}),
        ), patch.object(
            status_cmd.workspace, "read_workspace_state",
            return_value=workspace_mod.WorkspaceState(
                sid="sid-1", harness="opencode", storage="s3",
                storage_present=True, epoch=1,
            ),
        ), patch.object(
            status_cmd.runtime, "invoke_verified",
            return_value=runtime.InvocationResult(True, json.dumps(live_payload)),
        ), contextlib.redirect_stdout(buf):
            rc = status_cmd.cmd_status(cfg, list(args))
        return rc, buf.getvalue()

    def test_live_status_names_rebuilt_and_skipped_envs(self):
        rc, out = self.run_status({
            "status": "ok", "task": {}, "checkpoint": {"env_rebuild": REBUILD},
        })
        self.assertEqual(rc, 0)
        line = [x for x in out.splitlines() if x.startswith("env_rebuild")]
        self.assertEqual(len(line), 1, out)
        self.assertIn("node npm-ci ok", line[0])
        self.assertIn("no-lockfile: pyproject.toml without uv.lock", line[0])
        self.assertIn("worktree unchanged", line[0])

    def test_older_image_without_block_renders_nothing(self):
        rc, out = self.run_status({"status": "ok", "task": {}, "checkpoint": {}})
        self.assertEqual(rc, 0)
        self.assertNotIn("env_rebuild", out)


if __name__ == "__main__":
    unittest.main()
