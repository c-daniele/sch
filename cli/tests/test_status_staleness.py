"""Unit tests for the offline staleness classification of `sch status`
(add-task-liveness-safety, tasks 1.1-1.3; spec: headless-task-execution,
"Staleness dello stato running visibile offline").

Contract under test:
  - `running` + heartbeat older than 150s  -> STALE rendering + exit code 3
  - `running` + fresh heartbeat            -> rendering/exit code unchanged
  - `running` + absent/malformed heartbeat -> suspect (STALE, no exception)
  - terminal states                        -> rendering/exit code unchanged
  - `--json`                               -> byte-for-byte passthrough,
                                              classification only via exit code
  - the persisted S3 object is never rewritten
"""

import contextlib
import datetime
import io
import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

_CLI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _CLI_DIR not in sys.path:
    sys.path.insert(0, _CLI_DIR)

from sch import workspace as workspace_mod
from sch.commands import status as status_cmd

_NOW = datetime.datetime(2026, 8, 21, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _stamp(seconds_ago):
    return (_NOW - datetime.timedelta(seconds=seconds_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


class HeartbeatAgeTests(unittest.TestCase):
    def test_age_of_a_wellformed_stamp(self):
        self.assertAlmostEqual(
            status_cmd.heartbeat_age_seconds(_stamp(90), _NOW), 90.0
        )

    def test_absent_or_malformed_values_have_no_age(self):
        for value in (None, "", "   ", "not-a-date", "2026-08-21 12:00:00", 42, {}):
            self.assertIsNone(
                status_cmd.heartbeat_age_seconds(value, _NOW), repr(value)
            )

    def test_stamp_in_the_future_yields_a_negative_age(self):
        self.assertLess(status_cmd.heartbeat_age_seconds(_stamp(-30), _NOW), 0)

    def test_humanize_age_forms(self):
        self.assertEqual(status_cmd.humanize_age(0), "0s")
        self.assertEqual(status_cmd.humanize_age(45.9), "45s")
        self.assertEqual(status_cmd.humanize_age(60), "1m 0s")
        self.assertEqual(status_cmd.humanize_age(1872), "31m 12s")
        self.assertEqual(status_cmd.humanize_age(3600), "1h 00m")
        self.assertEqual(status_cmd.humanize_age(7500), "2h 05m")

    def test_humanize_age_of_a_negative_age_is_zero(self):
        self.assertEqual(status_cmd.humanize_age(-30), "0s")


class RunningStaleNoteTests(unittest.TestCase):
    def test_stale_running_is_qualified_with_the_heartbeat_age(self):
        note = status_cmd.running_stale_note(
            {"state": "running", "heartbeat_utc": _stamp(1872)}, _NOW
        )
        self.assertEqual(note, "STALE: ultimo heartbeat 31m 12s fa")

    def test_fresh_running_has_no_note(self):
        for age in (0, 30, 149, status_cmd.STALE_AFTER_S):
            self.assertEqual(
                status_cmd.running_stale_note(
                    {"state": "running", "heartbeat_utc": _stamp(age)}, _NOW
                ),
                "",
                "age={}".format(age),
            )

    def test_just_past_the_threshold_is_stale(self):
        note = status_cmd.running_stale_note(
            {"state": "running", "heartbeat_utc": _stamp(status_cmd.STALE_AFTER_S + 1)},
            _NOW,
        )
        self.assertTrue(note.startswith("STALE: ultimo heartbeat"))

    def test_missing_heartbeat_is_suspect(self):
        self.assertEqual(
            status_cmd.running_stale_note({"state": "running"}, _NOW),
            "STALE: no heartbeat",
        )

    def test_malformed_heartbeat_is_suspect(self):
        self.assertEqual(
            status_cmd.running_stale_note(
                {"state": "running", "heartbeat_utc": "yesterday"}, _NOW
            ),
            "STALE: no heartbeat",
        )

    def test_terminal_states_are_never_stale(self):
        for state in ("succeeded", "failed", "timed-out", "interrupted", "none"):
            self.assertEqual(
                status_cmd.running_stale_note(
                    {"state": state, "heartbeat_utc": _stamp(99999)}, _NOW
                ),
                "",
                state,
            )

    def test_non_dict_input_is_tolerated(self):
        for value in (None, "running", 3, ["running"]):
            self.assertEqual(status_cmd.running_stale_note(value, _NOW), "")


class StaleRenderingTests(unittest.TestCase):
    def _render(self, data, now=_NOW):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            status_cmd.render_status(
                data, stale_note=status_cmd.running_stale_note(data, now)
            )
        return buf.getvalue()

    def test_stale_state_line(self):
        out = self._render(
            {"state": "running", "task_id": "t-1", "heartbeat_utc": _stamp(1872)}
        )
        self.assertEqual(
            out.splitlines()[0],
            "state        : running (STALE: ultimo heartbeat 31m 12s fa)",
        )

    def test_missing_heartbeat_state_line(self):
        out = self._render({"state": "running"})
        self.assertEqual(
            out.splitlines()[0], "state        : running (STALE: no heartbeat)"
        )

    def test_fresh_and_terminal_renderings_are_byte_identical(self):
        for data in (
            {"state": "running", "task_id": "t-1", "heartbeat_utc": _stamp(30)},
            {"state": "succeeded", "exit_code": 0, "heartbeat_utc": _stamp(99999)},
            {"state": "none"},
        ):
            self.assertEqual(
                self._render(data),
                status_cmd.format_status(data, stale_note=""),
                data,
            )

    def test_format_status_computes_the_note_when_not_given(self):
        # Default path (no explicit note): the current clock is used, so a
        # very old heartbeat is stale regardless of when the test runs.
        out = status_cmd.format_status(
            {"state": "running", "heartbeat_utc": "2020-01-01T00:00:00Z"}
        )
        self.assertIn("(STALE: ultimo heartbeat ", out.splitlines()[0])


class CmdStatusExitCodeTests(unittest.TestCase):
    """`cmd_status` end-to-end over a stubbed S3 read: exit codes, `--json`
    passthrough purity, and the read-only guarantee."""

    def setUp(self):
        self.cfg = SimpleNamespace(
            region="eu-west-1", ws_dir=None, default_harness="opencode",
            default_storage="s3", workspace_registry_url="",
        )
        self.resolved = SimpleNamespace(
            sid="sid-1", harness="opencode", identity="", storage="s3", epoch=1,
            was_created=False,
        )

    def _run(self, payload, args=("ws",)):
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        buf = io.StringIO()
        with patch.object(
            status_cmd.harness_mod, "resolve_harness", return_value=self.resolved
        ) as resolve, patch.object(
            status_cmd, "read_offline_status", return_value=raw
        ), patch.object(
            status_cmd.workspace, "read_workspace_state",
            return_value=workspace_mod.WorkspaceState(
                sid="sid-1", harness="opencode", storage="s3",
                storage_present=True, epoch=1,
            ),
        ), patch.object(
            status_cmd.runtime, "invoke_verified"
        ) as invoke, contextlib.redirect_stdout(buf):
            rc = status_cmd.cmd_status(self.cfg, list(args))
        # Read-only by construction: no runtime invocation, and the harness
        # resolution is the non-provisioning one (no version lookup, no
        # rotation - task 2.5).
        invoke.assert_not_called()
        for call in resolve.call_args_list:
            self.assertNotIn("provisioning", call.kwargs)
        return rc, buf.getvalue()

    def test_stale_running_returns_three(self):
        rc, out = self._run({"state": "running", "heartbeat_utc": "2020-01-01T00:00:00Z"})
        self.assertEqual(rc, status_cmd.EXIT_RUNNING_STALE)
        self.assertIn("STALE", out)

    def test_fresh_running_returns_zero(self):
        fresh = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        rc, out = self._run({"state": "running", "heartbeat_utc": fresh})
        self.assertEqual(rc, 0)
        self.assertNotIn("STALE", out)

    def test_missing_heartbeat_returns_three(self):
        rc, out = self._run({"state": "running"})
        self.assertEqual(rc, status_cmd.EXIT_RUNNING_STALE)
        self.assertIn("STALE: no heartbeat", out)

    def test_terminal_state_returns_zero(self):
        rc, _ = self._run({"state": "succeeded", "exit_code": 0})
        self.assertEqual(rc, 0)

    def test_json_mode_is_pure_passthrough_with_exit_code_three(self):
        raw = '{"state":"running","heartbeat_utc":"2020-01-01T00:00:00Z","task_id":"t-9"}'
        rc, out = self._run(raw, args=("ws", "--json"))
        self.assertEqual(rc, status_cmd.EXIT_RUNNING_STALE)
        self.assertEqual(out, raw + "\n")
        # No synthetic field of any kind reaches the consumer.
        self.assertEqual(json.loads(out), json.loads(raw))

    def test_json_mode_fresh_returns_zero(self):
        fresh = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        raw = json.dumps({"state": "running", "heartbeat_utc": fresh})
        rc, out = self._run(raw, args=("ws", "--json"))
        self.assertEqual(rc, 0)
        self.assertEqual(out, raw + "\n")

    def test_invalid_json_keeps_the_existing_error_contract(self):
        rc, out = self._run("not json at all")
        self.assertEqual(rc, 1)
        self.assertIn("invalid JSON from S3", out)

    def test_invalid_json_in_json_mode_stays_a_passthrough(self):
        rc, out = self._run("not json at all", args=("ws", "--json"))
        self.assertEqual(rc, 0)
        self.assertEqual(out, "not json at all\n")


if __name__ == "__main__":
    unittest.main()
