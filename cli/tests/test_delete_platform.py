"""The delete command deliberately has no POSIX-only branches."""

import contextlib
import io
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sch.commands import delete


class DeletePlatformParityTests(unittest.TestCase):
    def test_single_and_bulk_paths_match_under_posix_and_windows_branches(self):
        calls = 0
        for platform in ("posix", "nt"):
            cfg = SimpleNamespace(workspace_registry_url="", ws_dir=Path("/tmp/delete-test"))
            with self.subTest(platform=platform), \
                    mock.patch("sch.commands.delete.os.name", platform), \
                    mock.patch("sch.commands.delete._delete_local") as delete_one, \
                    mock.patch("sch.commands.delete._targets", return_value=["a", "b"]), \
                    contextlib.redirect_stdout(io.StringIO()) as stdout, \
                    contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(delete.cmd_delete(cfg, ["a", "--yes"]), 0)
                self.assertEqual(delete.cmd_delete(cfg, ["--all", "--yes"]), 0)
                self.assertEqual(stdout.getvalue(), "")
                self.assertIn("summary: 2 deleted, 0 failed", stderr.getvalue())
                calls += delete_one.call_count
        self.assertEqual(calls, 6)


if __name__ == "__main__":
    unittest.main()
