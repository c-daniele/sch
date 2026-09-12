"""Unit tests for `sch.harness`: new-workspace defaulting, congruent vs.
divergent `--harness`, and legacy upgrade-reconcile/handoff.
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

from sch import harness, workspace


def _make_cfg(tmp_dir, default_harness="opencode"):
    return SimpleNamespace(
        ws_dir=Path(tmp_dir) / "workspaces", default_harness=default_harness,
        default_storage="s3",
    )


class NewWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = _make_cfg(self._tmp.name)

    def test_uses_explicit_flag(self):
        resolved = harness.resolve_harness(self.cfg, "ws1", "opencode")
        self.assertEqual(resolved.harness, "opencode")
        self.assertTrue(resolved.was_created)
        state = workspace.read_workspace_state(self.cfg, "ws1")
        self.assertEqual(state.harness, "opencode")
        self.assertEqual(state.sid, resolved.sid)
        self.assertEqual(state.storage, "s3")
        self.assertEqual(state.epoch, 1)

    def test_explicit_session_storage(self):
        resolved = harness.resolve_harness(self.cfg, "ws-session", "opencode", "session")
        self.assertEqual(resolved.storage, "session")

    def test_invalid_storage_does_not_create_workspace(self):
        with self.assertRaises(SystemExit):
            harness.resolve_harness(self.cfg, "ws-invalid-storage", "opencode", "bad")
        self.assertFalse(workspace.workspace_exists(self.cfg, "ws-invalid-storage"))

    def test_uses_default_when_flag_omitted(self):
        resolved = harness.resolve_harness(self.cfg, "ws2", "")
        self.assertEqual(resolved.harness, "opencode")

    def test_honors_custom_default(self):
        cfg = _make_cfg(self._tmp.name, default_harness="claude")
        resolved = harness.resolve_harness(cfg, "ws3", "")
        self.assertEqual(resolved.harness, "claude")

    def test_rejects_invalid_harness_without_creating_workspace(self):
        with self.assertRaises(SystemExit):
            harness.resolve_harness(self.cfg, "ws4", "bogus")
        self.assertFalse(workspace.workspace_exists(self.cfg, "ws4"))


class ExistingWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = _make_cfg(self._tmp.name)

    def test_congruent_flag_is_accepted(self):
        first = harness.resolve_harness(self.cfg, "ws", "opencode")
        resolved = harness.resolve_harness(self.cfg, "ws", "opencode")
        self.assertEqual(resolved.harness, "opencode")
        self.assertEqual(resolved.sid, first.sid)
        self.assertFalse(resolved.was_created)

    def test_no_flag_reuses_persisted_harness(self):
        first = harness.resolve_harness(self.cfg, "ws", "claude")
        resolved = harness.resolve_harness(self.cfg, "ws", "")
        self.assertEqual(resolved.harness, "claude")
        self.assertEqual(resolved.sid, first.sid)

    def test_divergent_flag_rejected_without_mutating_index(self):
        first = harness.resolve_harness(self.cfg, "ws", "opencode")
        raw_before = (self.cfg.ws_dir / "ws").read_text()

        with self.assertRaises(SystemExit):
            harness.resolve_harness(self.cfg, "ws", "claude")

        raw_after = (self.cfg.ws_dir / "ws").read_text()
        self.assertEqual(raw_before, raw_after)
        state = workspace.read_workspace_state(self.cfg, "ws")
        self.assertEqual(state.harness, "opencode")
        self.assertEqual(state.sid, first.sid)

    def test_divergent_storage_rejected_without_mutating_index(self):
        harness.resolve_harness(self.cfg, "ws-storage", "opencode", "s3")
        raw_before = (self.cfg.ws_dir / "ws-storage").read_text()
        with self.assertRaises(SystemExit):
            harness.resolve_harness(self.cfg, "ws-storage", "opencode", "session")
        self.assertEqual(raw_before, (self.cfg.ws_dir / "ws-storage").read_text())

    def test_missing_session_id_dies(self):
        self.cfg.ws_dir.mkdir(parents=True)
        (self.cfg.ws_dir / "broken").write_text(json.dumps({"harness": "opencode"}))
        with self.assertRaises(SystemExit):
            harness.resolve_harness(self.cfg, "broken", "")


class LegacyWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = _make_cfg(self._tmp.name)
        self.cfg.ws_dir.mkdir(parents=True)

    def _write_legacy(self, ws, sid):
        (self.cfg.ws_dir / ws).write_text(sid)

    def test_no_flag_reconciles_to_opencode_preserving_sid(self):
        self._write_legacy("legacy1", "sch-legacy1-aaaa")
        resolved = harness.resolve_harness(self.cfg, "legacy1", "")
        self.assertEqual(resolved.harness, "opencode")
        self.assertEqual(resolved.sid, "sch-legacy1-aaaa")
        state = workspace.read_workspace_state(self.cfg, "legacy1")
        self.assertEqual(state.harness, "opencode")
        self.assertEqual(state.sid, "sch-legacy1-aaaa")
        self.assertEqual(state.storage, "session")

    def test_existing_harness_without_storage_is_preserved(self):
        (self.cfg.ws_dir / "legacy-storage").write_text(json.dumps({
            "runtimeSessionId": "legacy-storage-sid", "harness": "claude",
        }))
        resolved = harness.resolve_harness(self.cfg, "legacy-storage", "")
        self.assertEqual(resolved.harness, "claude")
        self.assertEqual(resolved.storage, "session")

    def test_explicit_opencode_flag_reconciles_to_opencode(self):
        self._write_legacy("legacy2", "sch-legacy2-bbbb")
        resolved = harness.resolve_harness(self.cfg, "legacy2", "opencode")
        self.assertEqual(resolved.harness, "opencode")
        self.assertEqual(resolved.sid, "sch-legacy2-bbbb")

    def test_explicit_claude_flag_is_upgrade_handoff(self):
        self._write_legacy("legacy3", "sch-legacy3-cccc")
        resolved = harness.resolve_harness(self.cfg, "legacy3", "claude")
        self.assertEqual(resolved.harness, "claude")
        self.assertEqual(resolved.sid, "sch-legacy3-cccc")
        state = workspace.read_workspace_state(self.cfg, "legacy3")
        self.assertEqual(state.harness, "claude")

    def test_invalid_flag_on_legacy_dies_without_mutation(self):
        self._write_legacy("legacy4", "sch-legacy4-dddd")
        raw_before = (self.cfg.ws_dir / "legacy4").read_text()
        with self.assertRaises(SystemExit):
            harness.resolve_harness(self.cfg, "legacy4", "bogus")
        raw_after = (self.cfg.ws_dir / "legacy4").read_text()
        self.assertEqual(raw_before, raw_after)


class PiHarnessTests(unittest.TestCase):
    """add-pi-harness task 5.7: `pi` is a third enum value with the SAME
    resolution/persistence/exclusivity rules, and the legacy reconcile default
    is unchanged (spec: harness-selection)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = _make_cfg(self._tmp.name)

    def test_pi_is_a_valid_harness_value(self):
        self.assertIn("pi", harness.VALID_HARNESSES)
        self.assertEqual(len(harness.VALID_HARNESSES), 3)

    def test_explicit_pi_flag_is_persisted_for_a_new_workspace(self):
        resolved = harness.resolve_harness(self.cfg, "piws", "pi")
        self.assertEqual(resolved.harness, "pi")
        self.assertTrue(resolved.was_created)
        state = workspace.read_workspace_state(self.cfg, "piws")
        self.assertEqual(state.harness, "pi")
        self.assertEqual(state.sid, resolved.sid)

    def test_persisted_pi_is_reused_without_the_flag(self):
        first = harness.resolve_harness(self.cfg, "piws", "pi")
        resolved = harness.resolve_harness(self.cfg, "piws", "")
        self.assertEqual(resolved.harness, "pi")
        self.assertEqual(resolved.sid, first.sid)
        self.assertFalse(resolved.was_created)

    def test_pi_as_configured_default(self):
        cfg = _make_cfg(self._tmp.name, default_harness="pi")
        self.assertEqual(harness.resolve_harness(cfg, "dflt", "").harness, "pi")

    def test_mutual_exclusivity_holds_across_all_three_values(self):
        """Every ordered pair of distinct harnesses must be refused without
        mutating the index."""
        for bound in ("opencode", "claude", "pi"):
            for other in ("opencode", "claude", "pi"):
                if other == bound:
                    continue
                ws = "ws-{}-{}".format(bound, other)
                harness.resolve_harness(self.cfg, ws, bound)
                raw_before = (self.cfg.ws_dir / ws).read_text()
                with self.assertRaises(SystemExit, msg="{} -> {}".format(bound, other)):
                    harness.resolve_harness(self.cfg, ws, other)
                self.assertEqual(raw_before, (self.cfg.ws_dir / ws).read_text())
                self.assertEqual(
                    workspace.read_workspace_state(self.cfg, ws).harness, bound
                )

    def test_legacy_reconcile_default_is_still_opencode(self):
        """Adding a third harness MUST NOT change what a legacy index (session
        id only, no harness) reconciles to."""
        self.cfg.ws_dir.mkdir(parents=True, exist_ok=True)
        (self.cfg.ws_dir / "legacy-pi-era").write_text("sch-legacy-pi-era-eeee")
        resolved = harness.resolve_harness(self.cfg, "legacy-pi-era", "")
        self.assertEqual(resolved.harness, "opencode")
        self.assertEqual(resolved.sid, "sch-legacy-pi-era-eeee")

    def test_explicit_pi_flag_on_a_legacy_index_is_an_upgrade_handoff(self):
        self.cfg.ws_dir.mkdir(parents=True, exist_ok=True)
        (self.cfg.ws_dir / "legacy-to-pi").write_text("sch-legacy-to-pi-ffff")
        resolved = harness.resolve_harness(self.cfg, "legacy-to-pi", "pi")
        self.assertEqual(resolved.harness, "pi")
        self.assertEqual(resolved.sid, "sch-legacy-to-pi-ffff")
        self.assertEqual(
            workspace.read_workspace_state(self.cfg, "legacy-to-pi").harness, "pi"
        )

    def test_invalid_harness_message_names_all_three_values(self):
        import contextlib
        import io

        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            harness.resolve_harness(self.cfg, "nope", "bogus")
        message = err.getvalue()
        for value in ("opencode", "claude", "pi"):
            self.assertIn(value, message)


if __name__ == "__main__":
    unittest.main()
