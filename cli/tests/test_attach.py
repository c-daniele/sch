"""Argument and harness-selection tests for `sch attach`."""

import contextlib
import io
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

from sch import harness as harness_mod
from sch import workspace
from sch.commands import attach
from sch.commands import acp


def _make_cfg(tmp_dir):
    return SimpleNamespace(
        ws_dir=Path(tmp_dir) / "workspaces", default_harness="claude", region="eu-west-1"
    )


class AttachArgumentTests(unittest.TestCase):
    def test_parses_explicit_opencode_harness_and_force(self):
        self.assertEqual(
            attach._parse_args(["ws", "--harness", "opencode", "--force"]),
            ("ws", "opencode", "", True),
        )

    def test_model_is_rejected(self):
        with self.assertRaises(SystemExit):
            attach._parse_args([
                "ws", "--model", "amazon-bedrock/global.anthropic.claude-sonnet-5",
            ])

    def test_branch_error_suggests_git_native_workflow(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                attach._parse_args(["ws", "--branch", "feature/example"])

        message = stderr.getvalue()
        self.assertIn(
            "sch run ws --harness opencode --branch <branch>", message
        )
        self.assertIn("sch attach ws --harness opencode", message)

    def test_missing_harness_value_dies(self):
        with self.assertRaises(SystemExit):
            attach._parse_args(["ws", "--harness"])

    def test_invalid_storage_dies_in_parser(self):
        with self.assertRaises(SystemExit):
            attach._parse_args(["ws", "--storage", "efs"])

    def test_acp_invalid_storage_dies_in_parser(self):
        with self.assertRaises(SystemExit):
            acp._parse_args(["ws", "--storage", "efs"])


class AcpPiRejectionTests(unittest.TestCase):
    """add-pi-harness task 5.3/5.7: `sch acp` needs an ACP agent in the microVM.
    opencode has one natively, claude through the pinned adapter; pi has none,
    so it is refused with actionable alternatives and WITHOUT any runtime call
    (spec: remote-ui-tunnel, "ACP rifiutato su workspace pi")."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = _make_cfg(self._tmp.name)

    def test_pi_flag_is_refused_before_dependencies_or_state_mutation(self):
        stderr = io.StringIO()
        with patch.object(acp.deps, "resolve_node_bin") as node, \
             patch.object(acp.deps, "resolve_aws_bin") as aws, \
             patch.object(acp.harness_mod, "resolve_harness") as resolve, \
             patch.object(acp.runtime, "invoke_verified") as invoke, \
             contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                acp.cmd_acp(self.cfg, ["new-ws", "--harness", "pi"])
        node.assert_not_called()
        aws.assert_not_called()
        resolve.assert_not_called()
        invoke.assert_not_called()
        self.assertFalse(workspace.workspace_exists(self.cfg, "new-ws"))
        message = stderr.getvalue()
        self.assertIn("no ACP agent", message)
        self.assertIn("sch shell", message)

    def test_existing_pi_workspace_is_refused_without_a_runtime_call(self):
        resolved = SimpleNamespace(
            sid="sid", harness="pi", storage="s3", epoch=1,
            identity="", was_created=False,
        )
        cfg = _make_cfg(self._tmp.name)
        cfg.acp_mirror_root = Path(self._tmp.name) / "mirrors"
        stderr = io.StringIO()
        with patch.object(acp.deps, "resolve_node_bin", return_value="/usr/bin/node"), \
             patch.object(acp.deps, "resolve_aws_bin", return_value="/usr/bin/aws"), \
             patch.object(acp.deps, "prepend_path"), \
             patch.object(acp.Path, "is_file", return_value=True), \
             patch.object(acp.Path, "is_dir", return_value=True), \
             patch.object(acp.Path, "mkdir"), \
             patch.object(acp.harness_mod, "resolve_harness", return_value=resolved), \
             patch.object(acp.runtime, "invoke_verified") as invoke, \
             patch.object(acp.procs, "run_foreground") as run, \
             contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                acp.cmd_acp(cfg, ["ws"])
        invoke.assert_not_called()
        run.assert_not_called()
        self.assertIn("no ACP agent", stderr.getvalue())

    def test_opencode_and_claude_keep_their_acp_agent(self):
        self.assertEqual(acp._HAS_ACP_AGENT, ("opencode", "claude"))


class AttachHarnessSelectionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = _make_cfg(self._tmp.name)
        tunnel = Path(self._tmp.name) / "tunnel"
        tunnel.mkdir()
        (tunnel / "attach.js").write_text("")
        (tunnel / "node_modules").mkdir()
        patcher = patch.object(attach.repo, "tunnel_dir", return_value=tunnel)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_new_workspace_defaults_to_opencode_not_global_default(self):
        # `attach` provisions, so resolution now looks the deployed runtime
        # version up: stub it out (the cfg fixture has no stack plumbing).
        with patch.object(harness_mod, "deployed_runtime_version", return_value="7"), \
             patch.object(attach.deps, "which_node", return_value="node"):
            with patch.object(attach.deps, "which_opencode", return_value="opencode"):
                # Stop before remote operations; the workspace selection
                # has already run.
                with patch.object(
                    attach, "runtime_arn", side_effect=RuntimeError("stop")
                ):
                    with self.assertRaisesRegex(RuntimeError, "stop"):
                        attach.cmd_attach(self.cfg, ["new-ws"])

        state = workspace.read_workspace_state(self.cfg, "new-ws")
        self.assertEqual(state.harness, "opencode")
        # A brand-new session records the runtime version it is born on, so
        # the next `attach` compares instead of adopting (design D3).
        self.assertEqual(state.runtime_version, "7")

    def test_claude_flag_is_rejected_without_creating_workspace(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                attach.cmd_attach(self.cfg, ["new-ws", "--harness", "claude"])
        self.assertFalse(workspace.workspace_exists(self.cfg, "new-ws"))

    def test_pi_flag_is_rejected_without_creating_workspace(self):
        """add-pi-harness task 5.7: pi has no client/server split either
        (spec: remote-ui-tunnel, "Attach rifiutato su workspace con harness pi")."""
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                attach.cmd_attach(self.cfg, ["new-ws", "--harness", "pi"])
        self.assertFalse(workspace.workspace_exists(self.cfg, "new-ws"))
        message = stderr.getvalue()
        self.assertIn("pi", message)
        self.assertIn("client/server", message)
        # `sch acp` must NOT be offered for pi: there is no ACP agent for it.
        self.assertNotIn("sch acp", message)
        self.assertIn("sch shell", message)

    def test_existing_pi_workspace_is_rejected_without_a_runtime_call(self):
        resolved = SimpleNamespace(
            sid="sid", harness="pi", storage="s3", epoch=1,
            identity="", was_created=False,
        )
        stderr = io.StringIO()
        with patch.object(attach.deps, "which_node", return_value="node"), \
             patch.object(attach.deps, "which_opencode", return_value="opencode"), \
             patch.object(attach.Path, "is_file", return_value=True), \
             patch.object(attach.Path, "is_dir", return_value=True), \
             patch.object(attach.harness_mod, "resolve_harness", return_value=resolved), \
             patch.object(attach.runtime, "invoke_verified") as invoke, \
             contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                attach.cmd_attach(self.cfg, ["ws"])
        invoke.assert_not_called()
        self.assertIn("pi", stderr.getvalue())

if __name__ == "__main__":
    unittest.main()
