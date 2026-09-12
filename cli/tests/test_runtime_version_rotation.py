"""Unit tests for the runtime-version session rotation
(add-task-liveness-safety, tasks 2.1-2.5; spec: runtime-provisioning,
"Runtime version", R15d).

Contract under test:
  - provisioning commands compare the recorded version with the deployed one
    and rotate on mismatch (new sid, epoch+1, harness/storage/gitNative kept)
  - an index without `runtimeVersion` adopts the current version, no rotation
  - a failed version lookup proceeds on the existing session with a warning
  - read-only commands (`status`, `list`, `fetch`) never look the version up
    and never rotate -- an observation must not steal a live writer claim
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

from sch import config as config_mod
from sch import harness, workspace, workspace_registry
from sch.commands import fetch as fetch_cmd
from sch.commands import list as list_cmd
from sch.commands import reset_session as reset_cmd
from sch.commands import status as status_cmd


def _make_cfg(tmp_dir, default_harness="opencode"):
    return SimpleNamespace(
        ws_dir=Path(tmp_dir) / "workspaces",
        default_harness=default_harness,
        default_storage="s3",
        region="eu-west-1",
        workspace_registry_url="",
    )


class _Boom(Exception):
    """Raised by the lookup stubs that must never be called."""


class DeployedRuntimeVersionTests(unittest.TestCase):
    """`config.deployed_runtime_version` (task 2.2): one control-plane call,
    never fatal."""

    def setUp(self):
        self.cfg = SimpleNamespace(region="eu-west-1")

    def test_queries_get_agent_runtime_with_the_runtime_id(self):
        completed = SimpleNamespace(returncode=0, stdout="33\n", stderr="")
        with patch.object(
            config_mod, "runtime_arn",
            return_value="arn:aws:bedrock-agentcore:eu-west-1:1:runtime/sch_dev-abc",
        ), patch.object(config_mod.subprocess, "run", return_value=completed) as run:
            self.assertEqual(config_mod.deployed_runtime_version(self.cfg), "33")
        argv = run.call_args[0][0]
        self.assertEqual(argv[:3], ["aws", "bedrock-agentcore-control", "get-agent-runtime"])
        self.assertIn("--agent-runtime-id", argv)
        self.assertEqual(argv[argv.index("--agent-runtime-id") + 1], "sch_dev-abc")
        self.assertEqual(argv[argv.index("--region") + 1], "eu-west-1")

    def test_failed_call_returns_empty_string(self):
        completed = SimpleNamespace(returncode=255, stdout="", stderr="AccessDenied")
        with patch.object(
            config_mod, "runtime_arn",
            return_value="arn:aws:bedrock-agentcore:eu-west-1:1:runtime/sch_dev-abc",
        ), patch.object(config_mod.subprocess, "run", return_value=completed):
            self.assertEqual(config_mod.deployed_runtime_version(self.cfg), "")

    def test_missing_aws_cli_returns_empty_string(self):
        with patch.object(
            config_mod, "runtime_arn",
            return_value="arn:aws:bedrock-agentcore:eu-west-1:1:runtime/sch_dev-abc",
        ), patch.object(
            config_mod.subprocess, "run", side_effect=FileNotFoundError("aws")
        ):
            self.assertEqual(config_mod.deployed_runtime_version(self.cfg), "")

    def test_empty_or_none_output_returns_empty_string(self):
        for stdout in ("", "\n", "None\n"):
            completed = SimpleNamespace(returncode=0, stdout=stdout, stderr="")
            with patch.object(
                config_mod, "runtime_arn",
                return_value="arn:aws:bedrock-agentcore:eu-west-1:1:runtime/sch_dev-abc",
            ), patch.object(config_mod.subprocess, "run", return_value=completed):
                self.assertEqual(
                    config_mod.deployed_runtime_version(self.cfg), "", repr(stdout)
                )

    def test_unresolvable_arn_does_not_terminate(self):
        # `runtime_arn` dies (SystemExit) when the stack cannot be resolved;
        # this helper must degrade instead of taking the command down.
        with patch.object(config_mod, "runtime_arn", side_effect=SystemExit(1)), \
             patch.object(config_mod.subprocess, "run", side_effect=_Boom):
            self.assertEqual(config_mod.deployed_runtime_version(self.cfg), "")

    def test_arn_without_resource_id_returns_empty_string(self):
        with patch.object(config_mod, "runtime_arn", return_value="not-an-arn"), \
             patch.object(config_mod.subprocess, "run", side_effect=_Boom):
            self.assertEqual(config_mod.deployed_runtime_version(self.cfg), "")


class RuntimeVersionRotationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = _make_cfg(self._tmp.name)

    def _seed(self, ws="ws", version="33", storage="s3", epoch=1, harness_name="claude"):
        workspace.save_workspace_state(
            self.cfg, ws, "sch-{}-old".format(ws), harness_name, "", storage, epoch,
            runtime_version=version,
        )

    @contextlib.contextmanager
    def _lookup(self, version):
        buf = io.StringIO()
        with patch.object(
            harness, "deployed_runtime_version", return_value=version
        ) as lookup, contextlib.redirect_stderr(buf):
            yield lookup, buf

    def test_mismatch_rotates_with_epoch_increment(self):
        self._seed(version="33", epoch=4)
        with self._lookup("34") as (lookup, err):
            resolved = harness.resolve_harness(
                self.cfg, "ws", "", "", provisioning=True
            )
        lookup.assert_called_once_with(self.cfg)
        self.assertNotEqual(resolved.sid, "sch-ws-old")
        self.assertTrue(resolved.sid.startswith("sch-ws-"))
        self.assertEqual(resolved.epoch, 5)
        self.assertEqual(resolved.harness, "claude")
        self.assertEqual(resolved.storage, "s3")

        state = workspace.read_workspace_state(self.cfg, "ws")
        self.assertEqual(state.sid, resolved.sid)
        self.assertEqual(state.epoch, 5)
        self.assertEqual(state.harness, "claude")
        self.assertEqual(state.storage, "s3")
        self.assertEqual(state.runtime_version, "34")
        self.assertTrue(
            workspace.read_status(self.cfg, "ws").startswith("session-rotated "),
            workspace.read_status(self.cfg, "ws"),
        )

    def test_rotation_message_names_both_versions_and_the_l2_consequence(self):
        self._seed(version="33")
        with self._lookup("34") as (_, err):
            harness.resolve_harness(self.cfg, "ws", "", "", provisioning=True)
        message = err.getvalue()
        self.assertIn("33", message)
        self.assertIn("34", message)
        self.assertIn("sch-ws-old", message)
        self.assertIn("checkpoint (L2)", message)
        self.assertNotIn("storage=session:", message)

    def test_session_storage_gets_the_extra_warning(self):
        self._seed(version="33", storage="session")
        with self._lookup("34") as (_, err):
            harness.resolve_harness(self.cfg, "ws", "", "", provisioning=True)
        self.assertIn("storage=session:", err.getvalue())

    def test_rotation_preserves_the_git_native_binding(self):
        self._seed(version="33")
        workspace.save_git_native_state(self.cfg, "ws", "feat/x", "abc123", "/repo")
        with self._lookup("34"):
            harness.resolve_harness(self.cfg, "ws", "", "", provisioning=True)
        state = workspace.read_workspace_state(self.cfg, "ws")
        self.assertEqual(state.git_native["branch"], "feat/x")
        self.assertEqual(state.git_native["baseSha"], "abc123")

    def test_matching_version_is_silent_and_keeps_the_session(self):
        self._seed(version="33", epoch=2)
        with self._lookup("33") as (_, err):
            resolved = harness.resolve_harness(
                self.cfg, "ws", "", "", provisioning=True
            )
        self.assertEqual(resolved.sid, "sch-ws-old")
        self.assertEqual(resolved.epoch, 2)
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(
            workspace.read_workspace_state(self.cfg, "ws").runtime_version, "33"
        )

    def test_index_without_version_adopts_current_without_rotating(self):
        # Pre-existing workspace (design D3, adopt-current): recording the
        # version must not cost the operator their live L1 state.
        workspace.save_workspace_state(
            self.cfg, "ws", "sch-ws-old", "claude", "", "s3", 3
        )
        self.assertEqual(
            workspace.read_workspace_state(self.cfg, "ws").runtime_version, ""
        )
        with self._lookup("34") as (_, err):
            resolved = harness.resolve_harness(
                self.cfg, "ws", "", "", provisioning=True
            )
        self.assertEqual(resolved.sid, "sch-ws-old")
        self.assertEqual(resolved.epoch, 3)
        self.assertEqual(err.getvalue(), "")
        state = workspace.read_workspace_state(self.cfg, "ws")
        self.assertEqual(state.sid, "sch-ws-old")
        self.assertEqual(state.epoch, 3)
        self.assertEqual(state.runtime_version, "34")
        # ...and the comparison is effective from the next invocation.
        with self._lookup("35"):
            rotated = harness.resolve_harness(
                self.cfg, "ws", "", "", provisioning=True
            )
        self.assertNotEqual(rotated.sid, "sch-ws-old")

    def test_legacy_index_adopts_current_without_rotating(self):
        # Bare-sid legacy file: reconciled AND version-adopted, same sid.
        self.cfg.ws_dir.mkdir(parents=True)
        (self.cfg.ws_dir / "legacy").write_text("sch-legacy-old")
        with self._lookup("34"):
            resolved = harness.resolve_harness(
                self.cfg, "legacy", "", "", provisioning=True
            )
        self.assertEqual(resolved.sid, "sch-legacy-old")
        state = workspace.read_workspace_state(self.cfg, "legacy")
        self.assertEqual(state.sid, "sch-legacy-old")
        self.assertEqual(state.runtime_version, "34")

    def test_legacy_index_with_recorded_version_still_rotates(self):
        (self.cfg.ws_dir).mkdir(parents=True)
        (self.cfg.ws_dir / "half").write_text(json.dumps({
            "runtimeSessionId": "sch-half-old", "harness": "claude",
            "sessionEpoch": 2, "runtimeVersion": "33",
        }))
        with self._lookup("34"):
            resolved = harness.resolve_harness(
                self.cfg, "half", "", "", provisioning=True
            )
        self.assertNotEqual(resolved.sid, "sch-half-old")
        self.assertEqual(resolved.epoch, 3)
        self.assertEqual(
            workspace.read_workspace_state(self.cfg, "half").runtime_version, "34"
        )

    def test_new_workspace_records_the_version_it_is_born_on(self):
        with self._lookup("34"):
            resolved = harness.resolve_harness(
                self.cfg, "fresh", "opencode", "", provisioning=True
            )
        self.assertTrue(resolved.was_created)
        state = workspace.read_workspace_state(self.cfg, "fresh")
        self.assertEqual(state.sid, resolved.sid)
        self.assertEqual(state.epoch, 1)
        self.assertEqual(state.runtime_version, "34")

    def test_failed_lookup_warns_and_keeps_the_session(self):
        self._seed(version="33", epoch=2)
        with self._lookup("") as (_, err):
            resolved = harness.resolve_harness(
                self.cfg, "ws", "", "", provisioning=True
            )
        self.assertEqual(resolved.sid, "sch-ws-old")
        self.assertEqual(resolved.epoch, 2)
        self.assertIn("warning", err.getvalue())
        self.assertIn("cannot determine the deployed runtime version", err.getvalue())
        # The recorded version is left exactly as it was: a failed lookup is
        # not evidence about the deployed version.
        self.assertEqual(
            workspace.read_workspace_state(self.cfg, "ws").runtime_version, "33"
        )


class ReadOnlyCommandsNeverRotateTests(unittest.TestCase):
    """Task 2.5: `status`, `list` and `fetch` MUST NOT look the deployed
    version up nor rotate. A live TUI/task on the existing session must not
    lose its writer claim because somebody observed the workspace."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = _make_cfg(self._tmp.name)
        self.cfg.config_dir = Path(self._tmp.name) / "config"
        workspace.save_workspace_state(
            self.cfg, "ws", "sch-ws-live", "claude", "", "s3", 4,
            runtime_version="1",  # deliberately stale: a lookup WOULD rotate
        )
        self.index_before = (self.cfg.ws_dir / "ws").read_text()

    @contextlib.contextmanager
    def _forbid_lookup(self):
        """Any version lookup at all fails the test, whatever its outcome."""
        with patch.object(
            harness, "deployed_runtime_version", side_effect=_Boom
        ), patch.object(
            config_mod, "deployed_runtime_version", side_effect=_Boom
        ), patch.object(
            workspace, "generate_session_id", side_effect=_Boom
        ):
            yield

    def _assert_session_untouched(self):
        state = workspace.read_workspace_state(self.cfg, "ws")
        self.assertEqual(state.sid, "sch-ws-live")
        self.assertEqual(state.epoch, 4)
        self.assertEqual(state.runtime_version, "1")
        self.assertEqual((self.cfg.ws_dir / "ws").read_text(), self.index_before)

    def test_resolve_harness_defaults_to_non_provisioning(self):
        with self._forbid_lookup():
            resolved = harness.resolve_harness(self.cfg, "ws", "")
        self.assertEqual(resolved.sid, "sch-ws-live")
        self.assertEqual(resolved.epoch, 4)
        self._assert_session_untouched()

    def test_status_does_not_look_up_nor_rotate(self):
        buf = io.StringIO()
        with self._forbid_lookup(), patch.object(
            status_cmd, "read_offline_status",
            return_value='{"state":"running","heartbeat_utc":"2020-01-01T00:00:00Z"}',
        ), patch.object(status_cmd.runtime, "invoke_verified") as invoke, \
                contextlib.redirect_stdout(buf):
            rc = status_cmd.cmd_status(self.cfg, ["ws"])
        # Still classifies staleness (exit 3) - it just never provisions.
        self.assertEqual(rc, status_cmd.EXIT_RUNNING_STALE)
        invoke.assert_not_called()
        self._assert_session_untouched()

    def test_list_does_not_look_up_nor_rotate(self):
        buf = io.StringIO()
        with self._forbid_lookup(), contextlib.redirect_stdout(buf):
            rc = list_cmd.cmd_list(self.cfg, [])
        self.assertEqual(rc, 0)
        self.assertIn("sch-ws-live", buf.getvalue())
        self._assert_session_untouched()

    def test_fetch_does_not_look_up_nor_rotate(self):
        # `fetch` dies early on a workspace that was never seeded git-native;
        # what matters is that no version lookup happened on the way there.
        with self._forbid_lookup(), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                fetch_cmd.cmd_fetch(self.cfg, ["ws"])
        self._assert_session_untouched()

    def test_fetch_resolves_the_harness_without_the_provisioning_flag(self):
        # Guardrail on the call itself, independent of how far `fetch` gets.
        with patch.object(
            fetch_cmd.harness_mod, "resolve_harness", side_effect=_Boom
        ) as resolve:
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises((_Boom, SystemExit)):
                    fetch_cmd.cmd_fetch(self.cfg, ["ws"])
        for call in resolve.call_args_list:
            self.assertNotIn("provisioning", call.kwargs)
            self.assertLessEqual(len(call.args), 3)


class RegistryModeRotationTests(unittest.TestCase):
    """Registry mode: the mapping is authoritative remotely, so a rotation
    goes through the registry and is mirrored (with the version) locally."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = _make_cfg(self._tmp.name)
        self.cfg.workspace_registry_url = (
            "https://example.execute-api.eu-west-1.amazonaws.com/v1"
        )

    def _remote(self, sid="sid-1", epoch=2):
        return workspace_registry.RegistryWorkspace({
            "logicalWorkspace": "ws",
            "runtimeSessionId": sid,
            "harness": "opencode",
            "workspaceIdentity": "ws-owner-a",
            "storage": "s3",
            "sessionEpoch": epoch,
        })

    def _seed_cache(self, sid="sid-1", version="33", epoch=2):
        workspace.save_workspace_state(
            self.cfg, "ws", sid, "opencode", "ws-owner-a", "s3", epoch,
            runtime_version=version,
        )

    def test_mismatch_rotates_through_the_registry(self):
        self._seed_cache(sid="sid-1", version="33")
        with patch.object(
            workspace_registry, "resolve_or_die", return_value=self._remote("sid-1", 2)
        ), patch.object(
            workspace_registry, "rotate", return_value=self._remote("sid-2", 3)
        ) as rotate, patch.object(
            harness, "deployed_runtime_version", return_value="34"
        ), contextlib.redirect_stderr(io.StringIO()):
            resolved = harness.resolve_harness(
                self.cfg, "ws", "", "", provisioning=True
            )
        rotate.assert_called_once_with(self.cfg, "ws")
        self.assertEqual(resolved.sid, "sid-2")
        self.assertEqual(resolved.epoch, 3)
        cached = workspace.read_workspace_state(self.cfg, "ws")
        self.assertEqual(cached.sid, "sid-2")
        self.assertEqual(cached.epoch, 3)
        self.assertEqual(cached.runtime_version, "34")

    def test_registry_rotation_failure_warns_and_keeps_the_session(self):
        self._seed_cache(sid="sid-1", version="33")
        err = io.StringIO()
        with patch.object(
            workspace_registry, "resolve_or_die", return_value=self._remote("sid-1", 2)
        ), patch.object(
            workspace_registry, "rotate", side_effect=RuntimeError("registry down")
        ), patch.object(
            harness, "deployed_runtime_version", return_value="34"
        ), contextlib.redirect_stderr(err):
            resolved = harness.resolve_harness(
                self.cfg, "ws", "", "", provisioning=True
            )
        self.assertEqual(resolved.sid, "sid-1")
        self.assertIn("registry down", err.getvalue())
        self.assertEqual(
            workspace.read_workspace_state(self.cfg, "ws").runtime_version, "33"
        )

    def test_a_different_remote_session_adopts_instead_of_rotating(self):
        # The cached version describes the session this client last saw; when
        # the registry hands out another one, that version says nothing about
        # it -- adopt the current version rather than rotating a session the
        # client has never provisioned.
        self._seed_cache(sid="sid-1", version="33")
        with patch.object(
            workspace_registry, "resolve_or_die", return_value=self._remote("sid-9", 5)
        ), patch.object(
            workspace_registry, "rotate", side_effect=_Boom
        ), patch.object(
            harness, "deployed_runtime_version", return_value="34"
        ), contextlib.redirect_stderr(io.StringIO()):
            resolved = harness.resolve_harness(
                self.cfg, "ws", "", "", provisioning=True
            )
        self.assertEqual(resolved.sid, "sid-9")
        cached = workspace.read_workspace_state(self.cfg, "ws")
        self.assertEqual(cached.sid, "sid-9")
        self.assertEqual(cached.runtime_version, "34")

    def test_read_only_resolution_never_looks_the_version_up(self):
        self._seed_cache(sid="sid-1", version="1")
        with patch.object(
            workspace_registry, "resolve_or_die", return_value=self._remote("sid-1", 2)
        ), patch.object(
            workspace_registry, "rotate", side_effect=_Boom
        ), patch.object(
            harness, "deployed_runtime_version", side_effect=_Boom
        ):
            resolved = harness.resolve_harness(self.cfg, "ws", "")
        self.assertEqual(resolved.sid, "sid-1")
        cached = workspace.read_workspace_state(self.cfg, "ws")
        self.assertEqual(cached.sid, "sid-1")
        # The mirror write preserves the recorded version untouched.
        self.assertEqual(cached.runtime_version, "1")


class ResetSessionDropsRecordedVersionTests(unittest.TestCase):
    """A manual `reset-session` must not leave the old version pinned: the
    next provisioning command would otherwise rotate a session the operator
    has just created on purpose."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = _make_cfg(self._tmp.name)
        workspace.save_workspace_state(
            self.cfg, "ws", "sch-ws-old", "claude", "", "s3", 2,
            runtime_version="33",
        )

    def test_reset_drops_the_version_and_the_next_provisioning_adopts(self):
        with patch.object(reset_cmd, "input", create=True, return_value="y"), \
             contextlib.redirect_stderr(io.StringIO()):
            rc = reset_cmd.cmd_reset_session(self.cfg, ["ws"])
        self.assertEqual(rc, 0)
        state = workspace.read_workspace_state(self.cfg, "ws")
        self.assertNotEqual(state.sid, "sch-ws-old")
        self.assertEqual(state.epoch, 3)
        self.assertEqual(state.harness, "claude")
        self.assertEqual(state.runtime_version, "")
        reset_sid = state.sid

        # First provisioning after the reset: adopt-current, no rotation.
        with patch.object(
            harness, "deployed_runtime_version", return_value="34"
        ), contextlib.redirect_stderr(io.StringIO()):
            resolved = harness.resolve_harness(
                self.cfg, "ws", "", "", provisioning=True
            )
        self.assertEqual(resolved.sid, reset_sid)
        self.assertEqual(resolved.epoch, 3)
        self.assertEqual(
            workspace.read_workspace_state(self.cfg, "ws").runtime_version, "34"
        )


if __name__ == "__main__":
    unittest.main()
