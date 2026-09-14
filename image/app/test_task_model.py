"""Offline tests for the task action's per-invocation model handling
(add-task-model-flag: submit-time validation, ack echo, argv mapping,
status-object fields)."""

from __future__ import annotations

import __future__
import asyncio
import importlib.util
import json
import os
import sqlite3
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
    module_name = "sch_task_model_main"
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

# Hermetic spool dir (add-task-liveness-safety, task 4.4): _telegram_notify
# now falls back to the notifier's spool dir when the singleton does not exist
# yet, and this module submits/finishes tasks with no notifier registered. In a
# real microVM (SCH_TELEGRAM_* set) those events would otherwise land in the
# LIVE shim's spool and be delivered for real.
_SPOOL_TMP = None
_SPOOL_SAVED = None


def setUpModule():
    global _SPOOL_TMP, _SPOOL_SAVED  # noqa: PLW0603
    _SPOOL_TMP = tempfile.TemporaryDirectory()
    _SPOOL_SAVED = main.telegram_notifier.SPOOL_DIR
    main.telegram_notifier.SPOOL_DIR = Path(_SPOOL_TMP.name) / "spool"


def tearDownModule():
    main.telegram_notifier.SPOOL_DIR = _SPOOL_SAVED
    _SPOOL_TMP.cleanup()

MODEL = "eu.anthropic.claude-haiku-4-5-20251001-v1:0"


class FakeThread:
    """Records constructor args and never runs the target."""

    instances = []

    def __init__(self, *args, **kwargs):
        self.args = kwargs.get("args", ())
        self.kwargs = kwargs
        FakeThread.instances.append(self)

    def start(self):
        return None

    def join(self, *_args, **_kwargs):
        return None


class TaskModelTests(unittest.TestCase):
    def setUp(self):
        main._HARNESS["value"] = "opencode"
        main._WORKSPACE_NAME["value"] = "workspace"
        main._SESSION_EPOCH["value"] = 0
        main._STORAGE_BACKEND["value"] = "session"
        main.CHECKPOINT_BUCKET = ""
        self._reset_slot()
        FakeThread.instances = []
        self.uploads = []

        patches = [
            patch.object(main.threading, "Thread", FakeThread),
            patch.object(
                main, "_upload_task_status",
                side_effect=lambda ws, status: self.uploads.append(dict(status)) or True,
            ),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _reset_slot(self):
        with main._TASK_LOCK:
            main._TASK_STATE.update({
                "task_id": None,
                "state": "idle",
                "prompt": None,
                "harness": None,
                "model": None,
                "variant": None,
                "thread": None,
                "async_task_handle": None,
            })
        main._TASK_STATUS.clear()
        main._TASK_STATUS["state"] = "none"

    def submit(self, **updates):
        payload = {
            "action": "task",
            "workspace": "workspace",
            "harness": "opencode",
            "prompt": "build and test",
        }
        payload.update(updates)
        return main.invoke(payload)

    # --- submit-time validation (spec: "Malformed model field rejected
    # without effects") ---------------------------------------------------------

    def test_malformed_model_rejected_without_any_mutation(self):
        for model in ("", "model with spaces", "provider/model$id", 123, None):
            with self.subTest(model=model):
                response = self.submit(model=model)
                self.assertEqual(response["status"], "error")
                self.assertIn("model", response["message"])
                # No slot mutation, no S3 upload, no worker thread.
                self.assertEqual(main._TASK_STATE["state"], "idle")
                self.assertIsNone(main._TASK_STATE["task_id"])
                self.assertEqual(self.uploads, [])
                self.assertEqual(FakeThread.instances, [])

    def test_slot_stays_free_for_subsequent_correct_submit(self):
        rejected = self.submit(model="bad model")
        self.assertEqual(rejected["status"], "error")

        accepted = self.submit(model=MODEL)
        self.assertEqual(accepted["status"], "accepted")
        self.assertTrue(accepted["task_id"])

    # --- valid model: ack echo + state/status propagation --------------------

    def test_valid_model_is_echoed_and_recorded(self):
        response = self.submit(model=MODEL)

        self.assertEqual(response["status"], "accepted")
        self.assertEqual(response["model"], MODEL)
        self.assertEqual(response["harness"], "opencode")
        self.assertEqual(main._TASK_STATE["model"], MODEL)
        # Initial S3 status object carries the model.
        self.assertEqual(len(self.uploads), 1)
        self.assertEqual(self.uploads[0]["model"], MODEL)
        self.assertEqual(main._TASK_STATUS["model"], MODEL)
        # Worker thread receives model and variant as its last two
        # positional args (variant None when not requested).
        self.assertEqual(len(FakeThread.instances), 1)
        self.assertEqual(FakeThread.instances[0].args[-2], MODEL)
        self.assertIsNone(FakeThread.instances[0].args[-1])

    def test_running_info_surface_includes_model(self):
        self.submit(model=MODEL)
        info = main._task_info_field()
        self.assertEqual(info["state"], "running")
        self.assertEqual(info["model"], MODEL)

    # --- cold-boot submit (add-task-liveness-safety, task 4.4) ---------------

    def test_submit_before_the_notifier_exists_spools_the_event(self):
        # The `task` action is answered while _bootstrap is still restoring the
        # mount, i.e. before _start_telegram_notifier ran: the submit
        # notification must land in the spool instead of being dropped (spec:
        # 'No lifecycle event lost at cold boot').
        spool = main.telegram_notifier.SPOOL_DIR
        if spool.is_dir():
            for stale in spool.iterdir():
                stale.unlink()
        main._TELEGRAM["notifier"] = None
        with patch.dict(os.environ, {
            "SCH_TELEGRAM_BOT_TOKEN": "tok", "SCH_TELEGRAM_CHAT_ID": "-100999",
        }):
            response = self.submit(model=MODEL)
        self.assertEqual(response["status"], "accepted")
        events = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(spool.iterdir())
        ]
        self.assertEqual([event["type"] for event in events], ["task-submitted"])
        self.assertEqual(events[0]["workspace"], "workspace")
        self.assertEqual(events[0]["payload"]["harness"], "opencode")
        self.assertEqual(events[0]["payload"]["model"], MODEL)

    # --- absent model: byte-identical behavior -------------------------------

    def test_absent_model_keeps_ack_and_status_free_of_model(self):
        response = self.submit()

        self.assertEqual(response["status"], "accepted")
        self.assertNotIn("model", response)
        self.assertIsNone(main._TASK_STATE["model"])
        self.assertEqual(len(self.uploads), 1)
        self.assertNotIn("model", self.uploads[0])
        self.assertNotIn("model", main._TASK_STATUS)
        self.assertIsNone(FakeThread.instances[0].args[-2])
        self.assertIsNone(FakeThread.instances[0].args[-1])
        info = main._task_info_field()
        self.assertNotIn("model", info)

    def test_stale_model_from_previous_task_is_dropped(self):
        self.submit(model=MODEL)
        self._reset_slot()
        main._TASK_STATUS["model"] = MODEL  # simulate leftover terminal record

        self.submit()
        self.assertNotIn("model", main._TASK_STATUS)

    def test_stale_notification_fields_from_previous_task_are_dropped(self):
        # TASK-28: the delivery fields describe the previous terminal record;
        # a new running task must not inherit them through dict.update.
        self._reset_slot()
        main._TASK_STATUS.update({
            "notification_status": "delivered",
            "notified_utc": "2026-09-06T10:00:00Z",
            "notified_by": "shim",
        })

        self.submit()
        for field in ("notification_status", "notified_utc", "notified_by"):
            self.assertNotIn(field, main._TASK_STATUS)
            self.assertNotIn(field, self.uploads[0])

    # --- argv mapping (design D3) --------------------------------------------

    def test_opencode_argv_includes_discrete_model_pair(self):
        with patch.object(main.shutil, "which", return_value="/bin/opencode"):
            argv = main._build_headless_argv("opencode", "oc-session", "prompt", MODEL)
        self.assertEqual(
            argv,
            [
                "/bin/opencode", "run", "--session", "oc-session",
                "--model", MODEL, "--agent", main._TASK_AGENT,
                *main._TASK_AUTO_APPROVE_FLAGS, "prompt",
            ],
        )

    def test_claude_argv_includes_discrete_model_pair(self):
        argv = main._build_headless_argv("claude", "session-id", "prompt", MODEL)
        self.assertEqual(
            argv,
            [
                "claude", "-p", "--resume", "session-id", "--model", MODEL,
                "--agent", main._TASK_AGENT,
                "--dangerously-skip-permissions", "prompt",
            ],
        )

    def test_argv_unchanged_when_model_absent(self):
        for model in (None, ""):
            with self.subTest(model=model):
                with patch.object(main.shutil, "which", return_value="/bin/opencode"):
                    self.assertEqual(
                        main._build_headless_argv("opencode", None, "p", model),
                        main._build_headless_argv("opencode", None, "p"),
                    )
                self.assertEqual(
                    main._build_headless_argv("claude", None, "p", model),
                    main._build_headless_argv("claude", None, "p"),
                )

    # --- heartbeat and terminal record ---------------------------------------

    def test_heartbeat_preserves_model_field(self):
        self.submit(model=MODEL)
        task_id = main._TASK_STATE["task_id"]

        class OneIterationStop:
            """is_set: False for the first loop iteration, True afterwards."""

            def __init__(self):
                self.checks = 0

            def is_set(self):
                self.checks += 1
                # Loop guard (False), post-wait check (False), then stop.
                return self.checks > 2

            def wait(self, _timeout):
                return None

        calls = []
        with patch.object(
            main, "_download_task_status",
            return_value={"state": "running", "task_id": task_id},
        ), patch.object(
            main, "_upload_task_status",
            side_effect=lambda ws, s: calls.append(dict(s)) or True,
        ):
            main._run_task_heartbeat(
                "workspace", task_id, OneIterationStop(), "opencode", MODEL,
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["model"], MODEL)
        self.assertEqual(calls[0]["harness"], "opencode")

    def test_finish_task_terminal_record_includes_model_only_when_requested(self):
        import threading as real_threading

        for model, expect_key in ((MODEL, True), (None, False)):
            with self.subTest(model=model):
                self.uploads.clear()
                stop = real_threading.Event()
                thread = real_threading.Thread(target=lambda: None)
                thread.start()
                with patch.object(main, "_do_checkpoint", return_value={
                    "status": "ok", "manifest_written": True, "db_backup": "ok",
                }):
                    main._finish_task(
                        task_id="t-1",
                        prompt="p",
                        started=main._utcnow(),
                        started_ts=0.0,
                        harness="opencode",
                        harness_session_id=None,
                        state="succeeded",
                        exit_code=0,
                        error=None,
                        output="done",
                        output_truncated=False,
                        workspace="workspace",
                        async_handle=None,
                        heartbeat_stop=stop,
                        heartbeat_thread=thread,
                        model=model,
                    )
                self.assertEqual(len(self.uploads), 1)
                if expect_key:
                    self.assertEqual(self.uploads[0]["model"], MODEL)
                else:
                    self.assertNotIn("model", self.uploads[0])


class OpencodeContinueModelTests(unittest.TestCase):
    """`sch task --continue` without `--model` preserves the TUI-selected
    model and reasoning-effort variant stored on the resumed session row."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self._saved_db = main.OPENCODE_DB_LOCAL
        self._saved_config = main.OPENCODE_CONFIG_FILE
        self._saved_keys = main.PROVIDER_KEYS_FILE
        main.OPENCODE_DB_LOCAL = root / "opencode.db"
        main.OPENCODE_CONFIG_FILE = root / "opencode.json"
        main.PROVIDER_KEYS_FILE = root / "provider-keys.env"
        self.addCleanup(setattr, main, "OPENCODE_DB_LOCAL", self._saved_db)
        self.addCleanup(setattr, main, "OPENCODE_CONFIG_FILE", self._saved_config)
        self.addCleanup(setattr, main, "PROVIDER_KEYS_FILE", self._saved_keys)
        main.OPENCODE_CONFIG_FILE.write_text(json.dumps({
            "model": "amazon-bedrock/remote-default",
            "provider": {"amazon-bedrock": {"options": {}}},
        }))

    def _stage_keys(self, *names):
        main.PROVIDER_KEYS_FILE.write_text(
            "".join("{}=test-value\n".format(name) for name in names)
        )

    def _write_session(self, session_id, model_json):
        main.OPENCODE_DB_LOCAL.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(main.OPENCODE_DB_LOCAL)) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS session "
                "(id TEXT PRIMARY KEY, directory TEXT, time_updated INTEGER, title TEXT)"
            )
            try:
                conn.execute("ALTER TABLE session ADD COLUMN model TEXT")
            except sqlite3.OperationalError:
                pass  # column already present on a second write
            conn.execute(
                "INSERT OR REPLACE INTO session (id, directory, time_updated, title, model)"
                " VALUES (?, ?, ?, ?, ?)",
                (session_id, "/repo", 1, "t", model_json),
            )

    def test_stored_model_and_variant_are_preserved(self):
        self._write_session("ses_tui", json.dumps({
            "providerID": "amazon-bedrock", "id": "muse-spark-1.3", "variant": "high",
        }))
        self.assertEqual(
            main._opencode_continue_model("ses_tui"),
            ("amazon-bedrock/muse-spark-1.3", "high"),
        )

    def test_stored_model_without_variant_preserves_model_only(self):
        self._write_session("ses_plain", json.dumps({
            "providerID": "amazon-bedrock", "id": "some-model",
        }))
        self.assertEqual(
            main._opencode_continue_model("ses_plain"),
            ("amazon-bedrock/some-model", None),
        )

    def test_default_variant_sentinel_is_dropped(self):
        self._write_session("ses_def", json.dumps({
            "providerID": "amazon-bedrock", "id": "some-model", "variant": "default",
        }))
        self.assertEqual(
            main._opencode_continue_model("ses_def"),
            ("amazon-bedrock/some-model", None),
        )

    def test_unavailable_provider_falls_back_to_default_without_variant(self):
        # Existing imported-session override keeps working, and the stored
        # variant (which belongs to the unavailable provider's model) is not
        # carried over to the default model.
        self._write_session("ses_foreign", json.dumps({
            "providerID": "github-copilot", "id": "other-model", "variant": "high",
        }))
        self.assertEqual(
            main._opencode_continue_model("ses_foreign"),
            ("amazon-bedrock/remote-default", None),
        )

    def test_staged_key_provider_is_preserved(self):
        # A key-based provider backed by a staged key CAN serve at runtime
        # (the key makes it selectable with no login), so it must not hit
        # the unavailable-provider override — this is the reported case.
        self._stage_keys("SCH_OPENCODE_API_KEY")
        self._write_session("ses_keyed", json.dumps({
            "providerID": "opencode", "id": "muse-spark-1.3-contributor-free",
            "variant": "xhigh",
        }))
        self.assertEqual(
            main._opencode_continue_model("ses_keyed"),
            ("opencode/muse-spark-1.3-contributor-free", "xhigh"),
        )

    def test_key_removed_mid_life_falls_back_to_default(self):
        # Staging is total replacement ("last invoke wins"): a key removed
        # after the session was created is gone at runtime, so the override
        # correctly fires again.
        self._stage_keys("SCH_OPENCODE_API_KEY")
        self._write_session("ses_keyed", json.dumps({
            "providerID": "opencode", "id": "muse-spark-1.3-contributor-free",
            "variant": "xhigh",
        }))
        self.assertEqual(
            main._opencode_continue_model("ses_keyed")[0],
            "opencode/muse-spark-1.3-contributor-free",
        )
        self._stage_keys()  # user removed the key; empty staging file
        self.assertEqual(
            main._opencode_continue_model("ses_keyed"),
            ("amazon-bedrock/remote-default", None),
        )

    def test_missing_session_degrades_to_default(self):
        self.assertEqual(main._opencode_continue_model(None), (None, None))
        self.assertEqual(main._opencode_continue_model("ses_absent"), (None, None))
        # No DB file at all (fresh workspace): same degrade path.
        main.OPENCODE_DB_LOCAL.unlink(missing_ok=True)
        self.assertEqual(main._opencode_continue_model("ses_absent"), (None, None))

    def test_malformed_stored_model_degrades_to_default(self):
        # Configured provider but an unusable stored shape: nothing safe to
        # forward, so degrade to the harness default.
        for bad in (
            "not-json", "[1, 2]", '{"id": "b"}',
            json.dumps({"providerID": "amazon-bedrock"}),
            json.dumps({"providerID": "amazon-bedrock", "id": "c d"}),
            json.dumps({"providerID": "amazon-bedrock", "id": 123}),
        ):
            with self.subTest(bad=bad):
                self._write_session("ses_bad", bad)
                self.assertEqual(
                    main._opencode_continue_model("ses_bad"), (None, None)
                )

    def test_malformed_row_with_unconfigured_provider_uses_default(self):
        # The pre-existing imported-session override keys off the provider
        # alone: an unconfigured provider can never serve, whatever else the
        # row holds, so the runtime default still wins.
        for bad in (
            '{"providerID": "a"}',
            json.dumps({"providerID": "a b", "id": "c"}),
            json.dumps({"providerID": "a", "id": "c d"}),
        ):
            with self.subTest(bad=bad):
                self._write_session("ses_bad", bad)
                self.assertEqual(
                    main._opencode_continue_model("ses_bad"),
                    ("amazon-bedrock/remote-default", None),
                )

    def test_missing_model_column_degrades_to_default(self):
        # Older opencode.db without the model column: SELECT raises, caught.
        main.OPENCODE_DB_LOCAL.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(main.OPENCODE_DB_LOCAL)) as conn:
            conn.execute(
                "CREATE TABLE session "
                "(id TEXT PRIMARY KEY, directory TEXT, time_updated INTEGER, title TEXT)"
            )
            conn.execute(
                "INSERT INTO session VALUES (?, ?, ?, ?)",
                ("ses_old", "/repo", 1, "t"),
            )
        self.assertEqual(main._opencode_continue_model("ses_old"), (None, None))

    def test_opencode_argv_carries_model_and_variant_pairs(self):
        with patch.object(main.shutil, "which", return_value="/bin/opencode"):
            argv = main._build_headless_argv(
                "opencode", "ses_tui", "prompt",
                "amazon-bedrock/muse-spark-1.3", "high",
            )
        self.assertEqual(
            argv,
            [
                "/bin/opencode", "run", "--session", "ses_tui",
                "--model", "amazon-bedrock/muse-spark-1.3",
                "--variant", "high",
                "--agent", main._TASK_AGENT,
                *main._TASK_AUTO_APPROVE_FLAGS, "prompt",
            ],
        )

    def test_malformed_variant_is_dropped_but_model_kept(self):
        with patch.object(main.shutil, "which", return_value="/bin/opencode"):
            argv = main._build_headless_argv(
                "opencode", "ses_tui", "prompt", "prov/mod", "bad variant",
            )
        self.assertIn("--model", argv)
        self.assertNotIn("--variant", argv)

    def test_variant_is_ignored_for_other_harnesses(self):
        argv = main._build_headless_argv("claude", "ses", "prompt", MODEL, "high")
        self.assertIn("--model", argv)
        self.assertNotIn("--variant", argv)
        with patch.object(main.shutil, "which", return_value="/bin/pi"), patch.object(
            type(main.PI_ROLE_REMOTE_AUTO), "is_file", return_value=False,
        ):
            pi_argv = main._build_headless_argv("pi", None, "prompt", MODEL, "high")
        self.assertNotIn("--variant", pi_argv)

    def test_argv_without_variant_is_byte_identical_to_before(self):
        with patch.object(main.shutil, "which", return_value="/bin/opencode"):
            self.assertEqual(
                main._build_headless_argv("opencode", "ses", "p", MODEL),
                main._build_headless_argv("opencode", "ses", "p", MODEL, None),
            )


class TaskVariantTests(unittest.TestCase):
    """Explicit `--variant` handling mirrors `--model` (present-iff-requested
    on ack, state, status, heartbeat, terminal record and info)."""

    def setUp(self):
        main._HARNESS["value"] = "opencode"
        main._WORKSPACE_NAME["value"] = "workspace"
        main._SESSION_EPOCH["value"] = 0
        main._STORAGE_BACKEND["value"] = "session"
        main.CHECKPOINT_BUCKET = ""
        self._reset_slot()
        FakeThread.instances = []
        self.uploads = []

        patches = [
            patch.object(main.threading, "Thread", FakeThread),
            patch.object(
                main, "_upload_task_status",
                side_effect=lambda ws, status: self.uploads.append(dict(status)) or True,
            ),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _reset_slot(self):
        with main._TASK_LOCK:
            main._TASK_STATE.update({
                "task_id": None,
                "state": "idle",
                "prompt": None,
                "harness": None,
                "model": None,
                "variant": None,
                "thread": None,
                "async_task_handle": None,
            })
        main._TASK_STATUS.clear()
        main._TASK_STATUS["state"] = "none"

    def submit(self, **updates):
        payload = {
            "action": "task",
            "workspace": "workspace",
            "harness": "opencode",
            "prompt": "build and test",
        }
        payload.update(updates)
        return main.invoke(payload)

    def test_malformed_variant_rejected_without_any_mutation(self):
        for variant in ("", "high effort", "high;rm", 123, None):
            with self.subTest(variant=variant):
                response = self.submit(variant=variant)
                self.assertEqual(response["status"], "error")
                self.assertIn("variant", response["message"])
                self.assertEqual(main._TASK_STATE["state"], "idle")
                self.assertIsNone(main._TASK_STATE["task_id"])
                self.assertEqual(self.uploads, [])
                self.assertEqual(FakeThread.instances, [])

    def test_valid_variant_is_echoed_and_recorded(self):
        response = self.submit(model=MODEL, variant="high")

        self.assertEqual(response["status"], "accepted")
        self.assertEqual(response["model"], MODEL)
        self.assertEqual(response["variant"], "high")
        self.assertEqual(main._TASK_STATE["variant"], "high")
        self.assertEqual(len(self.uploads), 1)
        self.assertEqual(self.uploads[0]["variant"], "high")
        self.assertEqual(main._TASK_STATUS["variant"], "high")
        self.assertEqual(len(FakeThread.instances), 1)
        self.assertEqual(FakeThread.instances[0].args[-2], MODEL)
        self.assertEqual(FakeThread.instances[0].args[-1], "high")
        info = main._task_info_field()
        self.assertEqual(info["variant"], "high")

    def test_variant_without_model_is_accepted(self):
        response = self.submit(variant="high")
        self.assertEqual(response["status"], "accepted")
        self.assertEqual(response["variant"], "high")
        self.assertNotIn("model", response)

    def test_absent_variant_stays_absent(self):
        response = self.submit(model=MODEL)

        self.assertEqual(response["status"], "accepted")
        self.assertNotIn("variant", response)
        self.assertIsNone(main._TASK_STATE["variant"])
        self.assertNotIn("variant", self.uploads[0])
        self.assertNotIn("variant", main._TASK_STATUS)
        self.assertIsNone(FakeThread.instances[0].args[-1])
        self.assertNotIn("variant", main._task_info_field())

    def test_stale_variant_from_previous_task_is_dropped(self):
        self.submit(model=MODEL, variant="high")
        self._reset_slot()
        main._TASK_STATUS["variant"] = "high"  # simulate leftover record

        self.submit(model=MODEL)
        self.assertNotIn("variant", main._TASK_STATUS)

    def test_heartbeat_preserves_variant_field(self):
        self.submit(model=MODEL, variant="high")
        task_id = main._TASK_STATE["task_id"]

        class OneIterationStop:
            def __init__(self):
                self.checks = 0

            def is_set(self):
                self.checks += 1
                return self.checks > 2

            def wait(self, _timeout):
                return None

        calls = []
        with patch.object(
            main, "_download_task_status",
            return_value={"state": "running", "task_id": task_id},
        ), patch.object(
            main, "_upload_task_status",
            side_effect=lambda ws, s: calls.append(dict(s)) or True,
        ):
            main._run_task_heartbeat(
                "workspace", task_id, OneIterationStop(), "opencode",
                MODEL, "high",
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["variant"], "high")
        self.assertEqual(calls[0]["model"], MODEL)

    def test_finish_task_terminal_record_includes_variant_only_when_requested(self):
        import threading as real_threading

        for variant, expect_key in (("high", True), (None, False)):
            with self.subTest(variant=variant):
                self.uploads.clear()
                stop = real_threading.Event()
                thread = real_threading.Thread(target=lambda: None)
                thread.start()
                with patch.object(main, "_do_checkpoint", return_value={
                    "status": "ok", "manifest_written": True, "db_backup": "ok",
                }):
                    main._finish_task(
                        task_id="t-1",
                        prompt="p",
                        started=main._utcnow(),
                        started_ts=0.0,
                        harness="opencode",
                        harness_session_id=None,
                        state="succeeded",
                        exit_code=0,
                        error=None,
                        output="done",
                        output_truncated=False,
                        workspace="workspace",
                        async_handle=None,
                        heartbeat_stop=stop,
                        heartbeat_thread=thread,
                        model=MODEL,
                        variant=variant,
                    )
                self.assertEqual(len(self.uploads), 1)
                if expect_key:
                    self.assertEqual(self.uploads[0]["variant"], "high")
                else:
                    self.assertNotIn("variant", self.uploads[0])


if __name__ == "__main__":
    unittest.main()
