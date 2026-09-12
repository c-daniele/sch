import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from sch import repo
from sch.commands import setup


def run_setup(cfg, args):
    """Run cmd_setup capturing stdout/stderr; return (rc, out, err)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = setup.cmd_setup(cfg, args)
    return rc, out.getvalue(), err.getvalue()


class CheckPrerequisitesTests(unittest.TestCase):
    def test_all_present_is_fully_ok(self):
        with patch.object(setup.shutil, "which", return_value="/usr/bin/x"), \
             patch.object(setup, "_run", return_value=(0, "aws-cli/2.15.0")):
            checks = {c.name: c for c in setup.check_prerequisites()}
        self.assertTrue(all(c.ok for c in checks.values()))
        self.assertTrue(checks["aws-cli"].ok)
        self.assertEqual(checks["aws-cli"].detail, "aws-cli/2.15.0")

    def test_nothing_installed_only_aws_is_fatal(self):
        with patch.object(setup.shutil, "which", return_value=None):
            checks = {c.name: c for c in setup.check_prerequisites()}
        self.assertFalse(checks["aws-cli"].ok)
        self.assertTrue(checks["aws-cli"].required)
        self.assertFalse(checks["docker"].ok)
        self.assertFalse(checks["docker"].required)
        self.assertFalse(checks["opencode"].ok)
        self.assertFalse(checks["opencode"].required)
        self.assertTrue(checks["node"].required)

    def test_aws_v1_detected_as_missing(self):
        def which(name):
            return "/usr/bin/aws" if name == "aws" else None

        with patch.object(setup.shutil, "which", side_effect=which), \
             patch.object(setup, "_run", return_value=(0, "aws-cli/1.29.0")):
            checks = {c.name: c for c in setup.check_prerequisites()}
        self.assertFalse(checks["aws-cli"].ok)
        self.assertIn("v2", checks["aws-cli"].hint)


class StackStatusTests(unittest.TestCase):
    class Cfg:
        region = "eu-west-1"

        def stack_name(self):
            return "sch-dev-runtime"

    def test_deployed(self):
        with patch.object(setup, "_run", return_value=(0, "RuntimeArn\truntime-arn\n")):
            status, detail = setup.stack_status(self.Cfg())
        self.assertEqual(status, "deployed")
        self.assertIn("runtime-arn", detail)

    def test_missing(self):
        with patch.object(
            setup, "_run",
            return_value=(1, "An error occurred (ValidationError) ... does not exist"),
        ):
            status, _ = setup.stack_status(self.Cfg())
        self.assertEqual(status, "missing")

    def test_error(self):
        with patch.object(setup, "_run", return_value=(1, "Unable to locate credentials")):
            status, _ = setup.stack_status(self.Cfg())
        self.assertEqual(status, "error")


class CmdSetupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = type("Cfg", (), {"region": "eu-west-1"})()
        self.cfg.stack_name = lambda: "sch-dev-runtime"
        self.root = Path(self._tmp.name) / "repo"
        (self.root / "tunnel").mkdir(parents=True)
        (self.root / "tunnel" / "sync.js").write_text("")
        (self.root / "tunnel" / "node_modules").mkdir()
        (self.root / "infra").mkdir()
        (self.root / "infra" / "deploy.sh").write_text("#!/bin/bash\n")

    def test_happy_path_reports_setup_complete(self):
        with patch.object(setup, "check_prerequisites", return_value=[]), \
             patch.object(repo, "repo_root", return_value=self.root), \
             patch.object(setup, "stack_status", return_value=("deployed", "arn")):
            rc, out, err = run_setup(self.cfg, [])
        self.assertEqual(rc, 0)
        self.assertIn("setup complete", out)
        self.assertIn(str(self.root), out)

    def test_missing_stack_fails_with_deploy_hint(self):
        with patch.object(setup, "check_prerequisites", return_value=[]), \
             patch.object(repo, "repo_root", return_value=self.root), \
             patch.object(setup, "stack_status", return_value=("missing", "not found")):
            rc, out, err = run_setup(self.cfg, [])
        self.assertEqual(rc, 1)
        # One actionable command, not a cd + script incantation.
        self.assertIn("sch setup --deploy", err)
        self.assertNotIn("cd ", err)

    def test_failed_prerequisite_stops_before_repo_and_stack(self):
        from sch.commands.setup import Check

        failed = [Check("aws-cli", False, True, "not found", "install AWS CLI v2")]
        with patch.object(setup, "check_prerequisites", return_value=failed), \
             patch.object(repo, "repo_root") as repo_root_mock, \
             patch.object(setup, "stack_status") as stack_mock:
            rc, out, err = run_setup(self.cfg, [])
        self.assertEqual(rc, 1)
        repo_root_mock.assert_not_called()
        stack_mock.assert_not_called()
        self.assertIn("MISSING", out)

    def test_missing_repo_is_cloned_into_managed_root(self):
        clone_calls = []

        def fake_clone(url, destination):
            clone_calls.append((url, destination))
            return True

        with patch.object(setup, "check_prerequisites", return_value=[]), \
             patch.object(repo, "repo_root", side_effect=[None, self.root]), \
             patch.object(repo, "managed_root", return_value=Path(self._tmp.name) / "managed"), \
             patch.object(setup, "_clone_support_repo", side_effect=fake_clone), \
             patch.object(setup, "stack_status", return_value=("deployed", "arn")):
            rc, out, err = run_setup(self.cfg, ["--repo", "https://example.com/sch.git"])
        self.assertEqual(rc, 0)
        self.assertEqual(
            clone_calls,
            [("https://example.com/sch.git", Path(self._tmp.name) / "managed")],
        )

    def test_deploy_flag_runs_deploy_script(self):
        ran = []

        def fake_run(argv):
            ran.append(argv)
            result = type("R", (), {"returncode": 0})()
            return result

        with patch.object(setup, "check_prerequisites", return_value=[]), \
             patch.object(repo, "repo_root", return_value=self.root), \
             patch.object(setup, "stack_status", return_value=("deployed", "arn")), \
             patch.object(setup, "sys") as sys_mock, \
             patch.object(setup, "subprocess") as sub:
            sys_mock.platform = "linux"
            sub.run.side_effect = fake_run
            rc, out, err = run_setup(self.cfg, ["--deploy"])
        self.assertEqual(rc, 0)
        self.assertEqual(ran, [["bash", str(self.root / "infra" / "deploy.sh")]])

    def test_deploy_flag_on_windows_prints_instructions(self):
        with patch.object(setup, "check_prerequisites", return_value=[]), \
             patch.object(repo, "repo_root", return_value=self.root), \
             patch.object(setup, "stack_status", return_value=("deployed", "arn")), \
             patch.object(setup.sys, "platform", "win32"):
            rc, out, err = run_setup(self.cfg, ["--deploy"])
        self.assertEqual(rc, 1)
        self.assertIn("infra/deploy.sh", err)


class EnsureTunnelDepsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "repo"
        (self.root / "tunnel").mkdir(parents=True)

    def _capture(self, fn, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            value = fn(*args)
        return value, out.getvalue(), err.getvalue()

    def test_already_installed_is_a_noop(self):
        (self.root / "tunnel" / "node_modules").mkdir()
        with patch.object(setup.subprocess, "run") as run_mock:
            ok, out, _ = self._capture(setup.ensure_tunnel_deps, self.root)
        self.assertTrue(ok)
        run_mock.assert_not_called()
        self.assertIn("ok", out)

    def test_missing_deps_are_installed_with_npm(self):
        calls = []

        def fake_run(argv, cwd=None):
            calls.append((argv, cwd))
            return type("R", (), {"returncode": 0})()

        with patch.object(setup.shutil, "which", return_value="/usr/bin/npm"), \
             patch.object(setup.subprocess, "run", side_effect=fake_run):
            ok, out, _ = self._capture(setup.ensure_tunnel_deps, self.root)
        self.assertTrue(ok)
        self.assertEqual(
            calls, [(["/usr/bin/npm", "install"], str(self.root / "tunnel"))]
        )
        self.assertIn("installed", out)

    def test_npm_failure_is_reported_and_not_fatal(self):
        with patch.object(setup.shutil, "which", return_value="/usr/bin/npm"), \
             patch.object(setup.subprocess, "run",
                          return_value=type("R", (), {"returncode": 1})()):
            ok, _, err = self._capture(setup.ensure_tunnel_deps, self.root)
        self.assertFalse(ok)
        self.assertIn("npm install", err)

    def test_missing_npm_prints_the_command(self):
        with patch.object(setup.shutil, "which", return_value=None):
            ok, _, err = self._capture(setup.ensure_tunnel_deps, self.root)
        self.assertFalse(ok)
        self.assertIn("npm install", err)


class RefreshSupportRepoTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "managed"
        self.root.mkdir(parents=True)

    def _capture(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            value = setup.refresh_support_repo(*args)
        return value, out.getvalue(), err.getvalue()

    def test_clean_managed_checkout_is_fast_forwarded(self):
        git_calls = []

        def fake_git(root, *args):
            git_calls.append(args)
            if args[0] == "status":
                return 0, ""
            if args[0] == "pull":
                return 0, "Updating abc..def\nFast-forward\n"
            return 0, "true"

        with patch.object(repo, "managed_root", return_value=self.root), \
             patch.object(setup, "_git", side_effect=fake_git):
            ok, out, _ = self._capture(self.root)
        self.assertTrue(ok)
        self.assertIn(("pull", "--ff-only"), git_calls)
        self.assertIn("updating support repo", out.lower())

    def test_dirty_checkout_is_left_alone(self):
        def fake_git(root, *args):
            if args[0] == "status":
                return 0, " M infra/deploy.sh\n"
            if args[0] == "pull":
                raise AssertionError("must not pull a dirty checkout")
            return 0, "true"

        with patch.object(repo, "managed_root", return_value=self.root), \
             patch.object(setup, "_git", side_effect=fake_git):
            ok, out, _ = self._capture(self.root)
        self.assertTrue(ok)
        self.assertIn("local changes", out)

    def test_unmanaged_checkout_is_never_pulled(self):
        other = Path(self._tmp.name) / "my-clone"
        with patch.object(repo, "managed_root", return_value=self.root), \
             patch.object(setup, "_git") as git_mock:
            ok, out, _ = self._capture(other)
        self.assertTrue(ok)
        git_mock.assert_not_called()
        self.assertIn("not managed by sch", out)

    def test_pull_failure_is_reported_and_not_fatal(self):
        def fake_git(root, *args):
            if args[0] == "status":
                return 0, ""
            if args[0] == "pull":
                return 1, "fatal: Not possible to fast-forward, aborting.\n"
            return 0, "true"

        with patch.object(repo, "managed_root", return_value=self.root), \
             patch.object(setup, "_git", side_effect=fake_git):
            ok, _, err = self._capture(self.root)
        self.assertFalse(ok)
        self.assertIn("could not update", err)


if __name__ == "__main__":
    unittest.main()
