import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from sch import repo
from sch.commands import deploy


def run_deploy(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = deploy.cmd_deploy(None, args)
    return rc, out.getvalue(), err.getvalue()


class CmdDeployTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "repo"
        (self.root / "infra").mkdir(parents=True)
        (self.root / "infra" / "deploy.sh").write_text("#!/bin/bash\n")

    def test_options_are_passed_through_to_the_script(self):
        calls = []

        def fake_run(argv):
            calls.append(argv)
            return type("R", (), {"returncode": 0})()

        with patch.object(repo, "repo_root", return_value=self.root), \
             patch.object(deploy.sys, "platform", "darwin"), \
             patch.object(deploy.subprocess, "run", side_effect=fake_run):
            rc, _, _ = run_deploy(["-l", "-v", "v7"])
        self.assertEqual(rc, 0)
        self.assertEqual(
            calls,
            [["bash", str(self.root / "infra" / "deploy.sh"), "-l", "-v", "v7"]],
        )

    def test_script_exit_code_is_propagated(self):
        with patch.object(repo, "repo_root", return_value=self.root), \
             patch.object(deploy.sys, "platform", "darwin"), \
             patch.object(deploy.subprocess, "run",
                          return_value=type("R", (), {"returncode": 2})()):
            rc, _, _ = run_deploy([])
        self.assertEqual(rc, 2)

    def test_long_help_is_translated_to_the_script_flag(self):
        calls = []

        def fake_run(argv):
            calls.append(argv)
            return type("R", (), {"returncode": 0})()

        with patch.object(repo, "repo_root", return_value=self.root), \
             patch.object(deploy.sys, "platform", "darwin"), \
             patch.object(deploy.subprocess, "run", side_effect=fake_run):
            rc, _, _ = run_deploy(["--help"])
        self.assertEqual(rc, 0)
        self.assertEqual(calls[0][-1], "-h")

    def test_no_support_repo_points_at_sch_setup(self):
        with patch.object(repo, "repo_root", return_value=None):
            rc, _, err = run_deploy([])
        self.assertEqual(rc, 1)
        self.assertIn("sch setup", err)

    def test_missing_script_is_reported(self):
        (self.root / "infra" / "deploy.sh").unlink()
        with patch.object(repo, "repo_root", return_value=self.root):
            rc, _, err = run_deploy([])
        self.assertEqual(rc, 1)
        self.assertIn("infra/deploy.sh", err)

    def test_windows_explains_the_bash_requirement(self):
        with patch.object(repo, "repo_root", return_value=self.root), \
             patch.object(deploy.sys, "platform", "win32"):
            rc, _, err = run_deploy(["-s"])
        self.assertEqual(rc, 1)
        self.assertIn("bash", err)


if __name__ == "__main__":
    unittest.main()
