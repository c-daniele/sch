"""Tests for `sch task --handoff` (TASK-23): combined local-session export,
remote import, and headless submission with implied --continue."""

import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from sch import runtime
from sch.commands import task as task_cmd


def _resolved(harness="opencode"):
    return type(
        "Resolved", (), {
            "sid": "sid", "harness": harness, "identity": "ws",
            "storage": "s3", "epoch": 1, "was_created": False,
        }
    )()


class TaskHandoffParseTests(unittest.TestCase):
    def test_handoff_defaults(self):
        result = task_cmd._parse_args(["ws", "build"])
        self.assertEqual(result[9], False)
        self.assertEqual(result[10], "")
        self.assertEqual(result[11], False)

    def test_handoff_flag_parses(self):
        result = task_cmd._parse_args(["ws", "--handoff", "build"])
        self.assertTrue(result[9])
        self.assertEqual(result[10], "")
        self.assertFalse(result[11])

    def test_handoff_session_and_sanitize_parse(self):
        result = task_cmd._parse_args(
            ["ws", "--handoff", "--handoff-session", "ses_x", "--sanitize", "build"]
        )
        self.assertTrue(result[9])
        self.assertEqual(result[10], "ses_x")
        self.assertTrue(result[11])

    def test_handoff_session_without_handoff_dies(self):
        with self.assertRaises(SystemExit):
            task_cmd._parse_args(["ws", "--handoff-session", "ses_x", "build"])

    def test_sanitize_without_handoff_dies(self):
        with self.assertRaises(SystemExit):
            task_cmd._parse_args(["ws", "--sanitize", "build"])

    def test_handoff_session_missing_value_dies(self):
        with self.assertRaises(SystemExit):
            task_cmd._parse_args(["ws", "--handoff", "--handoff-session"])

    def test_invalid_harness_with_handoff_dies(self):
        with self.assertRaises(SystemExit):
            task_cmd._parse_args(["ws", "--handoff", "--harness", "vscode", "build"])

    def test_handoff_after_separator_is_prompt_text(self):
        # `--` contract: flags after it are prompt words, not options.
        result = task_cmd._parse_args(["ws", "--", "--handoff"])
        self.assertFalse(result[9])
        self.assertEqual(result[7], "--handoff")


class TaskHandoffRejectTests(unittest.TestCase):
    def _cfg(self):
        return type(
            "Config", (), {"region": "test", "ws_dir": Path("/nonexistent/sch-test-ws")}
        )()

    def test_non_opencode_harness_flag_rejected_without_mutation(self):
        for flag in ("claude", "pi"):
            with self.subTest(flag=flag):
                with patch.object(task_cmd.deps, "which_opencode") as which, \
                     patch.object(task_cmd.handoff_mod, "export_local_session") as export, \
                     patch.object(task_cmd.harness_mod, "resolve_harness") as resolve, \
                     patch.object(task_cmd.runtime, "invoke_verified") as invoke, \
                     contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        task_cmd.cmd_task(
                            self._cfg(),
                            ["ws", "--handoff", "--harness", flag, "build"],
                        )
                    which.assert_not_called()
                    export.assert_not_called()
                    resolve.assert_not_called()
                    invoke.assert_not_called()

    def test_missing_opencode_dies_before_resolve(self):
        with patch.object(task_cmd.deps, "which_opencode", return_value=None), \
             patch.object(task_cmd.harness_mod, "resolve_harness") as resolve, \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                task_cmd.cmd_task(self._cfg(), ["ws", "--handoff", "build"])
            resolve.assert_not_called()

    def test_bound_claude_workspace_rejected_before_warmup(self):
        with patch.object(task_cmd.deps, "which_opencode", return_value="/bin/opencode"), \
             patch.object(
                 task_cmd.handoff_mod, "export_local_session",
                 return_value=("ses_x", "/tmp/export.json", "1.0"),
             ), \
             patch.object(
                 task_cmd.harness_mod, "resolve_harness",
                 return_value=_resolved("claude"),
             ), \
             patch.object(task_cmd.runtime, "invoke_verified") as invoke, \
             patch.object(task_cmd.os, "remove"), \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                task_cmd.cmd_task(self._cfg(), ["ws", "--handoff", "build"])
            invoke.assert_not_called()


class TaskHandoffFlowTests(unittest.TestCase):
    def _cfg(self):
        return type(
            "Config", (), {"region": "test", "ws_dir": Path("/nonexistent/sch-test-ws")}
        )()

    def _run_handoff_task(self, args, harness="opencode", seed_mode=None):
        """Run cmd_task with --handoff, mocking every remote boundary.

        Returns (code, stdout, stderr, invoke_mock, handoff_import_mock,
        task_payload, call_order).
        """
        order = []
        warm = runtime.InvocationResult(
            True, json.dumps({"status": "ok", "storage": "s3"})
        )
        accepted = runtime.InvocationResult(
            True, json.dumps({"status": "accepted", "task_id": "t-1"})
        )

        def fake_invoke(cfg, sid, payload, op):
            order.append(op)
            data = json.loads(payload)
            if op == "task-warmup":
                return warm
            if op == "session-import":
                return runtime.InvocationResult(
                    True, json.dumps({"status": "ok", "sessionID": "ses_x"})
                )
            if op == "task":
                return accepted
            raise AssertionError(op)

        def fake_ensure_seeded(*a, **k):
            order.append("seed")

        def fake_upload_and_import(*a, **k):
            order.append("handoff-import")
            return "ses_x"

        out, err = io.StringIO(), io.StringIO()
        with patch.object(task_cmd.deps, "which_opencode", return_value="/bin/opencode"), \
             patch.object(
                 task_cmd.handoff_mod, "export_local_session",
                 return_value=("ses_x", "/tmp/export.json", "1.0"),
             ) as export, \
             patch.object(
                 task_cmd.harness_mod, "resolve_harness",
                 return_value=_resolved(harness),
             ) as resolve, \
             patch.object(task_cmd.gitnative, "resolve_mode", return_value=seed_mode), \
             patch.object(task_cmd.gitnative, "ensure_seeded", side_effect=fake_ensure_seeded), \
             patch.object(task_cmd.sync_mod, "resolve_binding", return_value=None), \
             patch.object(task_cmd.runtime, "invoke_verified", side_effect=fake_invoke) as invoke, \
             patch.object(
                 task_cmd.handoff_mod, "upload_and_import",
                 side_effect=fake_upload_and_import,
             ) as handoff_import, \
             patch.object(task_cmd.os, "remove"), \
             patch.object(task_cmd.workspace, "mark_status"), \
             patch.object(task_cmd, "runtime_arn", return_value="arn"), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = task_cmd.cmd_task(self._cfg(), args)
        task_payload = json.loads(invoke.call_args.args[2])
        return code, out, err, invoke, handoff_import, task_payload, order, resolve, export

    def test_handoff_implies_continue_and_keeps_stdout_pure(self):
        code, out, err, invoke, handoff_import, payload, order, _, _ = (
            self._run_handoff_task(["ws", "--handoff", "build it"])
        )
        self.assertEqual(code, 0)
        self.assertTrue(payload["continue"])
        self.assertEqual(out.getvalue().strip(), "t-1")
        self.assertIn("handed off", err.getvalue())
        self.assertIn("ses_x", err.getvalue())
        handoff_import.assert_called_once()

    def test_explicit_continue_with_handoff_is_redundant_but_accepted(self):
        _, _, _, _, _, payload, _, _, _ = self._run_handoff_task(
            ["ws", "--handoff", "--continue", "build it"]
        )
        self.assertTrue(payload["continue"])

    def test_seed_runs_before_handoff_import(self):
        mode = type("Mode", (), {})()
        _, _, _, _, _, _, order, _, _ = self._run_handoff_task(
            ["ws", "--handoff", "--branch", "feat/x", "build it"], seed_mode=mode
        )
        self.assertIn("seed", order)
        self.assertIn("handoff-import", order)
        self.assertLess(order.index("seed"), order.index("handoff-import"))
        self.assertLess(order.index("handoff-import"), order.index("task"))

    def test_handoff_forces_opencode_default_on_resolve(self):
        _, _, _, _, _, _, _, resolve, _ = self._run_handoff_task(
            ["ws", "--handoff", "build it"]
        )
        # Second positional arg of resolve_harness is the harness flag.
        self.assertEqual(resolve.call_args.args[2], "opencode")

    def test_import_failure_never_submits_task(self):
        warm = runtime.InvocationResult(
            True, json.dumps({"status": "ok", "storage": "s3"})
        )
        out, err = io.StringIO(), io.StringIO()
        with patch.object(task_cmd.deps, "which_opencode", return_value="/bin/opencode"), \
             patch.object(
                 task_cmd.handoff_mod, "export_local_session",
                 return_value=("ses_x", "/tmp/export.json", "1.0"),
             ), \
             patch.object(
                 task_cmd.harness_mod, "resolve_harness",
                 return_value=_resolved("opencode"),
             ), \
             patch.object(task_cmd.gitnative, "resolve_mode", return_value=None), \
             patch.object(task_cmd.sync_mod, "resolve_binding", return_value=None), \
             patch.object(task_cmd.runtime, "invoke_verified", return_value=warm) as invoke, \
             patch.object(
                 task_cmd.handoff_mod, "upload_and_import",
                 side_effect=SystemExit(1),
             ), \
             patch.object(task_cmd.os, "remove"), \
             patch.object(task_cmd.workspace, "mark_status"), \
             patch.object(task_cmd, "runtime_arn", return_value="arn"), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit):
                task_cmd.cmd_task(self._cfg(), ["ws", "--handoff", "build it"])
        # Only the warmup ran; no task submission.
        self.assertEqual(invoke.call_count, 1)

    def test_export_failure_dies_before_resolve(self):
        with patch.object(task_cmd.deps, "which_opencode", return_value="/bin/opencode"), \
             patch.object(
                 task_cmd.handoff_mod, "export_local_session",
                 side_effect=SystemExit(1),
             ) as export, \
             patch.object(task_cmd.harness_mod, "resolve_harness") as resolve, \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                task_cmd.cmd_task(self._cfg(), ["ws", "--handoff", "build it"])
            export.assert_called_once()
            resolve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
