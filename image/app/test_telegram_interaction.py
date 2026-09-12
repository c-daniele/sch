"""Container-free tests for the Telegram inbound interaction
(add-telegram-interaction, task 5.1): exactly-once consumption (conditional
Delete), TTL expiry -> discard+notify, D5 dispatch in the four workspace
states, broker (single decision, fail-safe timeout, late decision),
approval-message sync (outcome edit, dual-control)."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

import telegram_interaction as ti


class FakeConditionalError(Exception):
    def __init__(self, code="ConditionalCheckFailedException"):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeDdb:
    """Low-level DynamoDB client double with real conditional-delete
    semantics: delete succeeds once per key, then raises."""

    def __init__(self, items=None):
        self.items = list(items or [])
        self.deleted = []

    def query(self, **kwargs):
        ws = kwargs["ExpressionAttributeValues"][":ws"]["S"]
        return {"Items": [i for i in self.items if i["workspace"]["S"] == ws]}

    def delete_item(self, TableName, Key, ConditionExpression):  # noqa: N803
        key = (Key["workspace"]["S"], Key["sk"]["S"])
        for idx, item in enumerate(self.items):
            if (item["workspace"]["S"], item["sk"]["S"]) == key:
                self.items.pop(idx)
                self.deleted.append(key)
                return {}
        raise FakeConditionalError()


def _cmd_item(ws="ws1", sk="0001#1", ctype="text", expires_in=600, **extra):
    item = {
        "workspace": {"S": ws},
        "sk": {"S": sk},
        "type": {"S": ctype},
        "expiresAt": {"N": str(int(time.time()) + expires_in)},
    }
    for key, value in extra.items():
        item[key] = {"N": str(value)} if isinstance(value, int) else {"S": value}
    return item


class CommandQueueTests(unittest.TestCase):
    def test_commands_consumed_exactly_once(self):
        ddb = FakeDdb([_cmd_item(sk="0001#1", text="a"), _cmd_item(sk="0002#2", text="b")])
        queue = ti.CommandQueue(ddb, "commands", lambda: "ws1")
        first = queue.poll()
        self.assertEqual([c["text"] for c in first], ["a", "b"])
        self.assertEqual(queue.poll(), [])  # nothing left to consume

    def test_lost_delete_race_skips_the_command(self):
        ddb = FakeDdb([_cmd_item(sk="0001#1", text="a")])
        stale = FakeDdb([])  # queries see the item, deletes always lose
        stale.query = ddb.query
        queue = ti.CommandQueue(stale, "commands", lambda: "ws1")
        self.assertEqual(queue.poll(), [])

    def test_expired_command_is_flagged(self):
        ddb = FakeDdb([
            _cmd_item(sk="0001#1", text="stale", expires_in=-5),
            _cmd_item(sk="0002#2", text="fresco", expires_in=600),
        ])
        queue = ti.CommandQueue(ddb, "commands", lambda: "ws1")
        commands = queue.poll()
        self.assertEqual([c["expired"] for c in commands], [True, False])

    def test_no_workspace_no_poll(self):
        ddb = FakeDdb([_cmd_item()])
        queue = ti.CommandQueue(ddb, "commands", lambda: None)
        self.assertEqual(queue.poll(), [])
        self.assertEqual(len(ddb.items), 1)


class ApprovalBrokerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.broker = ti.ApprovalBroker(Path(self.tmp.name))

    def test_single_decision_per_request(self):
        rid = self.broker.create_request("Bash", "rm -rf /tmp/x")
        self.assertTrue(self.broker.has_pending_requests())
        self.assertTrue(self.broker.deposit_decision(rid, "approve"))
        # Duplicate/late decisions are rejected without altering the first.
        self.assertFalse(self.broker.deposit_decision(rid, "deny"))
        self.assertEqual(self.broker.get_decision(rid)["outcome"], "approve")
        self.assertFalse(self.broker.has_pending_requests())

    def test_invalid_outcome_rejected(self):
        rid = self.broker.create_request("Bash")
        self.assertFalse(self.broker.deposit_decision(rid, "yolo"))
        self.assertIsNone(self.broker.get_decision(rid))

    def test_wait_timeout_is_fail_safe_and_claims_the_slot(self):
        rid = self.broker.create_request("Bash")
        decision = self.broker.wait_decision(rid, timeout_s=0, poll_s=0.01)
        self.assertEqual(decision["outcome"], "timeout")
        # A late remote decision now finds the slot taken (design D4).
        self.assertFalse(self.broker.deposit_decision(rid, "approve"))

    def test_wait_returns_deposited_decision(self):
        rid = self.broker.create_request("Bash")
        self.broker.deposit_decision(rid, "deny")
        decision = self.broker.wait_decision(rid, timeout_s=5, poll_s=0.01)
        self.assertEqual(decision["outcome"], "deny")

    def test_native_resolution_marker(self):
        rid = self.broker.create_request("Bash")
        self.broker.mark_native_resolution(rid, "allow")
        self.assertEqual(self.broker.get_native(rid)["outcome"], "allow")

    def test_resolve_pending_as_fallback_is_atomic_and_skips_pi(self):
        opencode = self.broker.create_request("Bash", source="opencode")
        claude = self.broker.create_request("Write", source="claude")
        pi = self.broker.create_request("tool", source="pi")
        resolved = self.broker.resolve_pending_as_fallback()
        self.assertEqual(set(resolved), {opencode, claude})
        self.assertEqual(self.broker.get_decision(opencode)["outcome"], "fallback")
        self.assertEqual(self.broker.get_decision(claude)["outcome"], "fallback")
        self.assertIsNone(self.broker.get_decision(pi))
        self.assertEqual(self.broker.resolve_pending_as_fallback(), [])

    def test_resolve_pending_as_fallback_skips_headless_records(self):
        rid = self.broker.create_request(source="opencode", tool="Bash")
        request = self.broker.get_request(rid)
        request["execution_mode"] = "headless"
        (self.broker.requests_dir / f"{rid}.json").write_text(json.dumps(request))
        self.assertEqual(self.broker.resolve_pending_as_fallback(), [])
        self.assertIsNone(self.broker.get_decision(rid))


class FakeTelegramClient:
    def __init__(self):
        self.edits = []
        self.answers = []

    def edit_message_text(self, chat_id, message_id, text):
        self.edits.append((message_id, text))
        return {}

    def answer_callback_query(self, callback_query_id, text=""):
        self.answers.append((callback_query_id, text))
        return {}


def _manager(broker, ddb=None, gate=None, dispatch=None, notify=None):
    events = []
    queue = ti.CommandQueue(ddb or FakeDdb([]), "commands", lambda: "ws1")
    manager = ti.InteractionManager(
        queue=queue,
        broker=broker,
        client=FakeTelegramClient(),
        chat_id="-100",
        notify=notify or (lambda etype, payload, ws=None: events.append((etype, payload))),
        gate=gate or (lambda: True),
        dispatch_text=dispatch or (lambda text: None),
        poll_interval=0.0,
    )
    return manager, events


class InteractionManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.broker = ti.ApprovalBroker(Path(self.tmp.name))

    def test_gate_closed_no_poll(self):
        ddb = FakeDdb([_cmd_item(text="hello")])
        manager, _events = _manager(self.broker, ddb=ddb, gate=lambda: False)
        manager.tick()
        self.assertEqual(len(ddb.items), 1)  # untouched

    def test_expired_command_discarded_with_notification(self):
        ddb = FakeDdb([_cmd_item(text="tardi", expires_in=-1)])
        dispatched = []
        manager, events = _manager(self.broker, ddb=ddb, dispatch=dispatched.append)
        manager.tick()
        self.assertEqual(dispatched, [])  # never applied
        self.assertEqual([e[0] for e in events], ["command-expired"])
        self.assertIn("tardi", events[0][1]["preview"])

    def test_text_command_dispatched(self):
        ddb = FakeDdb([_cmd_item(text="fai una cosa")])
        dispatched = []
        manager, _events = _manager(self.broker, ddb=ddb, dispatch=dispatched.append)
        manager.tick()
        self.assertEqual(dispatched, ["fai una cosa"])

    def test_decision_command_reaches_the_broker(self):
        rid = self.broker.create_request("Bash")
        ddb = FakeDdb([_cmd_item(
            ctype="decision", requestId=rid, decision="approve",
            callbackQueryId="cb1", messageId=7,
        )])
        manager, _events = _manager(self.broker, ddb=ddb)
        manager.tick()
        self.assertEqual(self.broker.get_decision(rid)["outcome"], "approve")
        self.assertEqual(manager._client.answers, [("cb1", "✅ Approved")])

    def test_late_decision_is_ignored_with_feedback(self):
        rid = self.broker.create_request("Bash")
        self.broker.deposit_decision(rid, "timeout", source="hook-timeout")
        ddb = FakeDdb([_cmd_item(
            ctype="decision", requestId=rid, decision="approve", callbackQueryId="cb2",
        )])
        manager, events = _manager(self.broker, ddb=ddb)
        manager.tick()
        self.assertEqual(self.broker.get_decision(rid)["outcome"], "timeout")
        self.assertIn("Already resolved", manager._client.answers[0][1])
        self.assertEqual([e[0] for e in events], ["interaction"])

    def test_unknown_request_decision_is_ignored(self):
        ddb = FakeDdb([_cmd_item(ctype="decision", requestId="ghost", decision="deny")])
        manager, events = _manager(self.broker, ddb=ddb)
        manager.tick()
        self.assertIsNone(self.broker.get_decision("ghost"))
        self.assertEqual([e[0] for e in events], ["interaction"])

    def _publish(self, manager, rid):
        event = {"type": "permission-request", "payload": {"request_id": rid}}
        markup = manager.reply_markup_for(event)
        self.assertIn(f"{rid}:approve", json.dumps(markup))
        manager.on_permission_published(event, {"message_id": 42}, "ws1", "request X")

    def test_outcome_edits_the_published_message(self):
        rid = self.broker.create_request("Bash")
        manager, _events = _manager(self.broker)
        self._publish(manager, rid)
        self.broker.deposit_decision(rid, "deny")
        manager.tick()
        self.assertEqual(len(manager._client.edits), 1)
        message_id, text = manager._client.edits[0]
        self.assertEqual(message_id, 42)
        self.assertIn("denied", text)

    def test_dual_control_native_resolution_updates_the_message(self):
        rid = self.broker.create_request("Bash")
        manager, _events = _manager(self.broker)
        self._publish(manager, rid)
        # Remote wait timed out, then the operator resolved from the TUI.
        self.broker.deposit_decision(rid, "timeout", source="hook-timeout")
        manager.tick()
        self.assertIn("timeout", manager._client.edits[-1][1])
        self.broker.mark_native_resolution(rid, "allow")
        manager.tick()
        self.assertIn("resolved by the TUI", manager._client.edits[-1][1])

    def test_reconnect_fallback_updates_message_and_rejects_late_decision(self):
        rid = self.broker.create_request("Bash", source="claude")
        manager, _events = _manager(self.broker)
        self._publish(manager, rid)
        self.assertEqual(manager.resolve_pending_remote_approvals(), [rid])
        manager.tick()
        self.assertIn("resolved elsewhere", manager._client.edits[-1][1])
        self.assertNotIn(rid, manager._tracked)
        self.assertFalse(self.broker.deposit_decision(rid, "approve"))

    def test_permission_fallback_claims_exactly_one_decision(self):
        rid = self.broker.create_request("Bash", source="opencode")
        manager, _events = _manager(self.broker)
        event = {"type": "permission-request", "payload": {"request_id": rid}}
        self.assertTrue(manager.fallback_permission_request(event))
        self.assertEqual(self.broker.get_decision(rid)["outcome"], "fallback")
        self.assertTrue(manager.fallback_permission_request(event))
        self.assertFalse(self.broker.deposit_decision(rid, "deny"))

    def test_poll_errors_never_raise(self):
        class BrokenDdb:
            def query(self, **_kwargs):
                raise RuntimeError("dynamo down")

        manager, _events = _manager(self.broker, ddb=BrokenDdb())
        manager.tick()  # must not raise (design D7)


class DispatchD5Tests(unittest.TestCase):
    """The four D5 states, exercised against main.py's real dispatcher with
    fake collaborators (same loader pattern as test_telegram_lifecycle)."""

    @classmethod
    def setUpClass(cls):
        from test_telegram_lifecycle import load_main
        cls.main = load_main()

    def setUp(self):
        self.main._TELEGRAM["broker"] = None
        self.main._INTERACTIVE_ACTIVE = False
        self.notified = []
        self.main._telegram_notify = lambda etype, payload, ws=None: self.notified.append(
            (etype, payload)
        )

    def tearDown(self):
        self.main._TELEGRAM["broker"] = None

    def test_pending_approval_gets_courtesy_not_interpretation(self):
        class Broker:
            def has_pending_requests(self):
                return True

        self.main._TELEGRAM["broker"] = Broker()
        self.main._dispatch_telegram_text("approvo")  # text never decides
        self.assertEqual(self.notified[0][0], "interaction")
        self.assertIn("buttons", self.notified[0][1]["text"])

    def test_opencode_active_injects(self):
        self.main._resolve_harness = lambda: "opencode"
        self.main._serve_is_alive = lambda: True
        injected = []
        self.main._opencode_inject_text = lambda text: (
            injected.append(text) or {"ok": True, "session_id": "ses_123", "title": "T"}
        )
        self.main._dispatch_telegram_text("answer to the question")
        self.assertEqual(injected, ["answer to the question"])
        self.assertEqual(self.notified, [])  # turn-end milestone is the only reply

    def test_opencode_injection_failure_is_actionable(self):
        self.main._resolve_harness = lambda: "opencode"
        self.main._serve_is_alive = lambda: True
        self.main._opencode_inject_text = lambda text: {"ok": False, "error": "HTTP 500"}
        self.main._dispatch_telegram_text("testo")
        text = self.notified[0][1]["text"]
        self.assertIn("Injection failed", text)
        self.assertIn("follow-up", text)

    def test_claude_interactive_gets_the_limit_message(self):
        self.main._resolve_harness = lambda: "claude"
        self.main._INTERACTIVE_ACTIVE = True
        self.main._dispatch_telegram_text("reply like this")
        text = self.notified[0][1]["text"]
        self.assertIn("cannot", text)
        self.assertIn("sch run", text)

    def test_quiescent_submits_followup_task(self):
        self.main._resolve_harness = lambda: "claude"
        self.main._serve_is_alive = lambda: False
        submitted = []
        self.main._handle_task_action = lambda payload: (
            submitted.append(payload) or {"status": "accepted", "task_id": "t" * 32}
        )
        self.main._dispatch_telegram_text("also fix the tests")
        self.assertEqual(submitted[0]["prompt"], "also fix the tests")
        self.assertTrue(submitted[0]["continue"])
        self.assertEqual(self.notified, [])  # lifecycle notification suffices

    def test_followup_with_running_task_is_refused_with_reference(self):
        self.main._resolve_harness = lambda: "opencode"
        self.main._serve_is_alive = lambda: False
        self.main._handle_task_action = lambda payload: {
            "status": "busy", "task_id": "abcdef1234567890",
        }
        self.main._dispatch_telegram_text("altro lavoro")
        text = self.notified[0][1]["text"]
        self.assertIn("abcdef12", text)
        self.assertIn("was not started", text)


class InteractionGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from test_telegram_lifecycle import load_main
        cls.main = load_main()

    def setUp(self):
        self.main._INTERACTIVE_ACTIVE = False
        self.main._SERVE_STATE["proc"] = None
        self.main._TASK_STATE["state"] = "none"
        self.main._TELEGRAM["broker"] = None
        self.main._WORKSPACE_READY.clear()

    def tearDown(self):
        self.main._WORKSPACE_READY.clear()

    def test_warm_quiescent_runtime_polls_for_followups(self):
        self.main._WORKSPACE_READY.set()
        self.assertTrue(self.main._telegram_interaction_gate())

    def test_cold_runtime_does_not_poll(self):
        self.assertFalse(self.main._telegram_interaction_gate())


class PiDispatchTests(unittest.TestCase):
    """add-pi-harness task 7.3/7.4: per-state dispatch with harness=pi
    (spec: telegram-interaction, "Testo libero su sessione pi attiva")."""

    @classmethod
    def setUpClass(cls):
        from test_telegram_lifecycle import load_main
        cls.main = load_main()

    def setUp(self):
        self.main._TELEGRAM["broker"] = None
        self.main._INTERACTIVE_ACTIVE = False
        self.main._resolve_harness = lambda: "pi"
        self.notified = []
        self.main._telegram_notify = lambda etype, payload, ws=None: self.notified.append(
            (etype, payload)
        )

    def tearDown(self):
        self.main._TELEGRAM["broker"] = None
        self.main._INTERACTIVE_ACTIVE = False

    def test_interactive_pi_gets_the_limit_message_naming_the_harness(self):
        self.main._INTERACTIVE_ACTIVE = True
        self.main._dispatch_telegram_text("reply like this")
        etype, payload = self.notified[0]
        self.assertEqual(etype, "interaction")
        text = payload["text"]
        self.assertIn("pi", text)
        self.assertIn("cannot", text)
        self.assertIn("sch run", text)

    def test_pi_never_takes_the_opencode_injection_path(self):
        """Even if a backend somehow looks alive, pi has no injectable session:
        the dispatcher must not call the opencode injector."""
        self.main._INTERACTIVE_ACTIVE = True
        self.main._serve_is_alive = lambda: True
        calls = []
        self.main._opencode_inject_text = lambda text: calls.append(text) or {"ok": True}
        self.main._dispatch_telegram_text("testo")
        self.assertEqual(calls, [])
        self.assertIn("cannot", self.notified[0][1]["text"])

    def test_quiescent_pi_submits_a_followup_task(self):
        self.main._INTERACTIVE_ACTIVE = False
        self.main._serve_is_alive = lambda: False
        submitted = []
        self.main._handle_task_action = lambda payload: (
            submitted.append(payload) or {"status": "accepted", "task_id": "p" * 32}
        )
        self.main._dispatch_telegram_text("prosegui il lavoro")
        self.assertEqual(submitted[0]["prompt"], "prosegui il lavoro")
        self.assertTrue(submitted[0]["continue"])
        self.assertEqual(self.notified, [])

class TimeoutSemanticsTests(unittest.TestCase):
    def test_timeout_keeps_being_tracked_for_dual_control(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            broker = ti.ApprovalBroker(Path(tmp))
            rid = broker.create_request("Bash", "echo hi", 1, source="claude")
            broker.deposit_decision(rid, "timeout", source="hook-timeout")
            manager, _events = _manager(broker)
            manager.on_permission_published(
                {"type": "permission-request", "source": "claude",
                 "payload": {"request_id": rid}},
                {"message_id": 7}, "ws", "base",
            )
            manager.tick()
            self.assertIn(rid, manager._tracked)


if __name__ == "__main__":
    unittest.main()
