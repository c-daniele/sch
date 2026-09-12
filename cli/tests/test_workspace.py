"""Unit tests for `sch.workspace`: legacy index format tolerance,
save/load round-trips, workspace-name validation, and the `.status.<ws>`
bookkeeping format.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

_CLI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _CLI_DIR not in sys.path:
    sys.path.insert(0, _CLI_DIR)

from sch import workspace


def _make_cfg(tmp_dir):
    return SimpleNamespace(ws_dir=Path(tmp_dir) / "workspaces")


class ReadWorkspaceStateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = _make_cfg(self._tmp.name)

    def test_missing_file_returns_none(self):
        self.assertIsNone(workspace.read_workspace_state(self.cfg, "nope"))

    def test_legacy_bare_sid_string(self):
        self.cfg.ws_dir.mkdir(parents=True)
        (self.cfg.ws_dir / "ws1").write_text("sch-ws1-11111111-1111-1111-1111-111111111111")
        state = workspace.read_workspace_state(self.cfg, "ws1")
        self.assertEqual(state.sid, "sch-ws1-11111111-1111-1111-1111-111111111111")
        self.assertEqual(state.harness, "")

    def test_legacy_bare_sid_string_with_whitespace_is_stripped(self):
        self.cfg.ws_dir.mkdir(parents=True)
        (self.cfg.ws_dir / "ws1b").write_text("  sch-ws1b-sid  \n")
        state = workspace.read_workspace_state(self.cfg, "ws1b")
        self.assertEqual(state.sid, "sch-ws1b-sid")

    def test_legacy_json_scalar(self):
        self.cfg.ws_dir.mkdir(parents=True)
        (self.cfg.ws_dir / "ws2").write_text(json.dumps("sch-ws2-sid"))
        state = workspace.read_workspace_state(self.cfg, "ws2")
        self.assertEqual(state.sid, "sch-ws2-sid")
        self.assertEqual(state.harness, "")

    def test_legacy_object_with_session_id_key(self):
        self.cfg.ws_dir.mkdir(parents=True)
        (self.cfg.ws_dir / "ws3").write_text(json.dumps({"sessionId": "sch-ws3-sid"}))
        state = workspace.read_workspace_state(self.cfg, "ws3")
        self.assertEqual(state.sid, "sch-ws3-sid")
        self.assertEqual(state.harness, "")

    def test_current_format_with_harness(self):
        self.cfg.ws_dir.mkdir(parents=True)
        (self.cfg.ws_dir / "ws4").write_text(
            json.dumps({"runtimeSessionId": "sid4", "harness": "opencode"})
        )
        state = workspace.read_workspace_state(self.cfg, "ws4")
        self.assertEqual(state.sid, "sid4")
        self.assertEqual(state.harness, "opencode")
        self.assertEqual(state.storage, "")

    def test_current_format_with_storage(self):
        self.cfg.ws_dir.mkdir(parents=True)
        (self.cfg.ws_dir / "ws-storage").write_text(json.dumps({
            "runtimeSessionId": "sid-storage", "harness": "opencode", "storage": "s3",
        }))
        state = workspace.read_workspace_state(self.cfg, "ws-storage")
        self.assertEqual(state.storage, "s3")

    def test_runtime_session_id_takes_precedence_over_session_id(self):
        self.cfg.ws_dir.mkdir(parents=True)
        (self.cfg.ws_dir / "ws5").write_text(
            json.dumps({"runtimeSessionId": "new-sid", "sessionId": "old-sid"})
        )
        state = workspace.read_workspace_state(self.cfg, "ws5")
        self.assertEqual(state.sid, "new-sid")

    def test_session_epoch_round_trips(self):
        workspace.save_workspace_state(
            self.cfg, "epoch", "sid", "opencode", storage="s3", epoch=7
        )
        self.assertEqual(workspace.read_workspace_state(self.cfg, "epoch").epoch, 7)

    def test_invalid_persisted_storage_is_rejected(self):
        self.cfg.ws_dir.mkdir(parents=True)
        for index, value in enumerate(("efs", None, ["s3"])):
            ws = "bad-storage-{}".format(index)
            (self.cfg.ws_dir / ws).write_text(json.dumps({
                "runtimeSessionId": "sid", "harness": "opencode", "storage": value,
            }))
            with self.assertRaises(SystemExit):
                workspace.validate_storage_state(
                    workspace.read_workspace_state(self.cfg, ws), ws
                )


class SaveWorkspaceStateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = _make_cfg(self._tmp.name)

    def test_round_trip_with_harness(self):
        workspace.save_workspace_state(self.cfg, "ws", "sid-1", "claude", storage="s3")
        state = workspace.read_workspace_state(self.cfg, "ws")
        self.assertEqual(state.sid, "sid-1")
        self.assertEqual(state.harness, "claude")
        self.assertEqual(state.storage, "s3")

    def test_written_shape_matches_reference_format(self):
        workspace.save_workspace_state(self.cfg, "ws", "sid-1", "claude")
        raw = (self.cfg.ws_dir / "ws").read_text()
        self.assertEqual(
            json.loads(raw), {
                "runtimeSessionId": "sid-1", "harness": "claude",
                "sessionEpoch": 0,
            }
        )

    def test_empty_harness_omits_key(self):
        workspace.save_workspace_state(self.cfg, "ws", "sid-1", "")
        raw = (self.cfg.ws_dir / "ws").read_text()
        self.assertNotIn("harness", json.loads(raw))


class RuntimeVersionIndexTests(unittest.TestCase):
    """`runtimeVersion` in the local index (add-task-liveness-safety task
    2.1): written when a session id is (re)generated, preserved by every
    other writer exactly like `gitNative`."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = _make_cfg(self._tmp.name)

    def test_absent_by_default(self):
        workspace.save_workspace_state(self.cfg, "ws", "sid-1", "claude", storage="s3")
        raw = json.loads((self.cfg.ws_dir / "ws").read_text())
        self.assertNotIn("runtimeVersion", raw)
        self.assertEqual(
            workspace.read_workspace_state(self.cfg, "ws").runtime_version, ""
        )

    def test_recorded_and_exposed(self):
        workspace.save_workspace_state(
            self.cfg, "ws", "sid-1", "claude", storage="s3", runtime_version="33"
        )
        raw = json.loads((self.cfg.ws_dir / "ws").read_text())
        self.assertEqual(raw["runtimeVersion"], "33")
        self.assertEqual(
            workspace.read_workspace_state(self.cfg, "ws").runtime_version, "33"
        )

    def test_preserved_by_a_later_save_that_does_not_name_it(self):
        workspace.save_workspace_state(
            self.cfg, "ws", "sid-1", "claude", storage="s3", epoch=1,
            runtime_version="33",
        )
        # e.g. `sch list` in registry mode re-persisting a resolved record.
        workspace.save_workspace_state(
            self.cfg, "ws", "sid-1", "claude", storage="s3", epoch=1
        )
        self.assertEqual(
            workspace.read_workspace_state(self.cfg, "ws").runtime_version, "33"
        )

    def test_overwritten_when_a_new_version_is_named(self):
        workspace.save_workspace_state(
            self.cfg, "ws", "sid-1", "claude", storage="s3", runtime_version="33"
        )
        workspace.save_workspace_state(
            self.cfg, "ws", "sid-2", "claude", storage="s3", runtime_version="34"
        )
        state = workspace.read_workspace_state(self.cfg, "ws")
        self.assertEqual(state.sid, "sid-2")
        self.assertEqual(state.runtime_version, "34")

    def test_preserved_alongside_git_native_binding(self):
        workspace.save_workspace_state(
            self.cfg, "ws", "sid-1", "claude", storage="s3", runtime_version="33"
        )
        workspace.save_git_native_state(self.cfg, "ws", "feat/x", "abc123", "/repo")
        state = workspace.read_workspace_state(self.cfg, "ws")
        self.assertEqual(state.runtime_version, "33")
        self.assertEqual(state.git_native["branch"], "feat/x")
        # ...and a rotation keeps the binding while moving the version.
        workspace.save_workspace_state(
            self.cfg, "ws", "sid-2", "claude", storage="s3", runtime_version="34"
        )
        state = workspace.read_workspace_state(self.cfg, "ws")
        self.assertEqual(state.runtime_version, "34")
        self.assertEqual(state.git_native["branch"], "feat/x")

    def test_explicit_empty_string_drops_the_recorded_version(self):
        # What `sch reset-session` wants: the new sid has not been
        # provisioned yet, so the next provisioning command records the
        # version it actually boots on (adopt-current, no extra rotation).
        workspace.save_workspace_state(
            self.cfg, "ws", "sid-1", "claude", storage="s3", runtime_version="33"
        )
        workspace.save_workspace_state(
            self.cfg, "ws", "sid-2", "claude", storage="s3", runtime_version=""
        )
        raw = json.loads((self.cfg.ws_dir / "ws").read_text())
        self.assertNotIn("runtimeVersion", raw)
        self.assertEqual(
            workspace.read_workspace_state(self.cfg, "ws").runtime_version, ""
        )

    def test_numeric_value_in_the_index_is_read_as_a_string(self):
        self.cfg.ws_dir.mkdir(parents=True)
        (self.cfg.ws_dir / "ws").write_text(json.dumps({
            "runtimeSessionId": "sid-1", "harness": "claude", "storage": "s3",
            "runtimeVersion": 33,
        }))
        self.assertEqual(
            workspace.read_workspace_state(self.cfg, "ws").runtime_version, "33"
        )

    def test_malformed_value_in_the_index_is_ignored(self):
        self.cfg.ws_dir.mkdir(parents=True)
        for value in ({"v": 1}, [], True, None):
            (self.cfg.ws_dir / "ws").write_text(json.dumps({
                "runtimeSessionId": "sid-1", "harness": "claude",
                "storage": "s3", "runtimeVersion": value,
            }))
            self.assertEqual(
                workspace.read_workspace_state(self.cfg, "ws").runtime_version,
                "",
                repr(value),
            )


class WorkspaceNameValidationTests(unittest.TestCase):
    def test_accepts_valid_names(self):
        for name in ("a", "my-ws", "my_ws", "WS123", "a1-b_2"):
            self.assertEqual(workspace.validate_workspace_name(name), name)

    def test_rejects_invalid_names(self):
        for name in ("", "-leading-dash", "_leading-underscore", "has space", "has/slash"):
            with self.assertRaises(SystemExit):
                workspace.validate_workspace_name(name)


class GenerateSessionIdTests(unittest.TestCase):
    def test_format(self):
        sid = workspace.generate_session_id("myws")
        self.assertTrue(sid.startswith("sch-myws-"))
        uuid_part = sid[len("sch-myws-") :]
        self.assertRegex(
            uuid_part,
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        )

    def test_unique_across_calls(self):
        self.assertNotEqual(
            workspace.generate_session_id("ws"), workspace.generate_session_id("ws")
        )


class StatusBookkeepingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = _make_cfg(self._tmp.name)

    def test_mark_status_format(self):
        workspace.mark_status(self.cfg, "ws", "shell-opened")
        content = (self.cfg.ws_dir / ".status.ws").read_text().strip()
        self.assertRegex(
            content, r"^shell-opened \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
        )

    def test_read_status_default_when_missing(self):
        self.assertEqual(workspace.read_status(self.cfg, "missing"), "created")

    def test_read_status_returns_recorded_value(self):
        workspace.mark_status(self.cfg, "ws", "stopped")
        self.assertTrue(workspace.read_status(self.cfg, "ws").startswith("stopped "))


class ListWorkspaceNamesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = _make_cfg(self._tmp.name)

    def test_empty_when_dir_missing(self):
        self.assertEqual(workspace.list_workspace_names(self.cfg), [])

    def test_excludes_status_dotfiles_and_sorts(self):
        workspace.save_workspace_state(self.cfg, "bravo", "sid-b", "opencode")
        workspace.save_workspace_state(self.cfg, "alpha", "sid-a", "claude")
        workspace.mark_status(self.cfg, "alpha", "shell-opened")
        self.assertEqual(workspace.list_workspace_names(self.cfg), ["alpha", "bravo"])


if __name__ == "__main__":
    unittest.main()
