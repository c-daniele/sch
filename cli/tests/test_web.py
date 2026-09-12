"""Tests for the stdlib-only `sch web` command."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_CLI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _CLI_DIR not in sys.path:
    sys.path.insert(0, _CLI_DIR)

from sch import runtime, workspace
from sch.commands import web


def _cfg(tmp_dir):
    return SimpleNamespace(
        ws_dir=Path(tmp_dir) / "workspaces",
        default_harness="claude",
        default_storage="s3",
        workspace_registry_url="",
        region="test-region",
    )


def _result(data):
    return runtime.InvocationResult(True, json.dumps(data))


class FakeBridge:
    def __init__(self, line=None, returncode=0):
        self.stderr = io.StringIO(
            line
            or '{"type":"ready","host":"127.0.0.1","port":4321,'
            '"url":"http://127.0.0.1:4321"}\n'
        )
        self.returncode = returncode
        self.terminated = False

    def poll(self):
        return self.returncode if self.terminated else None

    def wait(self, timeout=None):
        if timeout is not None:
            self.terminated = True
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True


class WebArgumentTests(unittest.TestCase):
    def test_parser_supports_creation_flags_and_no_browser(self):
        self.assertEqual(
            web._parse_args(["ws", "--harness", "opencode", "--storage", "session", "--no-browser"]),
            ("ws", "opencode", "session", True),
        )

    def test_parser_rejects_force_and_invalid_storage(self):
        for args in (["ws", "--force"], ["ws", "--storage", "efs"]):
            with self.subTest(args=args), self.assertRaises(SystemExit):
                web._parse_args(args)


class WebCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = _cfg(self.tmp.name)
        self.resolved = SimpleNamespace(
            sid="sid", harness="opencode", storage="s3", epoch=2,
            identity="owner/ws", was_created=False,
        )

    def _run(self, args=None, bridge=None, browser_result=True, browser_error=None):
        bridge = bridge or FakeBridge()
        invokes = [
            _result({"status": "ok", "storage": "s3"}),
            _result({"status": "ok", "port": 4096, "capabilities": {"web": True}}),
        ]
        with patch.object(web, "_require_local_bridge", return_value="node.exe"), \
             patch.object(web.harness_mod, "resolve_harness", return_value=self.resolved), \
             patch.object(web, "runtime_arn", return_value="arn"), \
             patch.object(web.runtime, "invoke_verified", side_effect=invokes), \
             patch.object(web.runtime, "invoke_best_effort") as advisory, \
             patch.object(web.workspace, "mark_status") as status, \
             patch.object(web.subprocess, "Popen", return_value=bridge) as popen, \
             patch.object(
                 web.webbrowser, "open", return_value=browser_result,
                 side_effect=browser_error,
             ) as browser:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = web.cmd_web(self.cfg, args or ["ws"])
        return rc, out.getvalue(), advisory, status, popen, browser, bridge

    def test_claude_flag_fails_before_dependencies_workspace_or_runtime(self):
        with patch.object(web, "_require_local_bridge") as deps, \
             patch.object(web.harness_mod, "resolve_harness") as resolve, \
             patch.object(web.runtime, "invoke_verified") as invoke, \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                web.cmd_web(self.cfg, ["new", "--harness", "claude"])
        deps.assert_not_called()
        resolve.assert_not_called()
        invoke.assert_not_called()
        self.assertFalse(workspace.workspace_exists(self.cfg, "new"))

    def test_existing_claude_workspace_fails_without_runtime_call(self):
        claude = SimpleNamespace(
            sid="sid", harness="claude", storage="s3", epoch=1,
            identity="", was_created=False,
        )
        with patch.object(web, "_require_local_bridge", return_value="node"), \
             patch.object(web.harness_mod, "resolve_harness", return_value=claude), \
             patch.object(web.runtime, "invoke_verified") as invoke, \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                web.cmd_web(self.cfg, ["ws"])
        invoke.assert_not_called()

    def test_pi_flag_fails_before_dependencies_workspace_or_runtime(self):
        """add-pi-harness task 5.7 (spec: remote-ui-tunnel, "Web non disponibile
        su workspace pi")."""
        stderr = io.StringIO()
        with patch.object(web, "_require_local_bridge") as deps, \
             patch.object(web.harness_mod, "resolve_harness") as resolve, \
             patch.object(web.runtime, "invoke_verified") as invoke, \
             contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                web.cmd_web(self.cfg, ["new", "--harness", "pi"])
        deps.assert_not_called()
        resolve.assert_not_called()
        invoke.assert_not_called()
        self.assertFalse(workspace.workspace_exists(self.cfg, "new"))
        message = stderr.getvalue()
        self.assertIn("pi has no web UI", message)
        # No ACP agent for pi, so it must not be suggested.
        self.assertNotIn("sch acp", message)

    def test_existing_pi_workspace_fails_without_runtime_call(self):
        resolved = SimpleNamespace(
            sid="sid", harness="pi", storage="s3", epoch=1,
            identity="", was_created=False,
        )
        with patch.object(web, "_require_local_bridge", return_value="node"), \
             patch.object(web.harness_mod, "resolve_harness", return_value=resolved), \
             patch.object(web.runtime, "invoke_verified") as invoke, \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                web.cmd_web(self.cfg, ["ws"])
        invoke.assert_not_called()

    def test_new_workspace_defaults_to_opencode_and_passes_storage(self):
        with patch.object(web, "_require_local_bridge", return_value="node"), \
             patch.object(web.harness_mod, "resolve_harness", side_effect=RuntimeError("stop")) as resolve:
            with self.assertRaisesRegex(RuntimeError, "stop"):
                web.cmd_web(self.cfg, ["new", "--storage", "session"])
        # provisioning=True is part of the contract: `web` boots a microVM and
        # must rotate a session superseded by a runtime version change
        # (add-task-liveness-safety task 2.3).
        resolve.assert_called_once_with(
            self.cfg, "new", "opencode", "session", provisioning=True
        )

    def test_ready_url_printed_browser_opened_and_spawn_is_popen(self):
        rc, out, advisory, status, popen, browser, bridge = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(out, "http://127.0.0.1:4321\n")
        browser.assert_called_once_with("http://127.0.0.1:4321")
        argv = popen.call_args.args[0]
        self.assertEqual(argv[0], "node.exe")
        self.assertIn("web.js", argv[1])
        self.assertIn("4096", argv)
        self.assertTrue(bridge.terminated)
        self.assertEqual(status.call_args_list[0].args[2], "web-opened")
        self.assertEqual(status.call_args_list[1].args[2], "web-closed")
        payloads = [json.loads(call.args[2]) for call in advisory.call_args_list]
        self.assertEqual([item["active"] for item in payloads], [True, False])

    def test_no_browser_still_prints_url_and_waits(self):
        rc, out, _, _, _, browser, _ = self._run(["ws", "--no-browser"])
        self.assertEqual(rc, 0)
        self.assertIn("http://127.0.0.1:4321", out)
        browser.assert_not_called()

    def test_ready_url_is_flushed_for_dashboard_pipe_reader(self):
        with patch("builtins.print") as output:
            self._run(["ws", "--no-browser"])
        self.assertTrue(
            any(call.args == ("http://127.0.0.1:4321",) and call.kwargs.get("flush")
                for call in output.call_args_list)
        )

    def test_browser_exception_keeps_bridge_in_foreground(self):
        rc, out, _, _, _, browser, bridge = self._run(
            browser_error=OSError("none")
        )
        self.assertEqual(rc, 0)
        self.assertIn("http://127.0.0.1:4321", out)
        browser.assert_called_once()
        self.assertTrue(bridge.terminated)

    def test_browser_false_keeps_bridge_in_foreground(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc, out, _, _, _, _, bridge = self._run(browser_result=False)
        self.assertEqual(rc, 0)
        self.assertIn("http://127.0.0.1:4321", out)
        self.assertIn("could not open browser automatically", err.getvalue())
        self.assertTrue(bridge.terminated)

    def test_missing_web_capability_rejects_old_image_before_spawn(self):
        responses = [
            _result({"status": "ok", "storage": "s3"}),
            _result({"status": "ok", "port": 4096}),
        ]
        err = io.StringIO()
        with patch.object(web, "_require_local_bridge", return_value="node"), \
             patch.object(web.harness_mod, "resolve_harness", return_value=self.resolved), \
             patch.object(web, "runtime_arn", return_value="arn"), \
             patch.object(web.runtime, "invoke_verified", side_effect=responses), \
             patch.object(web.subprocess, "Popen") as popen, \
             contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit):
                web.cmd_web(self.cfg, ["ws"])
        self.assertIn("runtime image predates web access", err.getvalue())
        popen.assert_not_called()

    def test_invalid_ready_line_terminates_bridge_without_advisory(self):
        bridge = FakeBridge('{"type":"ready","host":"0.0.0.0","port":1,"url":"bad"}\n')
        with patch.object(web, "_require_local_bridge", return_value="node"), \
             patch.object(web.harness_mod, "resolve_harness", return_value=self.resolved), \
             patch.object(web, "runtime_arn", return_value="arn"), \
             patch.object(web.runtime, "invoke_verified", side_effect=[
                 _result({"status": "ok", "storage": "s3"}),
                 _result({"status": "ok", "port": 4096, "capabilities": {"web": True}}),
             ]), \
             patch.object(web.runtime, "invoke_best_effort") as advisory, \
             patch.object(web.subprocess, "Popen", return_value=bridge), \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                web.cmd_web(self.cfg, ["ws"])
        self.assertTrue(bridge.terminated)
        advisory.assert_not_called()

    def test_wait_error_still_marks_inactive_and_terminates(self):
        bridge = FakeBridge()
        bridge.wait = MagicMock(side_effect=[OSError("wait failed"), 0])
        advisory = MagicMock()
        with patch.object(web, "_require_local_bridge", return_value="node"), \
             patch.object(web.harness_mod, "resolve_harness", return_value=self.resolved), \
             patch.object(web, "runtime_arn", return_value="arn"), \
             patch.object(web.runtime, "invoke_verified", side_effect=[
                 _result({"status": "ok", "storage": "s3"}),
                 _result({"status": "ok", "port": 4096, "capabilities": {"web": True}}),
             ]), \
             patch.object(web.runtime, "invoke_best_effort", advisory), \
             patch.object(web.workspace, "mark_status"), \
             patch.object(web.subprocess, "Popen", return_value=bridge), \
             patch.object(web.webbrowser, "open"):
            with self.assertRaisesRegex(OSError, "wait failed"):
                web.cmd_web(self.cfg, ["ws"])
        self.assertTrue(bridge.terminated)
        payloads = [json.loads(call.args[2]) for call in advisory.call_args_list]
        self.assertEqual([item["active"] for item in payloads], [True, False])

    def test_does_not_check_or_run_local_opencode(self):
        with patch("sch.deps.which_opencode", side_effect=AssertionError("must not check")), \
             patch.object(web.subprocess, "run", side_effect=AssertionError("must not version-check")):
            rc, _, _, _, _, _, _ = self._run(["ws", "--no-browser"])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
