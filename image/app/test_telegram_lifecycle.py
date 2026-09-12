"""Offline tests for the Telegram lifecycle emission in the shim
(add-telegram-notifications, task 6.2): terminal notification emitted even
with a failed checkpoint, stall notified at most once per task, feature off
means zero side effects. add-task-liveness-safety (task 4.4): lifecycle
events emitted before the notifier exists are spooled, not dropped."""

from __future__ import annotations

import __future__
import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
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
    module_name = "sch_telegram_lifecycle_main"
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


class FakeNotifier:
    def __init__(self):
        self.events = []
        self.activity_age = None
        self.flushed = 0

    def notify(self, etype, payload, workspace=None):
        self.events.append((etype, payload, workspace))

    def last_activity_age(self, _workspace):
        return self.activity_age

    def flush(self, timeout=5.0):
        self.flushed += 1


class FeatureOffTests(unittest.TestCase):
    def setUp(self):
        main._TELEGRAM["notifier"] = None

    def test_start_is_noop_without_env(self):
        env = dict(os.environ)
        env.pop("SCH_TELEGRAM_BOT_TOKEN", None)
        env.pop("SCH_TELEGRAM_CHAT_ID", None)
        with patch.dict(os.environ, env, clear=True):
            main._start_telegram_notifier()
        self.assertIsNone(main._TELEGRAM["notifier"])

    def test_notify_is_silent_noop_when_unconfigured(self):
        # Must not raise, must not create anything (spec: byte-identical).
        # The env is cleared explicitly: a microVM really does carry the
        # SCH_TELEGRAM_* values, and inheriting them here would exercise the
        # cold-boot spool path (and write into the live shim's spool dir).
        with tempfile.TemporaryDirectory() as tmp:
            spool = Path(tmp) / "spool"
            env = dict(os.environ)
            env.pop("SCH_TELEGRAM_BOT_TOKEN", None)
            env.pop("SCH_TELEGRAM_CHAT_ID", None)
            with patch.dict(os.environ, env, clear=True), \
                 patch.object(main.telegram_notifier, "SPOOL_DIR", spool):
                main._telegram_notify("task-terminal", {"task_id": "x"}, "ws")
            self.assertFalse(spool.exists())

    def test_flush_is_noop_when_unconfigured(self):
        main._flush_telegram_notifier()

    def test_stall_check_is_noop_when_unconfigured(self):
        main._TELEGRAM_STALLED_TASKS.clear()
        main._check_task_stall("ws", "t1", {"heartbeat_utc": "2000-01-01T00:00:00Z"}, 0.0)
        self.assertEqual(main._TELEGRAM_STALLED_TASKS, set())


class EnabledMarkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.marker = Path(self.tmp.name) / "telegram-enabled"

    def test_marker_is_atomic_and_contains_only_capability(self):
        env = {
            "SCH_TELEGRAM_BOT_TOKEN": "secret-token",
            "SCH_TELEGRAM_CHAT_ID": "-100999",
        }
        with patch.dict(os.environ, env, clear=False), \
             patch.object(main, "TELEGRAM_ENABLED_MARKER", self.marker), \
             patch.object(main, "_telegram_interaction_enabled", return_value=True):
            main._publish_telegram_enabled_marker()
        data = json.loads(self.marker.read_text(encoding="utf-8"))
        self.assertEqual(data, {"version": 1, "interaction_enabled": True})
        self.assertNotIn("secret-token", self.marker.read_text(encoding="utf-8"))
        self.assertEqual(list(self.marker.parent.glob("*.tmp")), [])

    def test_disabled_and_shutdown_paths_remove_marker(self):
        self.marker.write_text("stale", encoding="utf-8")
        env = dict(os.environ)
        env.pop("SCH_TELEGRAM_BOT_TOKEN", None)
        env.pop("SCH_TELEGRAM_CHAT_ID", None)
        with patch.dict(os.environ, env, clear=True), \
             patch.object(main, "TELEGRAM_ENABLED_MARKER", self.marker):
            main._publish_telegram_enabled_marker()
            self.assertFalse(self.marker.exists())
            self.marker.write_text("active", encoding="utf-8")
            main._flush_telegram_notifier()
            self.assertFalse(self.marker.exists())


class ColdBootSpoolTests(unittest.TestCase):
    """add-task-liveness-safety (task 4.4, design D6), spec: 'No lifecycle
    event lost at cold boot'.

    The notifier singleton is created at the END of _bootstrap while the
    `task` action is already servable, so an event emitted in that window has
    nowhere in-process to go. It must land in the spool dir the notifier polls,
    not on the floor.
    """

    def setUp(self):
        main._TELEGRAM["notifier"] = None
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.spool = Path(self.tmp.name) / "spool"
        patcher = patch.object(main.telegram_notifier, "SPOOL_DIR", self.spool)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.configured = patch.dict(os.environ, {
            "SCH_TELEGRAM_BOT_TOKEN": "tok",
            "SCH_TELEGRAM_CHAT_ID": "-100999",
        })

    def spooled(self):
        if not self.spool.is_dir():
            return []
        return [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(self.spool.iterdir())
            if path.name.endswith(".json")
        ]

    def test_lifecycle_event_emitted_before_the_notifier_is_spooled(self):
        with self.configured:
            main._telegram_notify("task-submitted", {"task_id": "abc", "harness": "claude"}, "ws")
        events = self.spooled()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "task-submitted")
        self.assertEqual(events[0]["workspace"], "ws")
        self.assertEqual(events[0]["payload"]["task_id"], "abc")
        self.assertEqual(events[0]["source"], "shim")
        self.assertIsInstance(events[0]["ts"], float)
        # tmp-then-rename: no partial file left for the poller to trip on.
        self.assertEqual([p.name for p in self.spool.iterdir() if p.name.endswith(".tmp")], [])

    def test_spooled_event_is_delivered_when_the_notifier_starts(self):
        # End-to-end on the real notifier: the event the shim spooled during
        # boot must reach the Bot API call, with its own priority class.
        with self.configured:
            main._telegram_notify("task-submitted", {"task_id": "abc", "harness": "claude"}, "ws")
        tn = main.telegram_notifier
        sent = []

        class FakeClient:
            def send_message(self, chat_id, text, message_thread_id=None, **_kwargs):
                sent.append((chat_id, text, message_thread_id))
                return {"message_id": len(sent)}

            def create_forum_topic(self, _chat_id, name):
                return 7

        notifier = tn.Notifier(
            "tok", "-100999", client=FakeClient(),
            spool_dir=self.spool, min_topic_interval=0.0,
            default_workspace=lambda: "ws",
        )
        notifier._poll_spool()
        with notifier._lock:
            priorities = [prio for prio, _seq, _ev in notifier._queue]
        self.assertEqual(priorities, [tn.PRIO_TERMINAL])
        notifier._drain_once()
        self.assertEqual(len(sent), 1)
        self.assertIn("abc", sent[0][1])
        self.assertEqual(self.spooled(), [])

    def test_registered_notifier_keeps_the_direct_path(self):
        notifier = FakeNotifier()
        main._TELEGRAM["notifier"] = notifier
        self.addCleanup(lambda: main._TELEGRAM.update({"notifier": None}))
        with self.configured:
            main._telegram_notify("task-terminal", {"task_id": "z"}, "ws")
        self.assertEqual(len(notifier.events), 1)
        self.assertFalse(self.spool.exists())

    def test_unwritable_spool_degrades_to_a_warning(self):
        # An observability path must never break a submit.
        blocker = Path(self.tmp.name) / "blocker"
        blocker.write_text("not a directory")
        with patch.object(main.telegram_notifier, "SPOOL_DIR", blocker / "spool"), \
             self.configured, \
             self.assertLogs("sch_shim", level="WARNING") as captured:
            main._telegram_notify("task-submitted", {"task_id": "abc"}, "ws")
        self.assertTrue([line for line in captured.output if "spool fallback failed" in line])


class TerminalEmissionTests(unittest.TestCase):
    def setUp(self):
        self.notifier = FakeNotifier()
        main._TELEGRAM["notifier"] = self.notifier
        self.addCleanup(lambda: main._TELEGRAM.update({"notifier": None}))
        main._WORKSPACE_NAME["value"] = "ws"
        main.CHECKPOINT_BUCKET = ""

    def _finish(self, checkpoint_side_effect):
        stop = threading.Event()
        thread = threading.Thread(target=lambda: None)
        thread.start()
        with patch.object(main, "_do_checkpoint", side_effect=checkpoint_side_effect), \
             patch.object(main, "_upload_task_status", return_value=False):
            main._finish_task(
                task_id="task-1234", prompt="p", started=main._utcnow(),
                started_ts=time.time() - 7, harness="claude",
                harness_session_id=None, state="failed", exit_code=3,
                error="boom", output="", output_truncated=False,
                workspace="ws", async_handle=None,
                heartbeat_stop=stop, heartbeat_thread=thread,
            )

    def test_terminal_notified_even_when_checkpoint_and_upload_fail(self):
        self._finish(RuntimeError("checkpoint exploded"))
        terminals = [e for e in self.notifier.events if e[0] == "task-terminal"]
        self.assertEqual(len(terminals), 1)
        _etype, payload, workspace = terminals[0]
        self.assertEqual(workspace, "ws")
        self.assertEqual(payload["state"], "failed")
        self.assertEqual(payload["checkpoint_status"], "failed")
        self.assertEqual(payload["exit_code"], 3)

    def test_terminal_payload_has_confirmed_checkpoint_on_success(self):
        self._finish(lambda force: {
            "status": "ok", "manifest_written": True, "db_backup": "ok",
        })
        payload = [e for e in self.notifier.events if e[0] == "task-terminal"][0][1]
        self.assertEqual(payload["checkpoint_status"], "confirmed")


class TerminalDeliveryProtocolTests(unittest.TestCase):
    """TASK-28: the terminal record promises a notification
    (``notification_status: pending``) iff the channel is configured, and the
    shim flips it to ``delivered`` only through a fenced conditional write."""

    CONFIGURED = {"SCH_TELEGRAM_BOT_TOKEN": "tok", "SCH_TELEGRAM_CHAT_ID": "-100999"}

    def setUp(self):
        self.notifier = FakeNotifier()
        main._TELEGRAM["notifier"] = self.notifier
        self.addCleanup(lambda: main._TELEGRAM.update({"notifier": None}))
        main._WORKSPACE_NAME["value"] = "ws"
        main.CHECKPOINT_BUCKET = ""
        self.uploads = []

    def _finish(self, env):
        stop = threading.Event()
        thread = threading.Thread(target=lambda: None)
        thread.start()
        checkpoint = lambda force: {  # noqa: E731
            "status": "ok", "manifest_written": True, "db_backup": "ok",
        }
        with patch.dict(os.environ, env, clear=True), \
             patch.object(main, "_do_checkpoint", side_effect=checkpoint), \
             patch.object(main, "_upload_task_status",
                          side_effect=lambda ws, status: self.uploads.append(dict(status)) or True):
            main._finish_task(
                task_id="task-1234", prompt="p", started=main._utcnow(),
                started_ts=time.time() - 7, harness="claude",
                harness_session_id=None, state="succeeded", exit_code=0,
                error=None, output="", output_truncated=False,
                workspace="ws", async_handle=None,
                heartbeat_stop=stop, heartbeat_thread=thread,
            )

    def test_terminal_record_is_pending_when_the_channel_is_configured(self):
        self._finish(self.CONFIGURED)
        self.assertEqual(len(self.uploads), 1)
        self.assertEqual(self.uploads[0]["notification_status"], "pending")
        self.assertNotIn("notified_utc", self.uploads[0])
        # The pending mark is written BEFORE the event is enqueued: a microVM
        # dying right here leaves a record the watchdog will re-send.
        self.assertTrue([e for e in self.notifier.events if e[0] == "task-terminal"])

    def test_terminal_record_is_byte_identical_when_the_channel_is_off(self):
        env = dict(os.environ)
        env.pop("SCH_TELEGRAM_BOT_TOKEN", None)
        env.pop("SCH_TELEGRAM_CHAT_ID", None)
        self._finish(env)
        self.assertEqual(len(self.uploads), 1)
        for field in main.NOTIFICATION_FIELDS:
            self.assertNotIn(field, self.uploads[0])


class _FakeBody:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload


class _FakeClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _FakeS3:
    """Just enough S3 for the delivered-mark write, with ETag semantics."""

    def __init__(self):
        self.objects = {}
        self.puts = []
        self._etag = 0

    def store(self, key, payload):
        self._etag += 1
        self.objects[key] = {"body": json.dumps(payload).encode(),
                             "etag": f'"etag-{self._etag}"'}
        return self.objects[key]["etag"]

    def stored(self, key):
        return json.loads(self.objects[key]["body"].decode())

    def get_object(self, Bucket, Key):  # noqa: N803
        if Key not in self.objects:
            raise _FakeClientError("NoSuchKey")
        entry = self.objects[Key]
        return {"Body": _FakeBody(entry["body"]), "ETag": entry["etag"]}

    def put_object(self, Bucket, Key, Body, ContentType=None, IfMatch=None, IfNoneMatch=None):  # noqa: N803
        current = self.objects.get(Key)
        if IfMatch is not None and (current is None or current["etag"] != IfMatch):
            raise _FakeClientError("PreconditionFailed")
        if IfNoneMatch == "*" and current is not None:
            raise _FakeClientError("PreconditionFailed")
        self.puts.append({"Key": Key, "Body": json.loads(Body.decode()), "IfMatch": IfMatch})
        self._etag += 1
        self.objects[Key] = {"body": Body, "etag": f'"etag-{self._etag}"'}
        return {"ETag": self.objects[Key]["etag"]}


class DeliveredMarkTests(unittest.TestCase):
    KEY = "checkpoints/ws/task-status.json"

    def setUp(self):
        self.s3 = _FakeS3()
        main.CHECKPOINT_BUCKET = "bucket-tests"
        self.addCleanup(setattr, main, "CHECKPOINT_BUCKET", "")
        for p in (
            patch.object(main, "_s3", return_value=self.s3),
            patch.object(main, "_assert_writer_claim", return_value=None),
        ):
            p.start()
            self.addCleanup(p.stop)

    def terminal(self, **extra):
        record = {
            "task_id": "task-1234", "state": "succeeded", "harness": "claude",
            "finished_utc": "2026-09-06T10:00:00Z", "duration_s": 161,
            "checkpoint_status": "confirmed", "notification_status": "pending",
            "writer_token": main._WRITER_TOKEN, "session_epoch": 3,
        }
        record.update(extra)
        return record

    def test_pending_record_becomes_delivered_with_a_conditional_write(self):
        etag = self.s3.store(self.KEY, self.terminal())
        self.assertTrue(main._mark_task_terminal_notified("ws", "task-1234"))
        self.assertEqual(len(self.s3.puts), 1)
        self.assertEqual(self.s3.puts[0]["IfMatch"], etag)
        stored = self.s3.stored(self.KEY)
        self.assertEqual(stored["notification_status"], "delivered")
        self.assertEqual(stored["notified_by"], "shim")
        self.assertTrue(stored["notified_utc"])
        # Everything else is carried over verbatim, identity fields included.
        self.assertEqual(stored["state"], "succeeded")
        self.assertEqual(stored["duration_s"], 161)
        self.assertEqual(stored["writer_token"], main._WRITER_TOKEN)
        self.assertEqual(stored["session_epoch"], 3)

    def test_record_of_another_task_is_left_alone(self):
        self.s3.store(self.KEY, self.terminal(task_id="task-9999"))
        self.assertFalse(main._mark_task_terminal_notified("ws", "task-1234"))
        self.assertEqual(self.s3.puts, [])

    def test_running_record_is_never_overwritten(self):
        # A new submit already replaced the record: marking would regress it.
        self.s3.store(self.KEY, self.terminal(state="running", notification_status=None))
        self.assertFalse(main._mark_task_terminal_notified("ws", "task-1234"))
        self.assertEqual(self.s3.puts, [])
        self.assertEqual(self.s3.stored(self.KEY)["state"], "running")

    def test_already_delivered_record_is_not_rewritten(self):
        self.s3.store(self.KEY, self.terminal(notification_status="delivered",
                                              notified_by="task-watchdog"))
        self.assertTrue(main._mark_task_terminal_notified("ws", "task-1234"))
        self.assertEqual(self.s3.puts, [])

    def test_superseded_writer_token_is_refused_without_raising(self):
        self.s3.store(self.KEY, self.terminal(writer_token="someone-else"))
        with self.assertLogs("sch_shim", level="WARNING") as captured:
            self.assertFalse(main._mark_task_terminal_notified("ws", "task-1234"))
        self.assertEqual(self.s3.puts, [])
        self.assertTrue([l for l in captured.output if "not marked delivered" in l])

    def test_lost_race_leaves_the_newer_object_authoritative(self):
        self.s3.store(self.KEY, self.terminal())
        original_get = self.s3.get_object

        def get_then_replace(Bucket, Key):  # noqa: N803
            response = original_get(Bucket, Key)
            self.s3.store(self.KEY, self.terminal(task_id="task-next", state="running"))
            return response

        self.s3.get_object = get_then_replace
        with self.assertLogs("sch_shim", level="WARNING"):
            self.assertFalse(main._mark_task_terminal_notified("ws", "task-1234"))
        self.assertEqual(self.s3.puts, [])
        self.assertEqual(self.s3.stored(self.KEY)["task_id"], "task-next")

    def test_missing_record_and_unset_bucket_are_no_ops(self):
        with self.assertLogs("sch_shim", level="WARNING"):
            self.assertFalse(main._mark_task_terminal_notified("ws", "task-1234"))
        main.CHECKPOINT_BUCKET = ""
        self.assertFalse(main._mark_task_terminal_notified("ws", "task-1234"))
        self.assertEqual(self.s3.puts, [])


class OnSentWiringTests(unittest.TestCase):
    def test_only_terminal_events_reach_the_mark(self):
        marked = []
        with patch.object(main, "_mark_task_terminal_notified",
                          side_effect=lambda ws, tid: marked.append((ws, tid)) or True):
            main._on_telegram_sent({"type": "turn-end", "workspace": "ws",
                                    "payload": {"task_id": "x"}})
            main._on_telegram_sent({"type": "task-submitted", "workspace": "ws",
                                    "payload": {"task_id": "x"}})
            main._on_telegram_sent({"type": "task-terminal", "workspace": "ws",
                                    "payload": {}})
            main._on_telegram_sent({"type": "task-terminal", "workspace": "ws",
                                    "payload": {"task_id": "task-1234"}})
        self.assertEqual(marked, [("ws", "task-1234")])

    def test_terminal_without_workspace_falls_back_to_the_shim_identity(self):
        marked = []
        main._WORKSPACE_NAME["value"] = "resolved-ws"
        with patch.object(main, "_mark_task_terminal_notified",
                          side_effect=lambda ws, tid: marked.append((ws, tid)) or True):
            main._on_telegram_sent({"type": "task-terminal", "workspace": None,
                                    "payload": {"task_id": "task-1234"}})
        self.assertEqual(marked, [("resolved-ws", "task-1234")])

    def test_notifier_is_built_with_the_hook(self):
        captured = {}

        def fake_build(**kwargs):
            captured.update(kwargs)
            return None

        main._TELEGRAM["notifier"] = None
        with patch.object(main.telegram_notifier, "build_from_env", side_effect=fake_build), \
             patch.object(main, "_remove_telegram_enabled_marker"):
            main._start_telegram_notifier()
        self.assertIs(captured.get("on_sent"), main._on_telegram_sent)


class StallTests(unittest.TestCase):
    def setUp(self):
        self.notifier = FakeNotifier()
        main._TELEGRAM["notifier"] = self.notifier
        self.addCleanup(lambda: main._TELEGRAM.update({"notifier": None}))
        main._TELEGRAM_STALLED_TASKS.clear()
        self.old_start = time.time() - main.TELEGRAM_STALL_SECONDS * 3

    def test_stale_persisted_heartbeat_triggers_once(self):
        status = {"heartbeat_utc": "2000-01-01T00:00:00Z"}
        main._check_task_stall("ws", "t1", status, self.old_start)
        main._check_task_stall("ws", "t1", status, self.old_start)
        stalls = [e for e in self.notifier.events if e[0] == "task-stall"]
        self.assertEqual(len(stalls), 1)
        self.assertEqual(stalls[0][2], "ws")
        self.assertGreater(stalls[0][1]["stalled_s"], main.TELEGRAM_STALL_SECONDS)

    def test_fresh_heartbeat_and_no_milestones_is_not_a_stall(self):
        # Hooks never emitted anything (age None): silence must NOT count as
        # a stall (spec: 'Harness without seeded hooks' degrades quietly).
        status = {"heartbeat_utc": main._utcnow()}
        self.notifier.activity_age = None
        main._check_task_stall("ws", "t2", status, self.old_start)
        self.assertEqual([e for e in self.notifier.events if e[0] == "task-stall"], [])

    def test_milestone_silence_triggers_once(self):
        status = {"heartbeat_utc": main._utcnow()}
        self.notifier.activity_age = main.TELEGRAM_STALL_SECONDS + 60
        main._check_task_stall("ws", "t3", status, self.old_start)
        main._check_task_stall("ws", "t3", status, self.old_start)
        stalls = [e for e in self.notifier.events if e[0] == "task-stall"]
        self.assertEqual(len(stalls), 1)

    def test_young_task_never_stalls(self):
        status = {"heartbeat_utc": "2000-01-01T00:00:00Z"}
        main._check_task_stall("ws", "t4", status, time.time())
        self.assertEqual([e for e in self.notifier.events if e[0] == "task-stall"], [])


class ShutdownFlushTests(unittest.TestCase):
    def test_flush_delegates_to_notifier(self):
        notifier = FakeNotifier()
        main._TELEGRAM["notifier"] = notifier
        self.addCleanup(lambda: main._TELEGRAM.update({"notifier": None}))
        main._flush_telegram_notifier()
        self.assertEqual(notifier.flushed, 1)


if __name__ == "__main__":
    unittest.main()
