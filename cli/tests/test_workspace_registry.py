"""Unit tests for registry-mode client, cache, and identity propagation."""

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

from sch import harness, workspace, workspace_registry
from sch.commands import list as list_cmd


def _record(name="same-name", sid="sid-1", identity="ws-owner-a"):
    return {
        "logicalWorkspace": name,
        "runtimeSessionId": sid,
        "harness": "opencode",
        "workspaceIdentity": identity,
        "storage": "s3",
    }


def _cfg(tmp_dir, registry_url="https://example.execute-api.eu-west-1.amazonaws.com/v1"):
    return SimpleNamespace(
        ws_dir=Path(tmp_dir) / "workspaces",
        default_harness="claude",
        default_storage="s3",
        workspace_registry_url=registry_url,
        region="eu-west-1",
    )


class RegistryModeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = _cfg(self._tmp.name)

    def test_resolve_updates_local_cache_with_identity(self):
        remote = workspace_registry.RegistryWorkspace(_record(), was_created=True)
        with patch("sch.workspace_registry.resolve_or_die", return_value=remote):
            resolved = harness.resolve_harness(self.cfg, "same-name", "opencode")
        cached = workspace.read_workspace_state(self.cfg, "same-name")
        self.assertEqual(resolved.sid, "sid-1")
        self.assertEqual(resolved.identity, "ws-owner-a")
        self.assertEqual(cached.identity, "ws-owner-a")
        self.assertEqual(cached.storage, "s3")

    def test_registry_receives_creation_default_separately(self):
        remote = workspace_registry.RegistryWorkspace(_record(), was_created=True)
        with patch("sch.workspace_registry.resolve_or_die", return_value=remote) as resolve:
            harness.resolve_harness(self.cfg, "same-name", "")
        resolve.assert_called_once_with(
            self.cfg, "same-name", "", "", "s3"
        )

    def test_registry_failure_does_not_fall_back_to_cached_session(self):
        workspace.save_workspace_state(self.cfg, "same-name", "cached-sid", "opencode", "old")
        with patch("sch.workspace_registry.resolve_or_die", side_effect=SystemExit(1)):
            with self.assertRaises(SystemExit):
                harness.resolve_harness(self.cfg, "same-name", "")

    def test_list_uses_remote_owner_records_and_ignores_other_cache_status(self):
        workspace.save_workspace_state(self.cfg, "same-name", "stale", "opencode", "ws-owner-b")
        workspace.mark_status(self.cfg, "same-name", "stale-other-owner")
        remote = workspace_registry.RegistryWorkspace(_record())
        with patch("sch.workspace_registry.list_workspaces", return_value=[remote]):
            with patch("sys.stdout") as stdout:
                list_cmd.cmd_list(self.cfg, [])
        output = "".join(call.args[0] + call.kwargs.get("end", "\n") for call in stdout.write.call_args_list)
        self.assertIn("sid-1", output)
        self.assertNotIn("stale-other-owner", output)


class RegistryRecordValidationTests(unittest.TestCase):
    def test_rejects_incomplete_control_plane_response(self):
        with self.assertRaises(ValueError):
            workspace_registry.RegistryWorkspace({"logicalWorkspace": "ws"})

    def test_rejects_unsafe_workspace_identity(self):
        with self.assertRaises(ValueError):
            workspace_registry.RegistryWorkspace(_record(identity="../legacy"))

    def test_unset_endpoint_preserves_legacy_mode(self):
        self.assertFalse(workspace_registry.enabled(_cfg("/tmp", "")))

    def test_legacy_registry_record_defaults_to_session(self):
        record = _record()
        record.pop("storage")
        self.assertEqual(workspace_registry.RegistryWorkspace(record).storage, "session")

    def test_delete_client_uses_delete_without_resolve(self):
        cfg = _cfg("/tmp")
        with patch("sch.workspace_registry._request", return_value={"workspace": {"storage": "s3"}}) as request:
            self.assertEqual(workspace_registry.delete(cfg, "same-name")["storage"], "s3")
        request.assert_called_once_with(cfg, "DELETE", "/workspaces/same-name")

    def test_bulk_delete_client_uses_owner_scoped_endpoint(self):
        cfg = _cfg("/tmp")
        with patch("sch.workspace_registry._request", return_value={"results": []}) as request:
            self.assertEqual(workspace_registry.delete_all(cfg)["results"], [])
        request.assert_called_once_with(cfg, "DELETE", "/workspaces")

    def test_registry_rejects_present_invalid_storage(self):
        for value in ("efs", None, ["s3"]):
            record = _record()
            record["storage"] = value
            with self.assertRaises(ValueError):
                workspace_registry.RegistryWorkspace(record)


if __name__ == "__main__":
    unittest.main()
