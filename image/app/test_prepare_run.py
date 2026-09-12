"""Offline tests for prepare-run model validation and marker propagation."""

from __future__ import annotations

import __future__
import asyncio
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


class FakeApp:
    def entrypoint(self, func):
        return func

    def websocket(self, func):
        return func

    def add_async_task(self, *_args, **_kwargs):
        return object()

    def complete_async_task(self, *_args, **_kwargs):
        return None

    def run(self, *_args, **_kwargs):
        return None


def load_main():
    app_dir = Path(__file__).parent
    sys.path.insert(0, str(app_dir))
    bedrock = types.ModuleType("bedrock_agentcore")
    bedrock.BedrockAgentCoreApp = FakeApp
    boto3 = types.ModuleType("boto3")
    boto3.client = lambda *_args, **_kwargs: None
    module_name = "sch_prepare_run_main"
    spec = importlib.util.spec_from_file_location(module_name, app_dir / "main.py")
    module = importlib.util.module_from_spec(spec)
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    with patch.dict(
        sys.modules,
        {module_name: module, "bedrock_agentcore": bedrock, "boto3": boto3},
    ), patch("threading.Thread"):
        source = (app_dir / "main.py").read_text(encoding="utf-8")
        code = compile(
            source,
            str(app_dir / "main.py"),
            "exec",
            flags=__future__.annotations.compiler_flag,
            dont_inherit=True,
        )
        exec(code, module.__dict__)
    return module


main = load_main()


class PrepareRunModelTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        main.RUN_ONCE_MARKER = Path(self._tmp.name) / "run-once.json"
        main._HARNESS["value"] = "opencode"
        main._WORKSPACE_NAME["value"] = "workspace"
        main._SESSION_EPOCH["value"] = 0
        main._STORAGE_BACKEND["value"] = "session"
        main.CHECKPOINT_BUCKET = ""

    def invoke(self, **updates):
        payload = {"action": "prepare-run", "workspace": "workspace", "harness": "opencode"}
        payload.update(updates)
        return main.invoke(payload)

    def test_valid_model_is_written_and_echoed(self):
        model = "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
        response = self.invoke(model=model)

        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["model"], model)
        self.assertEqual(json.loads(main.RUN_ONCE_MARKER.read_text())["model"], model)

    def test_malformed_models_are_rejected_without_marker(self):
        for model in ("", "model with spaces", "provider/model$id", 123, None):
            with self.subTest(model=model):
                main.RUN_ONCE_MARKER.unlink(missing_ok=True)
                response = self.invoke(model=model)
                self.assertEqual(response["status"], "rejected")
                self.assertFalse(main.RUN_ONCE_MARKER.exists())

    def test_empty_model_is_rejected_while_absent_model_is_accepted(self):
        empty_response = self.invoke(model="")
        self.assertEqual(empty_response["status"], "rejected")
        self.assertFalse(main.RUN_ONCE_MARKER.exists())

        absent_response = self.invoke()
        self.assertEqual(absent_response["status"], "ok")
        self.assertNotIn("model", absent_response)

    def test_absent_model_is_absent_from_marker_and_response(self):
        response = self.invoke()
        marker = json.loads(main.RUN_ONCE_MARKER.read_text())

        self.assertEqual(response["status"], "ok")
        self.assertNotIn("model", response)
        self.assertNotIn("model", marker)


class PrepareRunContinueTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        main.RUN_ONCE_MARKER = Path(self._tmp.name) / "run-once.json"
        main._HARNESS["value"] = "opencode"
        main._WORKSPACE_NAME["value"] = "workspace"
        main._SESSION_EPOCH["value"] = 0
        main._STORAGE_BACKEND["value"] = "session"
        main.CHECKPOINT_BUCKET = ""
        # Warm workspace: the continue path gates on readiness (TASK-29), so
        # the resolution tests below run against a ready worktree.
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()
        self._orig_repo_dir = main.REPO_DIR
        main.REPO_DIR = self.repo
        main._WORKSPACE_READY.set()
        self.addCleanup(self._restore_ready_state)

    def _restore_ready_state(self):
        main.REPO_DIR = self._orig_repo_dir
        main._WORKSPACE_READY.clear()

    def invoke(self, **updates):
        payload = {"action": "prepare-run", "workspace": "workspace", "harness": "opencode"}
        payload.update(updates)
        return main.invoke(payload)

    def test_continue_resolves_and_embeds_session(self):
        with patch.object(
            main, "_resolve_latest_harness_session", return_value="ses_1"
        ) as resolve:
            response = self.invoke(**{"continue": True})

        resolve.assert_called_once_with("opencode")
        self.assertEqual(response["status"], "ok")
        self.assertIs(response["continue"], True)
        self.assertEqual(response["session_id"], "ses_1")
        marker = json.loads(main.RUN_ONCE_MARKER.read_text())
        self.assertIs(marker["continue"], True)
        self.assertEqual(marker["session_id"], "ses_1")

    def test_continue_without_session_arms_fresh_with_flag(self):
        with patch.object(
            main, "_resolve_latest_harness_session", return_value=None
        ):
            response = self.invoke(**{"continue": True})

        self.assertEqual(response["status"], "ok")
        self.assertIs(response["continue"], True)
        self.assertNotIn("session_id", response)
        marker = json.loads(main.RUN_ONCE_MARKER.read_text())
        self.assertIs(marker["continue"], True)
        self.assertNotIn("session_id", marker)

    def test_no_continue_leaves_marker_without_session_fields(self):
        with patch.object(
            main, "_resolve_latest_harness_session", return_value="ses_1"
        ) as resolve:
            response = self.invoke()

        resolve.assert_not_called()
        self.assertEqual(response["status"], "ok")
        self.assertNotIn("continue", response)
        self.assertNotIn("session_id", response)
        marker = json.loads(main.RUN_ONCE_MARKER.read_text())
        self.assertNotIn("continue", marker)
        self.assertNotIn("session_id", marker)

    def test_explicit_continue_false_is_ignored(self):
        with patch.object(
            main, "_resolve_latest_harness_session", return_value="ses_1"
        ) as resolve:
            response = self.invoke(**{"continue": False})

        resolve.assert_not_called()
        self.assertEqual(response["status"], "ok")
        self.assertNotIn("continue", response)

    def test_continue_composes_with_model(self):
        model = "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
        with patch.object(
            main, "_resolve_latest_harness_session", return_value="ses_9"
        ):
            response = self.invoke(**{"continue": True, "model": model})

        self.assertEqual(response["model"], model)
        self.assertEqual(response["session_id"], "ses_9")
        marker = json.loads(main.RUN_ONCE_MARKER.read_text())
        self.assertEqual(marker["model"], model)
        self.assertEqual(marker["session_id"], "ses_9")


class _FakeReadyEvent:
    """Stand-in for main._WORKSPACE_READY that records the wait and lets a
    test decide whether the workspace becomes ready (TASK-29 gate tests)."""

    def __init__(self, becomes_ready):
        self.becomes_ready = becomes_ready
        self.wait_calls = []
        self.ready = False
        self.wait_returned_at = None

    def wait(self, timeout=None):
        import time as _time

        self.wait_calls.append(timeout)
        self.ready = self.becomes_ready
        self.wait_returned_at = _time.time()
        return self.becomes_ready

    def is_set(self):
        return self.ready


class PrepareRunReadyGateTests(unittest.TestCase):
    """`sch run --continue` must look up the session only once the workspace
    is ready (TASK-29, decision-8): on a cold boot the session store is
    restored asynchronously, and an early lookup arms a fresh TUI while the
    pre-stop session sits in S3."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        main.RUN_ONCE_MARKER = Path(self._tmp.name) / "run-once.json"
        main._HARNESS["value"] = "opencode"
        main._WORKSPACE_NAME["value"] = "workspace"
        main._SESSION_EPOCH["value"] = 0
        main._STORAGE_BACKEND["value"] = "session"
        main.CHECKPOINT_BUCKET = ""
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()
        self._orig_repo_dir = main.REPO_DIR
        main.REPO_DIR = self.repo
        main._WORKSPACE_READY.clear()
        self.addCleanup(self._restore_repo_dir)

    def _restore_repo_dir(self):
        main.REPO_DIR = self._orig_repo_dir

    def invoke(self, **updates):
        payload = {"action": "prepare-run", "workspace": "workspace", "harness": "opencode"}
        payload.update(updates)
        return main.invoke(payload)

    def test_continue_resolves_only_after_the_ready_wait(self):
        event = _FakeReadyEvent(becomes_ready=True)
        seen_ready_at_resolution = []

        def resolver(_harness):
            seen_ready_at_resolution.append(event.ready)
            return "ses_pre_stop"

        with patch.object(main, "_WORKSPACE_READY", event), patch.object(
            main, "_resolve_latest_harness_session", side_effect=resolver
        ) as resolve:
            response = self.invoke(**{"continue": True})

        self.assertEqual(event.wait_calls, [main.PREPARE_RUN_READY_TIMEOUT_S])
        resolve.assert_called_once_with("opencode")
        self.assertEqual(seen_ready_at_resolution, [True])
        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["session_id"], "ses_pre_stop")
        marker = json.loads(main.RUN_ONCE_MARKER.read_text())
        self.assertEqual(marker["session_id"], "ses_pre_stop")
        # The autostart TTL (300 s) counts from the marker epoch: it must be
        # stamped after the wait, not before it.
        self.assertGreaterEqual(marker["epoch"], event.wait_returned_at)

    def test_continue_on_a_workspace_that_never_becomes_ready_is_an_error(self):
        event = _FakeReadyEvent(becomes_ready=False)
        with patch.object(main, "_WORKSPACE_READY", event), patch.object(
            main, "_resolve_latest_harness_session", return_value="ses_1"
        ) as resolve:
            response = self.invoke(**{"continue": True})

        resolve.assert_not_called()
        self.assertEqual(response["status"], "error")
        self.assertEqual(response["action"], "prepare-run")
        self.assertIn("cannot resolve the session to resume", response["error"])
        self.assertIn("retry `sch run --continue`", response["error"])
        self.assertIn(str(main.PREPARE_RUN_READY_TIMEOUT_S), response["error"])
        self.assertNotIn("continue", response)
        self.assertNotIn("session_id", response)
        self.assertFalse(main.RUN_ONCE_MARKER.exists(), "no fresh TUI must be armed")

    def test_continue_with_ready_event_but_missing_worktree_is_an_error(self):
        main._WORKSPACE_READY.set()
        self.addCleanup(main._WORKSPACE_READY.clear)
        main.REPO_DIR = Path(self._tmp.name) / "missing-repo"
        with patch.object(
            main, "_resolve_latest_harness_session", return_value="ses_1"
        ) as resolve:
            response = self.invoke(**{"continue": True})

        resolve.assert_not_called()
        self.assertEqual(response["status"], "error")
        self.assertFalse(main.RUN_ONCE_MARKER.exists())

    def test_without_continue_the_handler_never_waits(self):
        event = _FakeReadyEvent(becomes_ready=False)
        with patch.object(main, "_WORKSPACE_READY", event), patch.object(
            main, "_resolve_latest_harness_session", return_value="ses_1"
        ) as resolve:
            response = self.invoke()

        self.assertEqual(event.wait_calls, [])
        resolve.assert_not_called()
        self.assertEqual(response["status"], "ok")
        marker = json.loads(main.RUN_ONCE_MARKER.read_text())
        self.assertNotIn("continue", marker)
        self.assertNotIn("session_id", marker)

    def test_fast_rejects_do_not_wait(self):
        event = _FakeReadyEvent(becomes_ready=False)
        with patch.object(main, "_WORKSPACE_READY", event):
            mismatch = self.invoke(**{"continue": True, "harness": "claude"})
            bad_model = self.invoke(**{"continue": True, "model": "model with spaces"})

        self.assertEqual(mismatch["status"], "rejected")
        self.assertEqual(bad_model["status"], "rejected")
        self.assertEqual(event.wait_calls, [])
        self.assertFalse(main.RUN_ONCE_MARKER.exists())

    def test_ready_timeout_matches_the_task_worker_bound(self):
        self.assertEqual(main.PREPARE_RUN_READY_TIMEOUT_S, 240)


if __name__ == "__main__":
    unittest.main()
