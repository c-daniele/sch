"""Offline tests for the Telegram notifier module (add-telegram-notifications,
tasks 6.1): opt-in activation, queue priorities/saturation, coalescing,
truncation, topic mapping (create/reuse/recreate/fallback) with a mocked
Telegram client, spool consumption, 429 re-queue."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

import telegram_notifier as tn  # noqa: E402


class FakeClient:
    def __init__(self):
        self.sent = []            # (chat_id, text, thread_id)
        self.created = []         # workspace names
        self.next_thread_id = 100
        self.create_raises = None
        self.send_raises_once = None

    def send_message(self, chat_id, text, message_thread_id=None):
        if self.send_raises_once is not None:
            exc = self.send_raises_once
            self.send_raises_once = None
            raise exc
        self.sent.append((chat_id, tn.truncate(text), message_thread_id))
        return {"message_id": len(self.sent)}

    def create_forum_topic(self, chat_id, name):
        if self.create_raises is not None:
            raise self.create_raises
        self.created.append(name)
        self.next_thread_id += 1
        return self.next_thread_id


class MappingStore:
    def __init__(self):
        self.data = {}

    def load(self, workspace):
        return self.data.get(workspace)

    def save(self, workspace, state):
        self.data[workspace] = state


def make_notifier(client=None, store=None, **kwargs):
    client = client or FakeClient()
    store = store or MappingStore()
    notifier = tn.Notifier(
        "test-token", "-100123",
        client=client,
        load_topic_state=store.load,
        save_topic_state=store.save,
        default_workspace=lambda: "default-ws",
        spool_dir=Path(kwargs.pop("spool_dir", "/nonexistent-spool")),
        min_topic_interval=0.0,
        **kwargs,
    )
    return notifier, client, store


class ActivationTests(unittest.TestCase):
    def test_disabled_without_env(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SCH_TELEGRAM_BOT_TOKEN", None)
            os.environ.pop("SCH_TELEGRAM_CHAT_ID", None)
            self.assertFalse(tn.enabled())
            self.assertIsNone(tn.build_from_env())

    def test_disabled_with_partial_env(self):
        with patch.dict(os.environ, {"SCH_TELEGRAM_BOT_TOKEN": "t"}, clear=False):
            os.environ.pop("SCH_TELEGRAM_CHAT_ID", None)
            self.assertFalse(tn.enabled())
            self.assertIsNone(tn.build_from_env())

    def test_enabled_with_both(self):
        env = {"SCH_TELEGRAM_BOT_TOKEN": "t", "SCH_TELEGRAM_CHAT_ID": "c"}
        with patch.dict(os.environ, env, clear=False):
            self.assertTrue(tn.enabled())
            notifier = tn.build_from_env()
            self.assertIsInstance(notifier, tn.Notifier)


class TruncationTests(unittest.TestCase):
    def test_truncation_with_marker(self):
        text = "x" * 5000
        out = tn.truncate(text)
        self.assertEqual(len(out), tn.MAX_MESSAGE_CHARS)
        self.assertTrue(out.endswith(tn.TRUNCATION_MARKER))

    def test_short_text_untouched(self):
        self.assertEqual(tn.truncate("hello"), "hello")

    def test_sanitize_strips_token(self):
        self.assertNotIn("secret", tn._sanitize("boom secret boom", "secret"))


class QueueTests(unittest.TestCase):
    def test_terminal_never_dropped_on_saturation(self):
        notifier, _c, _s = make_notifier(queue_max=4)
        for i in range(4):
            notifier.notify("tool", {"summary": f"t{i}"}, "ws")
        notifier.notify("task-terminal", {"task_id": "abc"}, "ws")
        notifier.notify("task-terminal", {"task_id": "def"}, "ws")
        with notifier._lock:
            terminals = [e for p, _s2, e in notifier._queue if p == tn.PRIO_TERMINAL]
            self.assertEqual(len(terminals), 2)
            self.assertEqual(len(notifier._queue), 4)

    def test_new_digest_dropped_when_full_of_higher_priority(self):
        notifier, _c, _s = make_notifier(queue_max=2)
        notifier.notify("task-terminal", {"task_id": "a"}, "ws")
        notifier.notify("await-input", {"message": "?"}, "ws")
        notifier.notify("tool", {"summary": "noise"}, "ws")
        with notifier._lock:
            types = sorted(e.get("type") for _p, _s2, e in notifier._queue)
            self.assertEqual(types, ["await-input", "task-terminal"])

    def test_priority_order_on_drain(self):
        notifier, client, _s = make_notifier()
        notifier.notify("tool", {"summary": "later"}, "ws")
        notifier.notify("task-terminal", {"task_id": "abc", "state": "succeeded",
                                          "checkpoint_status": "confirmed",
                                          "duration_s": 3}, "ws")
        notifier._drain_once()
        self.assertIn("task", client.sent[0][1])
        self.assertIn("OK", client.sent[0][1])


class CoalescingTests(unittest.TestCase):
    def test_burst_of_tool_events_becomes_one_digest(self):
        notifier, client, _s = make_notifier()
        for i in range(5):
            notifier.notify("tool", {"summary": f"tool-{i}"}, "ws")
        notifier._drain_once()
        self.assertEqual(len(client.sent), 1)
        self.assertIn("digest (5 events)", client.sent[0][1])
        for i in range(5):
            self.assertIn(f"tool-{i}", client.sent[0][1])

    def test_different_workspaces_not_coalesced(self):
        notifier, client, _s = make_notifier()
        notifier.notify("tool", {"summary": "a"}, "ws-a")
        notifier.notify("tool", {"summary": "b"}, "ws-b")
        notifier._drain_once()
        self.assertEqual(len(client.sent), 2)


class TopicMappingTests(unittest.TestCase):
    def test_topic_created_and_persisted_on_first_send(self):
        notifier, client, store = make_notifier()
        notifier.notify("turn-end", {"text": "done"}, "my-project")
        notifier._drain_once()
        self.assertEqual(client.created, ["my-project"])
        self.assertEqual(client.sent[0][2], 101)
        self.assertEqual(store.data["my-project"]["thread_id"], 101)
        self.assertFalse(store.data["my-project"]["fallback"])

    def test_persisted_topic_reused_across_instances(self):
        store = MappingStore()
        store.data["my-project"] = {"chat_id": "-100123", "thread_id": 42, "fallback": False}
        notifier, client, _ = make_notifier(store=store)
        notifier.notify("turn-end", {"text": "again"}, "my-project")
        notifier._drain_once()
        self.assertEqual(client.created, [])  # no second topic
        self.assertEqual(client.sent[0][2], 42)

    def test_deleted_topic_recreated(self):
        store = MappingStore()
        store.data["ws"] = {"chat_id": "-100123", "thread_id": 42, "fallback": False}
        notifier, client, _ = make_notifier(store=store)
        client.send_raises_once = tn.BadThreadId("thread not found")
        notifier.notify("turn-end", {"text": "x"}, "ws")
        notifier._drain_once()
        self.assertEqual(client.created, ["ws"])
        self.assertEqual(client.sent[0][2], 101)
        self.assertEqual(store.data["ws"]["thread_id"], 101)

    def test_chat_without_topics_falls_back_with_prefix(self):
        notifier, client, store = make_notifier()
        client.create_raises = tn.TopicsUnsupported("chat is not a forum")
        notifier.notify("turn-end", {"text": "hello"}, "my-ws")
        notifier._drain_once()
        self.assertEqual(len(client.sent), 1)
        self.assertIsNone(client.sent[0][2])
        self.assertTrue(client.sent[0][1].startswith("[my-ws] "))
        self.assertTrue(store.data["my-ws"]["fallback"])
        # Fallback persisted: next send does not retry createForumTopic.
        client.create_raises = RuntimeError("must not be called again")
        notifier.notify("turn-end", {"text": "second"}, "my-ws")
        notifier._drain_once()
        self.assertEqual(len(client.sent), 2)

    def test_concurrent_workspaces_use_distinct_topics(self):
        notifier, client, _ = make_notifier()
        notifier.notify("turn-end", {"text": "a"}, "ws-a")
        notifier.notify("turn-end", {"text": "b"}, "ws-b")
        notifier._drain_once()
        threads = {ws: tid for (_c, _t, tid), ws in zip(client.sent, ["ws-a", "ws-b"])}
        self.assertEqual(len(set(threads.values())), 2)


class RetryTests(unittest.TestCase):
    def test_429_requeues_terminal_event(self):
        notifier, client, _ = make_notifier()
        client.send_raises_once = tn.RetryAfter(0.01)
        notifier.notify("task-terminal", {"task_id": "abc", "state": "succeeded",
                                          "checkpoint_status": "confirmed",
                                          "duration_s": 1}, "ws")
        notifier._drain_once()
        # Re-queued and sent on the retry within the same drain.
        self.assertEqual(len(client.sent), 1)


class SendLoggingTests(unittest.TestCase):
    """add-task-liveness-safety (task 4.3, design D5): the success half of the
    send outcome must be readable from the logs alone."""

    def test_successful_send_is_logged_with_routing_details(self):
        notifier, client, _ = make_notifier()
        with self.assertLogs("sch_telegram", level="INFO") as captured:
            notifier.notify("turn-end", {"text": "done"}, "my-ws")
            notifier._drain_once()
        self.assertEqual(len(client.sent), 1)
        lines = [line for line in captured.output if "telegram sent:" in line]
        self.assertEqual(len(lines), 1, captured.output)
        self.assertIn("workspace=my-ws", lines[0])
        self.assertIn("type=turn-end", lines[0])
        self.assertIn("chat=-100123", lines[0])
        self.assertIn("thread=101", lines[0])

    def test_digest_send_logs_the_coalesced_count(self):
        notifier, client, _ = make_notifier()
        with self.assertLogs("sch_telegram", level="INFO") as captured:
            for i in range(3):
                notifier.notify("tool", {"summary": f"t{i}"}, "ws")
            notifier._drain_once()
        self.assertEqual(len(client.sent), 1)
        lines = [line for line in captured.output if "telegram sent:" in line]
        self.assertEqual(len(lines), 1, captured.output)
        self.assertIn("type=digest", lines[0])
        self.assertIn("digest_count=3", lines[0])

    def test_send_log_never_carries_the_token(self):
        # Whole-line sanitization, exactly like every other logged string: a
        # workspace name that happens to contain the token comes out masked.
        notifier, _client, _ = make_notifier()
        with self.assertLogs("sch_telegram", level="INFO") as captured:
            notifier.notify("turn-end", {"text": "x"}, "ws-test-token-here")
            notifier._drain_once()
        line = [l for l in captured.output if "telegram sent:" in l][0]
        self.assertNotIn("test-token", line)
        self.assertIn("workspace=ws-***-here", line)

    def test_failed_send_logs_no_success_line(self):
        notifier, client, _ = make_notifier()
        client.send_raises_once = RuntimeError("boom")
        with self.assertLogs("sch_telegram", level="INFO") as captured:
            notifier.notify("turn-end", {"text": "x"}, "ws")
            notifier._drain_once()
        self.assertEqual(client.sent, [])
        self.assertFalse([line for line in captured.output if "telegram sent:" in line])
        self.assertTrue([line for line in captured.output if "send failed" in line])


class SpoolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.spool = Path(self.tmp.name)

    def test_spool_event_consumed_and_removed(self):
        notifier, client, _ = make_notifier(spool_dir=str(self.spool))
        (self.spool / "1.json").write_text(json.dumps(
            {"type": "turn-end", "payload": {"text": "turn done"}}
        ))
        notifier._poll_spool()
        notifier._drain_once()
        self.assertEqual(len(client.sent), 1)
        self.assertIn("turn done", client.sent[0][1])
        self.assertEqual(list(self.spool.iterdir()), [])
        # Spool events without a workspace route to the default workspace.
        self.assertEqual(client.created, ["default-ws"])

    def test_malformed_spool_event_discarded(self):
        notifier, client, _ = make_notifier(spool_dir=str(self.spool))
        (self.spool / "bad.json").write_text("{ not json")
        (self.spool / "notdict.json").write_text('"just a string"')
        notifier._poll_spool()
        notifier._drain_once()
        self.assertEqual(client.sent, [])
        self.assertEqual(list(self.spool.iterdir()), [])

    def test_milestone_updates_last_activity(self):
        notifier, _c, _s = make_notifier(spool_dir=str(self.spool))
        self.assertIsNone(notifier.last_activity_age("default-ws"))
        (self.spool / "1.json").write_text(json.dumps(
            {"type": "todo", "payload": {"summary": "x"}}
        ))
        notifier._poll_spool()
        age = notifier.last_activity_age("default-ws")
        self.assertIsNotNone(age)
        self.assertLess(age, 5)

    def test_tool_milestone_rearms_the_stall_signal(self):
        # add-task-liveness-safety, spec 'rearmed stall-detector milestone
        # signal': a claude workspace now emits in-turn `tool`
        # events, so last_activity_age() stops returning None and the second
        # signal of _check_task_stall becomes usable for claude too.
        notifier, _c, _s = make_notifier(spool_dir=str(self.spool))
        self.assertIsNone(notifier.last_activity_age("default-ws"))
        (self.spool / "1.json").write_text(json.dumps(
            {"type": "tool", "payload": {"summary": "Bash: make test"}}
        ))
        notifier._poll_spool()
        self.assertIsNotNone(notifier.last_activity_age("default-ws"))


class PresenceGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.spool = Path(self.tmp.name)

    def write(self, name, event):
        (self.spool / name).write_text(json.dumps(event), encoding="utf-8")

    def test_attached_interactive_is_discarded_without_replay(self):
        seen = []
        notifier, client, _ = make_notifier(
            spool_dir=str(self.spool),
            presence_at_emission=lambda ts: seen.append(ts) or True,
        )
        self.write("1.json", {
            "type": "turn-end", "execution_mode": "interactive", "ts": 100.0,
            "payload": {"text": "hidden"},
        })
        notifier._poll_spool()
        notifier._drain_once()
        self.assertEqual(seen, [100.0])
        self.assertEqual(client.sent, [])
        self.assertEqual(list(self.spool.iterdir()), [])
        notifier._presence_at_emission = lambda _ts: False
        notifier._drain_once()
        self.assertEqual(client.sent, [])

    def test_detached_event_remains_queued_after_reconnect(self):
        current = {"attached": False}
        history = {100.0: False, 200.0: True}
        notifier, client, _ = make_notifier(
            spool_dir=str(self.spool),
            presence_at_emission=lambda ts: history[ts],
            current_presence=lambda: current["attached"],
        )
        self.write("1.json", {
            "type": "turn-end", "execution_mode": "interactive", "ts": 100.0,
            "payload": {"text": "deliver"},
        })
        notifier._poll_spool()
        current["attached"] = True
        self.write("2.json", {
            "type": "turn-end", "execution_mode": "interactive", "ts": 200.0,
            "payload": {"text": "suppress after reconnect"},
        })
        notifier._poll_spool()
        notifier._drain_once()
        self.assertEqual(len(client.sent), 1)
        self.assertIn("deliver", client.sent[0][1])

    def test_headless_lifecycle_and_inbound_bypass_presence(self):
        notifier, client, _ = make_notifier(
            spool_dir=str(self.spool), presence_at_emission=lambda _ts: True,
        )
        events = [
            {"type": "turn-end", "execution_mode": "headless", "ts": 1,
             "payload": {"text": "headless"}},
            {"type": "task-terminal", "execution_mode": "interactive", "ts": 2,
             "payload": {"task_id": "t", "state": "succeeded"}},
            {"type": "interaction", "execution_mode": "interactive", "ts": 3,
             "payload": {"text": "reply"}},
        ]
        for index, event in enumerate(events):
            self.write(f"{index}.json", event)
        notifier._poll_spool()
        notifier._drain_once()
        self.assertEqual(len(client.sent), 3)

    def test_presence_errors_fail_open_and_coalescing_sees_only_eligible_events(self):
        def attached(ts):
            if ts == 3:
                raise ValueError("invalid snapshot")
            return ts == 1

        notifier, client, _ = make_notifier(
            spool_dir=str(self.spool), presence_at_emission=attached,
        )
        for ts in (1, 2, 3):
            self.write(f"{ts}.json", {
                "type": "tool", "execution_mode": "interactive", "ts": ts,
                "workspace": "ws", "payload": {"summary": f"tool-{ts}"},
            })
        with self.assertLogs("sch_telegram", level="WARNING"):
            notifier._poll_spool()
        notifier._drain_once()
        self.assertEqual(len(client.sent), 1)
        self.assertIn("digest (2 events)", client.sent[0][1])
        self.assertNotIn("tool-1", client.sent[0][1])

    def test_permission_race_deposits_fallback_and_suppresses_publication(self):
        class Interaction:
            def __init__(self):
                self.events = []

            def fallback_permission_request(self, event):
                self.events.append(event)
                return True

        interaction = Interaction()
        notifier, client, _ = make_notifier(
            spool_dir=str(self.spool),
            presence_at_emission=lambda _ts: False,
            current_presence=lambda: True,
        )
        notifier.set_interaction(interaction)
        self.write("1.json", {
            "type": "permission-request", "execution_mode": "interactive", "ts": 1,
            "payload": {"request_id": "r1", "tool": "Bash"},
        })
        notifier._poll_spool()
        notifier._drain_once()
        self.assertEqual(len(interaction.events), 1)
        self.assertEqual(client.sent, [])

    def test_permission_race_is_rechecked_immediately_before_send(self):
        current = {"attached": False}

        class Interaction:
            def __init__(self):
                self.events = []

            def fallback_permission_request(self, event):
                self.events.append(event)
                return True

            def reply_markup_for(self, _event):
                return None

        interaction = Interaction()
        notifier, client, _ = make_notifier(
            spool_dir=str(self.spool),
            presence_at_emission=lambda _ts: False,
            current_presence=lambda: current["attached"],
        )
        notifier.set_interaction(interaction)
        self.write("1.json", {
            "type": "permission-request", "execution_mode": "interactive", "ts": 1,
            "payload": {"request_id": "r1", "tool": "Bash"},
        })
        notifier._poll_spool()
        current["attached"] = True
        notifier._drain_once()
        self.assertEqual(len(interaction.events), 1)
        self.assertEqual(client.sent, [])

    def test_observational_permission_uses_emission_state_only(self):
        current = {"attached": False}
        notifier, client, _ = make_notifier(
            spool_dir=str(self.spool),
            presence_at_emission=lambda _ts: False,
            current_presence=lambda: current["attached"],
        )
        self.write("1.json", {
            "type": "permission-request", "execution_mode": "interactive", "ts": 1,
            "payload": {"tool": "Bash"},
        })
        notifier._poll_spool()
        current["attached"] = True
        notifier._drain_once()
        self.assertEqual(len(client.sent), 1)

    def test_reconnect_fallback_never_publishes_after_later_detach(self):
        class Interaction:
            def fallback_permission_request(self, _event):
                return True

            def permission_request_resolved(self, _event):
                return True

            def reply_markup_for(self, _event):
                return {"inline_keyboard": []}

        notifier, client, _ = make_notifier(
            spool_dir=str(self.spool),
            presence_at_emission=lambda _ts: False,
            current_presence=lambda: False,
        )
        notifier.set_interaction(Interaction())
        self.write("1.json", {
            "type": "permission-request", "execution_mode": "interactive", "ts": 1,
            "payload": {"request_id": "r1", "tool": "Bash"},
        })
        notifier._poll_spool()
        notifier._drain_once()
        self.assertEqual(client.sent, [])


class FlushTests(unittest.TestCase):
    def test_flush_sends_pending_terminals_only(self):
        notifier, client, _ = make_notifier()
        notifier.notify("tool", {"summary": "noise"}, "ws")
        notifier.notify("task-terminal", {"task_id": "abc", "state": "failed",
                                          "checkpoint_status": "failed",
                                          "duration_s": 9, "exit_code": 1}, "ws")
        notifier.flush(timeout=2.0)
        self.assertEqual(len(client.sent), 1)
        self.assertIn("FAILED", client.sent[0][1])


class OnSentHookTests(unittest.TestCase):
    """TASK-28: the success hook is the shim's only signal that a terminal
    notification really left the microVM (it marks the S3 record delivered so
    the watchdog does not re-send it). It must fire exactly on success."""

    TERMINAL = {"task_id": "abc", "state": "succeeded",
                "checkpoint_status": "confirmed", "duration_s": 3}

    def test_hook_receives_the_event_after_a_successful_send(self):
        seen = []
        notifier, client, _ = make_notifier(on_sent=seen.append)
        notifier.notify("task-terminal", self.TERMINAL, "ws")
        notifier._drain_once()
        self.assertEqual(len(client.sent), 1)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["type"], "task-terminal")
        self.assertEqual(seen[0]["workspace"], "ws")
        self.assertEqual(seen[0]["payload"]["task_id"], "abc")

    def test_hook_not_called_on_a_failed_send(self):
        seen = []
        notifier, client, _ = make_notifier(on_sent=seen.append)
        client.send_raises_once = RuntimeError("boom")
        notifier.notify("task-terminal", self.TERMINAL, "ws")
        with self.assertLogs("sch_telegram", level="WARNING"):
            notifier._drain_once()
        self.assertEqual(client.sent, [])
        self.assertEqual(seen, [])

    def test_hook_called_once_after_a_429_retry(self):
        seen = []
        notifier, client, _ = make_notifier(on_sent=seen.append)
        client.send_raises_once = tn.RetryAfter(0.01)
        notifier.notify("task-terminal", self.TERMINAL, "ws")
        notifier._drain_once()
        self.assertEqual(len(client.sent), 1)
        self.assertEqual(len(seen), 1)

    def test_hook_runs_on_the_shutdown_flush_path_too(self):
        seen = []
        notifier, client, _ = make_notifier(on_sent=seen.append)
        notifier.notify("task-terminal", self.TERMINAL, "ws")
        notifier.flush(timeout=2.0)
        self.assertEqual(len(client.sent), 1)
        self.assertEqual([e["type"] for e in seen], ["task-terminal"])

    def test_hook_failure_is_logged_and_contained(self):
        def explode(_event):
            raise RuntimeError("s3 down test-token")

        notifier, client, _ = make_notifier(on_sent=explode)
        notifier.notify("task-terminal", self.TERMINAL, "ws")
        with self.assertLogs("sch_telegram", level="INFO") as captured:
            notifier._drain_once()
        self.assertEqual(len(client.sent), 1)
        # The send itself is still reported as delivered, the hook failure is
        # a warning next to it, and the token is masked like everywhere else.
        self.assertTrue([l for l in captured.output if "telegram sent:" in l])
        hook_lines = [l for l in captured.output if "on_sent hook failed" in l]
        self.assertEqual(len(hook_lines), 1, captured.output)
        self.assertNotIn("test-token", hook_lines[0])
        # And the loop keeps working afterwards.
        notifier.notify("turn-end", {"text": "next"}, "ws")
        notifier._drain_once()
        self.assertEqual(len(client.sent), 2)

    def test_hook_sees_the_coalesced_digest_not_the_members(self):
        seen = []
        notifier, client, _ = make_notifier(on_sent=seen.append)
        for i in range(3):
            notifier.notify("tool", {"summary": f"t{i}"}, "ws")
        notifier._drain_once()
        self.assertEqual(len(client.sent), 1)
        self.assertEqual([e["type"] for e in seen], ["digest"])


if __name__ == "__main__":
    unittest.main()
