"""Offline tests for task --continue resolution and surfacing (TASK-27).

Contract under test:
  - the worker resolves the session to continue AFTER the workspace is
    ready (cold-boot db restore has landed), not at thread start;
  - an explicit handoff hint wins without a store lookup;
  - a miss still degrades to a fresh session (spec R2) but records
    continue_resolved=False instead of staying silent;
  - without --continue the resolver never runs and no continue fields are
    persisted;
  - the submit acknowledgement echoes continue:true when requested.
"""

from __future__ import annotations

import __future__
import asyncio
import importlib.util
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
    module_name = "sch_task_continue_main"
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


class ResolveHelperTests(unittest.TestCase):
    def test_not_requested_resolves_nothing(self):
        with patch.object(main, "_resolve_latest_harness_session") as resolve:
            session_id, resolved = main._resolve_continue_session(
                False, None, "opencode"
            )
        self.assertIsNone(session_id)
        self.assertIsNone(resolved)
        resolve.assert_not_called()

    def test_hint_wins_without_store_lookup(self):
        with patch.object(main, "_resolve_latest_harness_session") as resolve:
            session_id, resolved = main._resolve_continue_session(
                True, "ses_hint", "opencode"
            )
        self.assertEqual(session_id, "ses_hint")
        self.assertTrue(resolved)
        resolve.assert_not_called()

    def test_store_hit(self):
        with patch.object(
            main, "_resolve_latest_harness_session", return_value="ses_old"
        ):
            session_id, resolved = main._resolve_continue_session(
                True, None, "opencode"
            )
        self.assertEqual(session_id, "ses_old")
        self.assertTrue(resolved)

    def test_store_miss_flags_degraded(self):
        with patch.object(
            main, "_resolve_latest_harness_session", return_value=None
        ):
            session_id, resolved = main._resolve_continue_session(
                True, None, "opencode"
            )
        self.assertIsNone(session_id)
        self.assertFalse(resolved)


class FakeProc:
    pid = 4242
    returncode = 0

    def communicate(self, timeout=None):
        return "done", ""

    def poll(self):
        return 0


class WorkerContinueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.ready = self.root / ".ready"
        self.ready.touch()
        self._saved_state = dict(main._TASK_STATE)
        self._saved_status = dict(main._TASK_STATUS)
        self.addCleanup(main._TASK_STATE.update, self._saved_state)
        self.addCleanup(main._TASK_STATUS.update, self._saved_status)
        main._TASK_STATE.clear()
        main._TASK_STATUS.clear()
        main._TASK_STATE.update({"task_id": "t1", "state": "running"})
        main._TASK_STATUS.update({
            "task_id": "t1",
            "state": "running",
            "harness_session_id": None,
        })
        self._continue_session = None

    def _run_worker(self, continue_session, session_id_hint, resolved_session,
                      model=None):
        # Mirror the submit handler: it records continue_requested at accept.
        self._continue_session = continue_session
        if continue_session:
            main._TASK_STATUS["continue_requested"] = True
        recorded = {"argv": None, "finish": None, "uploads": []}
        finished = {}

        def fake_resolve(harness):
            return resolved_session

        def fake_argv(harness, session_id, prompt, argv_model, variant=None):
            recorded["argv"] = (harness, session_id, prompt, argv_model, variant)
            return ["/bin/true"]

        def fake_finish(**kwargs):
            finished.update(kwargs)

        def fake_upload(workspace, body):
            recorded["uploads"].append(dict(body))
            return True

        with patch.object(main, "REPO_DIR", self.repo), \
             patch.object(main, "_harness_ready_marker", return_value=self.ready), \
             patch.object(main, "_resolve_latest_harness_session", side_effect=fake_resolve) as resolve, \
             patch.object(main, "_build_headless_argv", side_effect=fake_argv), \
             patch.object(main, "_run_task_heartbeat", return_value=None), \
             patch.object(main, "_finish_task", side_effect=fake_finish), \
             patch.object(main, "_upload_task_status", side_effect=fake_upload), \
             patch.object(main.subprocess, "Popen", return_value=FakeProc()):
            main._run_task(
                task_id="t1", prompt="hi", timeout_s=5,
                continue_session=continue_session,
                session_id_hint=session_id_hint,
                workspace="ws", harness="opencode", model=model,
            )
        return recorded, finished, resolve

    def test_post_ready_hit_resumes_and_records(self):
        recorded, finished, _ = self._run_worker(True, None, "ses_old")
        self.assertEqual(recorded["argv"][1], "ses_old")
        self.assertEqual(finished["harness_session_id"], "ses_old")
        self.assertTrue(finished["continue_requested"])
        self.assertTrue(finished["continue_resolved"])
        bodies = [b for b in recorded["uploads"] if "continue_resolved" in b]
        self.assertTrue(bodies)
        self.assertTrue(bodies[-1]["continue_resolved"])
        self.assertEqual(bodies[-1]["harness_session_id"], "ses_old")
        self.assertTrue(bodies[-1]["continue_requested"])

    def test_post_ready_miss_degrades_but_records(self):
        recorded, finished, _ = self._run_worker(True, None, None)
        self.assertIsNone(recorded["argv"][1])
        self.assertIsNone(finished["harness_session_id"])
        self.assertTrue(finished["continue_requested"])
        self.assertFalse(finished["continue_resolved"])
        bodies = [b for b in recorded["uploads"] if "continue_resolved" in b]
        self.assertTrue(bodies)
        self.assertFalse(bodies[-1]["continue_resolved"])

    def test_no_continue_never_resolves_nor_records(self):
        recorded, finished, resolve = self._run_worker(False, None, "ses_old")
        resolve.assert_not_called()
        self.assertIsNone(recorded["argv"][1])
        self.assertIsNone(finished["harness_session_id"])
        self.assertFalse(finished["continue_requested"])
        self.assertIsNone(finished["continue_resolved"])
        self.assertEqual(recorded["uploads"], [])
        self.assertNotIn("continue_requested", main._TASK_STATUS)
        self.assertNotIn("continue_resolved", main._TASK_STATUS)

    def test_hint_skips_store_lookup(self):
        recorded, finished, resolve = self._run_worker(True, "ses_hint", "ses_old")
        resolve.assert_not_called()
        self.assertEqual(recorded["argv"][1], "ses_hint")
        self.assertEqual(finished["harness_session_id"], "ses_hint")
        self.assertTrue(finished["continue_resolved"])

    def test_continue_forwards_stored_model_and_variant(self):
        # No explicit --model: the resumed session's stored model+variant
        # (TUI selection) must reach the headless argv.
        with patch.object(
            main, "_opencode_continue_model",
            return_value=("amazon-bedrock/muse-spark-1.3", "high"),
        ) as preserved:
            recorded, _, _ = self._run_worker(True, None, "ses_old")
        preserved.assert_called_once_with("ses_old")
        self.assertEqual(
            recorded["argv"],
            ("opencode", "ses_old", "hi", "amazon-bedrock/muse-spark-1.3", "high"),
        )

    def test_explicit_model_wins_and_skips_stored_lookup(self):
        # An explicit --model always wins; the stored variant belongs to the
        # previous model and must not leak into the argv.
        with patch.object(main, "_opencode_continue_model") as preserved:
            recorded, _, _ = self._run_worker(
                True, None, "ses_old", model="explicit/other-model"
            )
        preserved.assert_not_called()
        self.assertEqual(
            recorded["argv"],
            ("opencode", "ses_old", "hi", "explicit/other-model", None),
        )


class ResolveOrderingTests(unittest.TestCase):
    def test_resolution_happens_after_the_ready_wait(self):
        # Structural guard for the TASK-27 fix: resolving before the
        # workspace-ready wait reintroduces the cold-boot race (the session
        # store restore lands during the wait). Same technique as
        # test_fs_workspace_ready.py.
        import inspect

        source = inspect.getsource(main._run_task)
        wait_at = source.index("ready_deadline")
        resolve_at = source.index("_resolve_continue_session(")
        self.assertGreater(
            resolve_at, wait_at,
            "continuation must resolve after the workspace-ready wait",
        )


class SubmitAckTests(unittest.TestCase):
    def setUp(self):
        self._saved_state = dict(main._TASK_STATE)
        self._saved_status = dict(main._TASK_STATUS)
        self.addCleanup(main._TASK_STATE.update, self._saved_state)
        self.addCleanup(main._TASK_STATUS.update, self._saved_status)
        main._TASK_STATE.clear()
        main._TASK_STATUS.clear()
        main._TASK_STATE.update({"state": "none", "task_id": None})

    def _submit(self, payload):
        created = {}

        class DummyThread:
            def __init__(self, target=None, name=None, daemon=None, args=()):
                created["target"] = target
                created["args"] = args

            def start(self):
                return None

        with patch.object(main, "_resolve_workspace_name", return_value="ws"), \
             patch.object(main, "_resolve_harness", return_value="opencode"), \
             patch.object(main, "_upload_task_status", return_value=True), \
             patch.object(main, "_telegram_notify", return_value=None), \
             patch.object(main.threading, "Thread", DummyThread):
            return main._handle_task_action(payload)

    def test_continue_request_is_echoed_and_recorded(self):
        response = self._submit({"prompt": "hi", "continue": True})
        self.assertEqual(response["status"], "accepted")
        self.assertTrue(response["task_id"])
        self.assertIs(response["continue"], True)
        self.assertTrue(main._TASK_STATUS["continue_requested"])

    def test_absent_continue_stays_absent(self):
        response = self._submit({"prompt": "hi"})
        self.assertEqual(response["status"], "accepted")
        self.assertNotIn("continue", response)
        self.assertNotIn("continue_requested", main._TASK_STATUS)


if __name__ == "__main__":
    unittest.main()
