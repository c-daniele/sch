"""Tests for task --continue surfacing (TASK-27).

  - the submit acknowledgement echo: missing echo on a requested --continue
    warns on stderr (parity with `sch run --continue` and the --model echo);
  - `sch status` renders the continuation provenance only when the task was
    submitted with --continue (absent flag -> byte-identical rendering).
"""

import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from sch import runtime
from sch.commands import status as status_cmd
from sch.commands import task as task_cmd


def _resolved(harness="opencode"):
    return type(
        "Resolved", (), {
            "sid": "sid", "harness": harness, "identity": "ws",
            "storage": "s3", "epoch": 1, "was_created": False,
        }
    )()


def _cfg():
    return type(
        "Config", (), {"region": "test", "ws_dir": Path("/nonexistent/sch-test-ws")}
    )()


def _run_continue_task(ack_extra):
    """Drive cmd_task with --continue; ack_extra customizes the task ack."""
    warm = runtime.InvocationResult(True, json.dumps({"status": "ok", "storage": "s3"}))
    ack_payload = {"status": "accepted", "task_id": "t-1"}
    ack_payload.update(ack_extra)
    accepted = runtime.InvocationResult(True, json.dumps(ack_payload))

    def fake_invoke(cfg, sid, payload, op):
        if op == "task-warmup":
            return warm
        if op == "task":
            return accepted
        raise AssertionError(op)

    out, err = io.StringIO(), io.StringIO()
    with patch.object(
        task_cmd.harness_mod, "resolve_harness", return_value=_resolved()
    ), patch.object(task_cmd.gitnative, "resolve_mode", return_value=None), \
         patch.object(task_cmd.sync_mod, "resolve_binding", return_value=None), \
         patch.object(task_cmd.runtime, "invoke_verified", side_effect=fake_invoke), \
         patch.object(task_cmd.workspace, "mark_status"), \
         contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = task_cmd.cmd_task(_cfg(), ["ws", "--continue", "build it"])
    return code, out.getvalue(), err.getvalue()


class ContinueAckTests(unittest.TestCase):
    def test_missing_echo_warns(self):
        code, out, err = _run_continue_task({})
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "t-1")
        self.assertIn("did not echo requested --continue", err)

    def test_present_echo_stays_quiet(self):
        code, out, err = _run_continue_task({"continue": True})
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "t-1")
        self.assertNotIn("--continue", err)


class ContinuationRenderingTests(unittest.TestCase):
    def _render(self, data):
        return status_cmd.format_status(dict(data))

    def test_absent_flag_renders_nothing(self):
        text = self._render({"state": "succeeded", "harness_session_id": "ses_x"})
        self.assertNotIn("continuation", text)
        self.assertIn("session_id   : ses_x", text)

    def test_resumed_renders(self):
        text = self._render({
            "state": "succeeded",
            "harness_session_id": "ses_x",
            "continue_requested": True,
            "continue_resolved": True,
        })
        self.assertIn("continuation : resumed prior session", text)

    def test_degraded_renders_fresh_warning(self):
        text = self._render({
            "state": "succeeded",
            "continue_requested": True,
            "continue_resolved": False,
        })
        self.assertIn("started fresh", text)

    def test_pending_resolution_renders(self):
        text = self._render({
            "state": "running",
            "continue_requested": True,
        })
        self.assertIn("continuation : requested (resolving)", text)


if __name__ == "__main__":
    unittest.main()
