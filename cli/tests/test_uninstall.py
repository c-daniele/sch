import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from sch import repo
from sch.commands import uninstall


class Cfg:
    region = "eu-west-1"
    project = "sch"
    env = "dev"

    def __init__(self, config_dir):
        self.config_dir = config_dir

    def stack_name(self):
        return "sch-dev-runtime"


def run_uninstall(cfg, args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = uninstall.cmd_uninstall(cfg, args)
    return rc, out.getvalue(), err.getvalue()


class UninstallFixture(unittest.TestCase):
    """Every path the command may delete is redirected into a temp tree: a test
    must never be able to remove the developer's real ~/.config/sch, ~/.sch/env
    or $TMPDIR/sch-* artifacts."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)

        self.config_dir = root / "config" / "sch"
        (self.config_dir / "workspaces").mkdir(parents=True)
        self.managed = root / "share" / "sch" / "repo"
        (self.managed / "tunnel").mkdir(parents=True)
        self.keys = root / "home" / ".sch" / "env"
        self.keys.parent.mkdir(parents=True)
        self.keys.write_text("OPENCODE_API_KEY=x\n")
        self.fake_tmp = root / "tmpdir"
        self.fake_tmp.mkdir()
        (self.fake_tmp / "sch-leftover").mkdir()

        self.cfg = Cfg(self.config_dir)
        self._patches = [
            patch.object(repo, "managed_root", return_value=self.managed),
            patch.object(uninstall, "provider_env_path", return_value=self.keys),
            patch.object(uninstall.tempfile, "gettempdir",
                         return_value=str(self.fake_tmp)),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])


class GuardTests(UninstallFixture):
    def test_refuses_while_the_runtime_stack_is_deployed(self):
        with patch.object(uninstall, "stack_status", return_value=("deployed", "arn")), \
             patch.object(uninstall.shutil, "rmtree") as rmtree:
            rc, _, err = run_uninstall(self.cfg, ["--yes"])
        self.assertEqual(rc, 1)
        self.assertIn("sch destroy", err)
        rmtree.assert_not_called()

    def test_refuses_when_the_aws_state_is_unknown(self):
        with patch.object(uninstall, "stack_status", return_value=("error", "no creds")):
            rc, _, err = run_uninstall(self.cfg, ["--yes"])
        self.assertEqual(rc, 1)
        self.assertIn("--force", err)

    def test_force_skips_the_guard_entirely(self):
        with patch.object(uninstall, "stack_status") as status, \
             patch.object(uninstall, "detect_install", return_value=("checkout", [])):
            rc, _, _ = run_uninstall(self.cfg, ["--yes", "--force"])
        status.assert_not_called()
        self.assertEqual(rc, 0)
        self.assertFalse(self.config_dir.exists())


class PlanTests(UninstallFixture):
    def test_plan_lists_every_local_location(self):
        targets = dict(
            (label, path) for label, path in uninstall.local_targets(self.cfg, False)
        )
        self.assertEqual(targets["local state (index + caches)"], self.config_dir)
        self.assertEqual(targets["managed support checkout"], self.managed)
        self.assertEqual(targets["provider keys"], self.keys)
        self.assertIn("temporary artifact", targets)

    def test_keep_keys_excludes_the_provider_env(self):
        labels = [label for label, _ in uninstall.local_targets(self.cfg, True)]
        self.assertNotIn("provider keys", labels)

    def test_dry_run_removes_nothing(self):
        with patch.object(uninstall, "stack_status", return_value=("missing", "gone")), \
             patch.object(uninstall, "detect_install", return_value=("pipx", ["pipx", "uninstall", "sch"])), \
             patch.object(uninstall.subprocess, "run") as run_mock:
            rc, out, _ = run_uninstall(self.cfg, ["--dry-run"])
        self.assertEqual(rc, 0)
        self.assertIn("dry run", out)
        self.assertTrue(self.config_dir.exists())
        self.assertTrue(self.keys.exists())
        run_mock.assert_not_called()

    def test_wrong_confirmation_aborts(self):
        with patch.object(uninstall, "stack_status", return_value=("missing", "gone")), \
             patch.object(uninstall, "detect_install", return_value=("checkout", [])), \
             patch("builtins.input", return_value="no"):
            rc, _, err = run_uninstall(self.cfg, [])
        self.assertEqual(rc, 1)
        self.assertIn("cancelled", err)
        self.assertTrue(self.config_dir.exists())


class RemovalTests(UninstallFixture):
    def test_removes_state_then_the_client(self):
        calls = []

        def fake_run(argv):
            calls.append(argv)
            return type("R", (), {"returncode": 0})()

        with patch.object(uninstall, "stack_status", return_value=("missing", "gone")), \
             patch.object(uninstall, "detect_install", return_value=("pipx", ["pipx", "uninstall", "sch"])), \
             patch.object(uninstall.sys, "platform", "darwin"), \
             patch.object(uninstall.subprocess, "run", side_effect=fake_run):
            rc, out, _ = run_uninstall(self.cfg, ["--yes"])
        self.assertEqual(rc, 0)
        self.assertFalse(self.config_dir.exists())
        self.assertFalse(self.managed.exists())
        self.assertFalse(self.keys.exists())
        self.assertFalse((self.fake_tmp / "sch-leftover").exists())
        self.assertEqual(calls, [["pipx", "uninstall", "sch"]])
        self.assertIn("uninstalled", out)

    def test_keep_client_leaves_the_package_alone(self):
        with patch.object(uninstall, "stack_status", return_value=("missing", "gone")), \
             patch.object(uninstall, "detect_install", return_value=("pipx", ["pipx", "uninstall", "sch"])), \
             patch.object(uninstall.subprocess, "run") as run_mock:
            rc, out, _ = run_uninstall(self.cfg, ["--yes", "--keep-client"])
        self.assertEqual(rc, 0)
        self.assertFalse(self.config_dir.exists())
        run_mock.assert_not_called()
        self.assertIn("left in place", out)

    def test_windows_prints_the_command_instead_of_running_it(self):
        with patch.object(uninstall, "stack_status", return_value=("missing", "gone")), \
             patch.object(uninstall, "detect_install", return_value=("pip", ["py", "-m", "pip", "uninstall", "-y", "sch"])), \
             patch.object(uninstall.sys, "platform", "win32"), \
             patch.object(uninstall.subprocess, "run") as run_mock:
            rc, out, _ = run_uninstall(self.cfg, ["--yes"])
        self.assertEqual(rc, 0)
        run_mock.assert_not_called()
        self.assertIn("Finish by removing the client", out)

    def test_failed_package_removal_is_reported(self):
        with patch.object(uninstall, "stack_status", return_value=("missing", "gone")), \
             patch.object(uninstall, "detect_install", return_value=("pipx", ["pipx", "uninstall", "sch"])), \
             patch.object(uninstall.sys, "platform", "darwin"), \
             patch.object(uninstall.subprocess, "run",
                          return_value=type("R", (), {"returncode": 1})()):
            rc, _, err = run_uninstall(self.cfg, ["--yes"])
        self.assertEqual(rc, 1)
        self.assertIn("still installed", err)


class DetectInstallTests(unittest.TestCase):
    def test_pipx_is_detected(self):
        def which(name):
            return "/usr/bin/pipx" if name == "pipx" else None

        with patch.object(uninstall.shutil, "which", side_effect=which), \
             patch.object(uninstall, "_run", return_value=(0, "sch 0.1.0\n")):
            method, command = uninstall.detect_install()
        self.assertEqual(method, "pipx")
        self.assertEqual(command[1:], ["uninstall", "sch"])

    def test_uv_tool_is_detected(self):
        def which(name):
            return "/usr/bin/uv" if name == "uv" else None

        with patch.object(uninstall.shutil, "which", side_effect=which), \
             patch.object(uninstall, "_run", return_value=(0, "sch v0.1.0\n")):
            method, command = uninstall.detect_install()
        self.assertEqual(method, "uv tool")
        self.assertEqual(command[1:], ["tool", "uninstall", "sch"])

    def test_pip_is_the_fallback_when_no_tool_manages_it(self):
        with patch.object(uninstall.shutil, "which", return_value=None), \
             patch.object(uninstall, "_run", return_value=(0, "Name: sch\n")):
            method, command = uninstall.detect_install()
        self.assertEqual(method, "pip")
        self.assertIn("uninstall", command)

    def test_checkout_when_nothing_has_it_installed(self):
        with patch.object(uninstall.shutil, "which", return_value=None), \
             patch.object(uninstall, "_run", return_value=(1, "")):
            method, command = uninstall.detect_install()
        self.assertEqual(method, "checkout")
        self.assertEqual(command, [])


if __name__ == "__main__":
    unittest.main()
