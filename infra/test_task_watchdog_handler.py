"""Isolated task watchdog handler tests — no S3, no network
(add-task-liveness-safety, task 3.2, pattern of test_telegram_webhook_handler).

The five scenarios the change requires are marked below; the rest are
guardrails against the ways this Lambda could become destructive (it is the
only component of SCH allowed to rewrite a task status it does not own).
TASK-28 adds the at-least-once terminal notification path (``pending``
terminal records re-sent and marked ``delivered``).
"""

import datetime
import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path

# Assigned, not setdefault-ed: unlike the webhook handler's TELEGRAM_* names,
# these SCH_* variables really exist inside a session microVM (the shim's own
# environment), and inheriting them would point the tests at the live bucket
# and the real bot token.
os.environ["SCH_CHECKPOINT_BUCKET"] = "checkpoints-tests"
os.environ["SCH_TELEGRAM_BOT_TOKEN"] = "token-tests"
os.environ["SCH_TELEGRAM_CHAT_ID"] = "-10042"
os.environ.pop("SCH_WATCHDOG_STALE_S", None)
os.environ.pop("SCH_WATCHDOG_NOTIFY_AFTER_S", None)

_MODULE = Path(__file__).with_name("task_watchdog_handler.py")
_SPEC = importlib.util.spec_from_file_location("task_watchdog_handler", _MODULE)
watchdog = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = watchdog
_SPEC.loader.exec_module(watchdog)

BUCKET = "checkpoints-tests"
TOKEN = "wt-aaaa"


def _summary(**counts):
    """The handler's sweep summary with every counter at zero unless given."""
    base = {"scanned": 0, "stale": 0, "reconciled": 0, "notified": 0, "pending": 0, "resent": 0}
    base.update(counts)
    return base


def _stamp(seconds_ago):
    moment = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        seconds=seconds_ago
    )
    return moment.strftime(watchdog.HEARTBEAT_FORMAT)


def _running(heartbeat_ago=30, epoch=2, token=TOKEN, **extra):
    status = {
        "task_id": "1234567890abcdef",
        "state": "running",
        "prompt": "fai X",
        "harness": "claude",
        "started_utc": _stamp(3600),
        "heartbeat_utc": _stamp(heartbeat_ago),
        "image_version": "30",
        "writer_token": token,
        "session_epoch": epoch,
        "session_id": "sch-ws-1",
    }
    status.update(extra)
    return status


def _terminal(finished_ago=600, state="succeeded", notification="pending", **extra):
    """A terminal record as the shim writes it (TASK-28): the notification
    promise is present iff ``notification`` is not None."""
    status = _running(heartbeat_ago=finished_ago)
    status.update({
        "state": state,
        "finished_utc": _stamp(finished_ago),
        "duration_s": 161,
        "exit_code": 0 if state == "succeeded" else 1,
        "error": None if state == "succeeded" else "harness exited 1",
        "checkpoint_status": "confirmed",
    })
    if notification is not None:
        status["notification_status"] = notification
    status.update(extra)
    return status


class FakeClientError(Exception):
    """botocore.exceptions.ClientError shape, without botocore."""

    def __init__(self, code, message="fake error"):
        super().__init__("{}: {}".format(code, message))
        self.response = {"Error": {"Code": code, "Message": message}}


class _Body:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload


class FakeS3:
    """Enough of the S3 client for the watchdog, with real ETag semantics."""

    def __init__(self):
        self.objects = {}  # key -> {"body": bytes, "etag": str}
        self._etag = 0
        self.puts = []
        self.list_pages = None  # optional canned pagination
        self.before_put = None  # hook simulating a concurrent writer
        self.read_errors = {}  # key -> exception to raise on get_object

    # -- test helpers -----------------------------------------------------
    def store(self, key, payload):
        self._etag += 1
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.objects[key] = {"body": body, "etag": '"etag-{}"'.format(self._etag)}
        return self.objects[key]["etag"]

    def stored_json(self, key):
        return json.loads(self.objects[key]["body"].decode())

    # -- client surface ---------------------------------------------------
    def list_objects_v2(self, Bucket, Prefix, Delimiter=None, ContinuationToken=None):  # noqa: N803
        assert Bucket == BUCKET
        if self.list_pages is not None:
            if ContinuationToken is None:
                return self.list_pages[0]
            for page in self.list_pages:
                if page.get("_token") == ContinuationToken:
                    return page
            raise AssertionError("unexpected continuation token")
        prefixes = sorted({
            key[len(Prefix):].split("/")[0]
            for key in self.objects
            if key.startswith(Prefix)
        })
        return {
            "CommonPrefixes": [{"Prefix": Prefix + name + "/"} for name in prefixes],
            "IsTruncated": False,
        }

    def get_object(self, Bucket, Key):  # noqa: N803
        assert Bucket == BUCKET
        if Key in self.read_errors:
            raise self.read_errors[Key]
        if Key not in self.objects:
            raise FakeClientError("NoSuchKey", "not found")
        entry = self.objects[Key]
        return {"Body": _Body(entry["body"]), "ETag": entry["etag"]}

    def put_object(self, Bucket, Key, Body, ContentType=None, IfMatch=None, IfNoneMatch=None):  # noqa: N803
        assert Bucket == BUCKET
        if self.before_put is not None:
            hook, self.before_put = self.before_put, None
            hook(self)
        current = self.objects.get(Key)
        if IfMatch is not None and (current is None or current["etag"] != IfMatch):
            raise FakeClientError("PreconditionFailed", "At least one of the "
                                                        "pre-conditions you specified did not hold")
        if IfNoneMatch == "*" and current is not None:
            raise FakeClientError("PreconditionFailed", "object exists")
        self.puts.append({"Key": Key, "Body": Body, "IfMatch": IfMatch})
        self._etag += 1
        self.objects[Key] = {"body": Body, "etag": '"etag-{}"'.format(self._etag)}
        return {"ETag": self.objects[Key]["etag"]}


class WatchdogTestCase(unittest.TestCase):
    def setUp(self):
        self.s3 = FakeS3()
        watchdog._S3 = self.s3
        self.sent = []
        self._orig_call = watchdog._telegram_call
        self._call_result = True
        watchdog._telegram_call = self._fake_call
        self._orig_token = watchdog.BOT_TOKEN
        self._orig_chat = watchdog.CHAT_ID
        self._orig_threshold = watchdog.STALE_AFTER_S
        self._orig_grace = watchdog.NOTIFY_AFTER_S

    def tearDown(self):
        watchdog._S3 = None
        watchdog._telegram_call = self._orig_call
        watchdog.BOT_TOKEN = self._orig_token
        watchdog.CHAT_ID = self._orig_chat
        watchdog.STALE_AFTER_S = self._orig_threshold
        watchdog.NOTIFY_AFTER_S = self._orig_grace

    def _fake_call(self, method, payload):
        self.sent.append((method, payload))
        return self._call_result

    # -- fixtures ---------------------------------------------------------
    def status_key(self, workspace="ws-alpha"):
        return "checkpoints/{}/task-status.json".format(workspace)

    def seed(self, status, workspace="ws-alpha", topic=None):
        etag = self.s3.store(self.status_key(workspace), status)
        if topic is not None:
            self.s3.store(
                "checkpoints/{}/telegram-topic.json".format(workspace), topic
            )
        return etag


class StalenessTests(WatchdogTestCase):
    def test_heartbeat_age_and_humanize(self):
        self.assertIsNone(watchdog.heartbeat_age_seconds(None))
        self.assertIsNone(watchdog.heartbeat_age_seconds(""))
        self.assertIsNone(watchdog.heartbeat_age_seconds("not-a-date"))
        self.assertIsNone(watchdog.heartbeat_age_seconds(1234))
        age = watchdog.heartbeat_age_seconds(_stamp(120))
        self.assertTrue(115 <= age <= 125, age)
        self.assertEqual(watchdog.humanize_age(-5), "0s")
        self.assertEqual(watchdog.humanize_age(45), "45s")
        self.assertEqual(watchdog.humanize_age(72), "1m 12s")
        self.assertEqual(watchdog.humanize_age(7500), "2h 05m")

    def test_default_threshold_is_600_seconds(self):
        self.assertEqual(self._orig_threshold, 600)

    def test_is_stale_only_for_running_records_past_the_threshold(self):
        self.assertFalse(watchdog.is_stale(_running(heartbeat_ago=30)))
        self.assertFalse(watchdog.is_stale(_running(heartbeat_ago=599)))
        self.assertTrue(watchdog.is_stale(_running(heartbeat_ago=601)))
        for state in ("succeeded", "failed", "timed-out", "interrupted", "none"):
            self.assertFalse(
                watchdog.is_stale(_running(heartbeat_ago=99999, state=state)), state
            )
        self.assertFalse(watchdog.is_stale(None))
        self.assertFalse(watchdog.is_stale("nonsense"))

    def test_threshold_is_configurable(self):
        watchdog.STALE_AFTER_S = 120
        self.assertTrue(watchdog.is_stale(_running(heartbeat_ago=200)))
        self.assertFalse(watchdog.is_stale(_running(heartbeat_ago=60)))


class ScanTests(WatchdogTestCase):
    # --- scenario: fresh running state -> no action -----------------------
    def test_fresh_running_task_is_left_untouched(self):
        etag = self.seed(_running(heartbeat_ago=25), topic={"chat_id": "-10042", "thread_id": 7})
        summary = watchdog.handler()
        self.assertEqual(summary, _summary(scanned=1))
        self.assertEqual(self.s3.puts, [])
        self.assertEqual(self.s3.objects[self.status_key()]["etag"], etag)
        self.assertEqual(self.sent, [])

    def test_terminal_records_are_left_untouched(self):
        for index, state in enumerate(("succeeded", "failed", "timed-out", "interrupted", "none")):
            workspace = "ws-{}".format(index)
            self.seed(_running(heartbeat_ago=99999, state=state), workspace=workspace)
        summary = watchdog.handler()
        self.assertEqual(summary["scanned"], 5)
        self.assertEqual(summary["stale"], 0)
        self.assertEqual(self.s3.puts, [])
        self.assertEqual(self.sent, [])

    # --- scenario: running stale -> riscrittura + notifica ----------------
    def test_stale_running_task_is_rewritten_and_notified(self):
        etag = self.seed(
            _running(heartbeat_ago=1860),
            topic={"chat_id": "-10042", "thread_id": 148, "fallback": False},
        )
        summary = watchdog.handler()
        self.assertEqual(summary, _summary(scanned=1, stale=1, reconciled=1, notified=1))

        # Two conditional writes: the reconciliation, fenced on the ETag
        # observed in the same tick, then the delivered mark (TASK-28), fenced
        # on the ETag the reconciliation produced.
        self.assertEqual(len(self.s3.puts), 2)
        self.assertEqual(self.s3.puts[0]["IfMatch"], etag)
        reconciled = json.loads(self.s3.puts[0]["Body"].decode())
        self.assertEqual(reconciled["notification_status"], "pending")
        self.assertNotIn("notified_utc", reconciled)
        self.assertNotEqual(self.s3.puts[1]["IfMatch"], etag)

        stored = self.s3.stored_json(self.status_key())
        self.assertEqual(stored["state"], "interrupted")
        self.assertEqual(stored["reconciled_by"], "task-watchdog")
        self.assertTrue(stored["reconciled_utc"])
        self.assertEqual(stored["outcome"], "unknown")
        self.assertEqual(stored["checkpoint_status"], "unknown")
        self.assertEqual(stored["notification_status"], "delivered")
        self.assertEqual(stored["notified_by"], "task-watchdog")
        self.assertTrue(stored["notified_utc"])
        # Fencing (design D2): identity fields are carried over verbatim.
        self.assertEqual(stored["writer_token"], TOKEN)
        self.assertEqual(stored["session_epoch"], 2)
        self.assertEqual(stored["session_id"], "sch-ws-1")
        # Observability of the death window: the heartbeat is not erased.
        self.assertEqual(stored["heartbeat_utc"], _running(heartbeat_ago=1860)["heartbeat_utc"])
        self.assertEqual(stored["harness"], "claude")
        self.assertEqual(stored["prompt"], "fai X")

        self.assertEqual(len(self.sent), 1)
        method, payload = self.sent[0]
        self.assertEqual(method, "sendMessage")
        self.assertEqual(payload["chat_id"], "-10042")
        self.assertEqual(payload["message_thread_id"], 148)
        self.assertIn("12345678", payload["text"])
        self.assertIn("31m", payload["text"])
        self.assertIn("interrupted", payload["text"])
        # In a mapped topic the [workspace] prefix would be redundant noise.
        self.assertNotIn("[ws-alpha]", payload["text"])

    def test_reconciled_body_never_invents_ownership_fields(self):
        # A record written before writer fencing existed must not gain a token.
        legacy = _running(heartbeat_ago=1200)
        legacy.pop("writer_token")
        legacy.pop("session_epoch")
        self.seed(legacy)
        watchdog.handler()
        stored = self.s3.stored_json(self.status_key())
        self.assertNotIn("writer_token", stored)
        self.assertNotIn("session_epoch", stored)
        self.assertEqual(stored["state"], "interrupted")

    # --- scenario: race con scrittura concorrente ------------------------
    def test_lost_race_rewrites_nothing_and_notifies_nothing(self):
        self.seed(_running(heartbeat_ago=1800))
        fresh = _running(heartbeat_ago=1)

        def concurrent_writer(s3):
            s3.store(self.status_key(), fresh)

        self.s3.before_put = concurrent_writer
        summary = watchdog.handler()
        self.assertEqual(summary, _summary(scanned=1, stale=1))
        self.assertEqual(self.s3.puts, [])
        self.assertEqual(self.s3.stored_json(self.status_key()), fresh)
        self.assertEqual(self.sent, [])

    # --- scenario: object with a newer epoch -> never regressed -----------
    def test_newer_epoch_writer_is_never_regressed(self):
        self.seed(_running(heartbeat_ago=3000, epoch=2, token="wt-old"))
        rotated = _running(heartbeat_ago=2, epoch=3, token="wt-new")

        def rotation(s3):
            s3.store(self.status_key(), rotated)

        self.s3.before_put = rotation
        summary = watchdog.handler()
        self.assertEqual(summary["reconciled"], 0)
        self.assertEqual(self.sent, [])
        stored = self.s3.stored_json(self.status_key())
        self.assertEqual(stored["session_epoch"], 3)
        self.assertEqual(stored["writer_token"], "wt-new")
        self.assertEqual(stored["state"], "running")

    def test_the_rewrite_never_lowers_the_epoch_it_read(self):
        self.seed(_running(heartbeat_ago=1800, epoch=7, token="wt-7"))
        watchdog.handler()
        stored = self.s3.stored_json(self.status_key())
        self.assertEqual(stored["session_epoch"], 7)
        self.assertEqual(stored["writer_token"], "wt-7")

    # --- scenario: alive-but-slow task finishing after reconciliation ------
    def test_terminal_record_of_a_slow_task_wins_over_the_reconciliation(self):
        """The shim's fence (main.py::_upload_task_status) must accept the
        write: it compares the CURRENT object's writer_token with its own, and
        the watchdog preserved exactly that token."""
        self.seed(_running(heartbeat_ago=1800))
        watchdog.handler()
        self.assertEqual(self.s3.stored_json(self.status_key())["state"], "interrupted")

        # Replay of the shim's terminal write, same discipline as the shim.
        current = self.s3.get_object(Bucket=BUCKET, Key=self.status_key())
        current_body = json.loads(current["Body"].read().decode())
        self.assertEqual(
            current_body["writer_token"], TOKEN,
            "the watchdog minted a new token: a live shim would now be fenced out",
        )
        terminal = dict(current_body)
        terminal.update({
            "state": "succeeded",
            "exit_code": 0,
            "finished_utc": _stamp(0),
            "duration_s": 4200,
            "checkpoint_status": "confirmed",
        })
        self.s3.put_object(
            Bucket=BUCKET, Key=self.status_key(),
            Body=json.dumps(terminal).encode(), ContentType="application/json",
            IfMatch=current["ETag"],
        )
        final = self.s3.stored_json(self.status_key())
        self.assertEqual(final["state"], "succeeded")
        self.assertEqual(final["exit_code"], 0)

        # And the next sweep has nothing left to do.
        summary = watchdog.handler()
        self.assertEqual(summary["stale"], 0)

    def test_running_without_a_usable_heartbeat_is_not_rewritten(self):
        """Deliberate asymmetry with `sch status` (which flags it as suspect):
        rewriting on an unprovable record would notify on every single tick."""
        for heartbeat in (None, "", "yesterday"):
            self.s3 = FakeS3()
            watchdog._S3 = self.s3
            self.sent = []
            status = _running(heartbeat_ago=9999)
            if heartbeat is None:
                status.pop("heartbeat_utc")
            else:
                status["heartbeat_utc"] = heartbeat
            self.seed(status)
            summary = watchdog.handler()
            self.assertEqual(summary["stale"], 0, heartbeat)
            self.assertEqual(self.s3.puts, [], heartbeat)
            self.assertEqual(self.sent, [], heartbeat)


class NotificationTests(WatchdogTestCase):
    def test_reconciliation_happens_without_telegram_configured(self):
        watchdog.BOT_TOKEN = ""
        watchdog.CHAT_ID = ""
        self.seed(_running(heartbeat_ago=1800))
        summary = watchdog.handler()
        self.assertEqual(summary["reconciled"], 1)
        self.assertEqual(summary["notified"], 0)
        stored = self.s3.stored_json(self.status_key())
        self.assertEqual(stored["state"], "interrupted")
        # No channel, no promise: the record must not look like a lost
        # notification to a later deploy that enables Telegram.
        self.assertNotIn("notification_status", stored)
        self.assertEqual(len(self.s3.puts), 1)
        self.assertEqual(self.sent, [])

    def test_missing_topic_mapping_falls_back_to_the_workspace_prefix(self):
        self.seed(_running(heartbeat_ago=1800))
        watchdog.handler()
        _method, payload = self.sent[0]
        self.assertNotIn("message_thread_id", payload)
        self.assertTrue(payload["text"].startswith("[ws-alpha] "), payload["text"])

    def test_persisted_fallback_mapping_uses_the_prefix_too(self):
        self.seed(
            _running(heartbeat_ago=1800),
            topic={"chat_id": "-10042", "thread_id": None, "fallback": True},
        )
        watchdog.handler()
        _method, payload = self.sent[0]
        self.assertNotIn("message_thread_id", payload)
        self.assertTrue(payload["text"].startswith("[ws-alpha] "))

    def test_mapping_of_another_chat_is_ignored(self):
        self.seed(
            _running(heartbeat_ago=1800),
            topic={"chat_id": "-9999", "thread_id": 5, "fallback": False},
        )
        watchdog.handler()
        _method, payload = self.sent[0]
        self.assertNotIn("message_thread_id", payload)

    def test_a_failed_send_does_not_undo_the_reconciliation(self):
        self._call_result = False
        self.seed(_running(heartbeat_ago=1800))
        summary = watchdog.handler()
        self.assertEqual(summary["reconciled"], 1)
        self.assertEqual(summary["notified"], 0)
        stored = self.s3.stored_json(self.status_key())
        self.assertEqual(stored["state"], "interrupted")
        # TASK-28: the promise stays open, nothing is marked delivered.
        self.assertEqual(stored["notification_status"], "pending")
        self.assertNotIn("notified_utc", stored)
        self.assertEqual(len(self.s3.puts), 1)

    def test_a_failed_reconciliation_send_is_retried_after_the_grace(self):
        """R20 moves from at-most-once to at-least-once (TASK-28): the
        watchdog's own message is re-sent by the pending path, with the same
        wording, once the grace measured from the reconciliation has passed."""
        self._call_result = False
        self.seed(_running(heartbeat_ago=1800), topic={"chat_id": "-10042", "thread_id": 9})
        watchdog.handler()
        self.assertEqual(len(self.sent), 1)  # the failed attempt
        self.assertEqual(self.s3.stored_json(self.status_key())["notification_status"], "pending")

        # Grace not elapsed yet (finished_utc == reconciliation instant).
        self._call_result = True
        self.assertEqual(watchdog.handler()["pending"], 0)
        self.assertEqual(len(self.sent), 1)

        watchdog.NOTIFY_AFTER_S = -1
        summary = watchdog.handler()
        self.assertEqual(summary["pending"], 1)
        self.assertEqual(summary["resent"], 1)
        self.assertEqual(summary["notified"], 0)
        self.assertEqual(len(self.sent), 2)
        text = self.sent[1][1]["text"]
        self.assertIn("INTERRUPTED (watchdog)", text)
        self.assertIn("no heartbeat for", text)
        self.assertNotIn("delivered by the watchdog", text)
        self.assertEqual(self.sent[1][1]["message_thread_id"], 9)
        stored = self.s3.stored_json(self.status_key())
        self.assertEqual(stored["notification_status"], "delivered")
        self.assertEqual(stored["notified_by"], "task-watchdog")
        self.assertEqual(stored["state"], "interrupted")
        # And nothing left to do afterwards.
        self.assertEqual(watchdog.handler()["pending"], 0)
        self.assertEqual(len(self.sent), 2)

    def test_send_is_exactly_once_in_the_healthy_path(self):
        self.seed(_running(heartbeat_ago=1800))
        first = watchdog.handler()
        watchdog.NOTIFY_AFTER_S = -1  # even with the grace gone: it is delivered
        second = watchdog.handler()
        self.assertEqual(first["notified"], 1)
        self.assertEqual(second["notified"], 0)
        self.assertEqual(second["pending"], 0)
        self.assertEqual(len(self.sent), 1)

    def test_bot_token_is_sanitized_out_of_logs(self):
        watchdog.BOT_TOKEN = "secret-token"
        self.assertEqual(
            watchdog._sanitize("call to bot secret-token failed"),
            "call to bot *** failed",
        )

    def test_message_without_a_readable_heartbeat_says_so(self):
        status = _running()
        status.pop("heartbeat_utc")
        text = watchdog.format_message("ws-alpha", status, 148)
        self.assertIn("no readable heartbeat", text)
        self.assertNotIn("[ws-alpha]", text)


class PendingTerminalTests(WatchdogTestCase):
    """TASK-28 (spec telegram-notifications R10, I5): a terminal record whose
    notification is still ``pending`` after the grace period is re-sent from
    here and marked ``delivered`` only when the Bot API accepted it."""

    TOPIC = {"chat_id": "-10042", "thread_id": 148, "fallback": False}

    def test_default_grace_and_max_age(self):
        self.assertEqual(self._orig_grace, 300)
        self.assertEqual(watchdog.NOTIFY_MAX_AGE_S, 86400)

    def test_predicate(self):
        self.assertTrue(watchdog.is_pending_terminal(_terminal(finished_ago=301)))
        self.assertFalse(watchdog.is_pending_terminal(_terminal(finished_ago=299)))
        self.assertFalse(watchdog.is_pending_terminal(_terminal(finished_ago=86401)))
        self.assertTrue(watchdog.is_pending_terminal(_terminal(finished_ago=86000)))
        for state in ("succeeded", "failed", "timed-out", "interrupted"):
            self.assertTrue(watchdog.is_pending_terminal(_terminal(state=state)), state)
        self.assertFalse(watchdog.is_pending_terminal(_terminal(state="running")))
        self.assertFalse(watchdog.is_pending_terminal(_terminal(state="none")))
        self.assertFalse(watchdog.is_pending_terminal(_terminal(notification="delivered")))
        self.assertFalse(watchdog.is_pending_terminal(_terminal(notification=None)))
        no_stamp = _terminal()
        no_stamp.pop("finished_utc")
        self.assertFalse(watchdog.is_pending_terminal(no_stamp))
        self.assertFalse(watchdog.is_pending_terminal(_terminal(finished_utc="yesterday")))
        self.assertFalse(watchdog.is_pending_terminal(None))
        self.assertTrue(watchdog.is_pending_terminal(_terminal(finished_ago=100), grace=60))

    # --- scenario: the microVM died before the flush ------------------------
    def test_pending_terminal_past_the_grace_is_resent_and_marked(self):
        etag = self.seed(_terminal(finished_ago=600), topic=self.TOPIC)
        summary = watchdog.handler()
        self.assertEqual(summary, _summary(scanned=1, pending=1, resent=1))

        self.assertEqual(len(self.sent), 1)
        method, payload = self.sent[0]
        self.assertEqual(method, "sendMessage")
        self.assertEqual(payload["chat_id"], "-10042")
        self.assertEqual(payload["message_thread_id"], 148)
        text = payload["text"]
        self.assertIn("✅ task 12345678 OK", text)
        self.assertIn("checkpoint: confirmed", text)
        self.assertIn("duration: 161s", text)
        self.assertIn("delivered by the watchdog", text)
        self.assertNotIn("exit code", text)
        self.assertNotIn("[ws-alpha]", text)

        # One conditional write, fenced on the ETag read in the same tick.
        self.assertEqual(len(self.s3.puts), 1)
        self.assertEqual(self.s3.puts[0]["IfMatch"], etag)
        stored = self.s3.stored_json(self.status_key())
        self.assertEqual(stored["notification_status"], "delivered")
        self.assertEqual(stored["notified_by"], "task-watchdog")
        self.assertTrue(stored["notified_utc"])
        # The record itself is untouched: state, outcome, identity fields.
        self.assertEqual(stored["state"], "succeeded")
        self.assertEqual(stored["duration_s"], 161)
        self.assertEqual(stored["checkpoint_status"], "confirmed")
        self.assertEqual(stored["writer_token"], TOKEN)
        self.assertEqual(stored["session_epoch"], 2)
        self.assertNotIn("reconciled_by", stored)

        # Second tick: delivered, nothing to do.
        self.assertEqual(watchdog.handler(), _summary(scanned=1))
        self.assertEqual(len(self.sent), 1)

    def test_pending_terminal_within_the_grace_is_left_alone(self):
        etag = self.seed(_terminal(finished_ago=120), topic=self.TOPIC)
        self.assertEqual(watchdog.handler(), _summary(scanned=1))
        self.assertEqual(self.sent, [])
        self.assertEqual(self.s3.puts, [])
        self.assertEqual(self.s3.objects[self.status_key()]["etag"], etag)

    def test_grace_is_configurable(self):
        watchdog.NOTIFY_AFTER_S = 60
        self.seed(_terminal(finished_ago=120))
        self.assertEqual(watchdog.handler()["resent"], 1)

    def test_pending_terminal_older_than_the_max_age_is_not_retried(self):
        self.seed(_terminal(finished_ago=watchdog.NOTIFY_MAX_AGE_S + 100))
        self.assertEqual(watchdog.handler(), _summary(scanned=1))
        self.assertEqual(self.sent, [])
        self.assertEqual(self.s3.puts, [])

    def test_delivered_and_unpromised_records_are_never_resent(self):
        # Delivered by the shim; written by an image predating the protocol;
        # written with the channel disabled: none of them is a lost message.
        self.seed(_terminal(notification="delivered", notified_by="shim"), workspace="ws-a")
        self.seed(_terminal(notification=None), workspace="ws-b")
        self.seed(_terminal(notification=None, state="failed"), workspace="ws-c")
        summary = watchdog.handler()
        self.assertEqual(summary, _summary(scanned=3))
        self.assertEqual(self.sent, [])
        self.assertEqual(self.s3.puts, [])

    def test_pending_flag_on_a_running_record_is_ignored(self):
        self.seed(_running(heartbeat_ago=30, notification_status="pending"))
        self.assertEqual(watchdog.handler(), _summary(scanned=1))
        self.assertEqual(self.sent, [])

    # --- scenario: Telegram still unreachable from the Lambda too -----------
    def test_failed_send_keeps_the_record_pending_for_the_next_tick(self):
        self._call_result = False
        self.seed(_terminal(finished_ago=600))
        summary = watchdog.handler()
        self.assertEqual(summary, _summary(scanned=1, pending=1))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.s3.puts, [])
        self.assertEqual(self.s3.stored_json(self.status_key())["notification_status"], "pending")

        self._call_result = True
        summary = watchdog.handler()
        self.assertEqual(summary, _summary(scanned=1, pending=1, resent=1))
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.s3.stored_json(self.status_key())["notification_status"], "delivered")

    # --- scenario: race with a new submit between the send and the mark -----
    def test_lost_race_on_the_mark_leaves_the_newer_object_authoritative(self):
        self.seed(_terminal(finished_ago=600))
        fresh = _running(heartbeat_ago=1, task_id="fedcba0987654321")

        def new_submit(s3):
            s3.store(self.status_key(), fresh)

        self.s3.before_put = new_submit
        summary = watchdog.handler()
        # The message went out (this is the accepted duplicate class), but the
        # new task's running record was never overwritten with a stale body.
        self.assertEqual(summary, _summary(scanned=1, pending=1, resent=1))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.s3.puts, [])
        self.assertEqual(self.s3.stored_json(self.status_key()), fresh)

    def test_a_status_without_an_etag_is_not_resent(self):
        # No fence means no possible mark: sending would repeat every tick.
        self.seed(_terminal(finished_ago=600))
        self.s3.objects[self.status_key()]["etag"] = ""
        summary = watchdog.handler()
        self.assertEqual(summary, _summary(scanned=1, pending=1))
        self.assertEqual(self.sent, [])
        self.assertEqual(self.s3.puts, [])

    def test_telegram_off_resends_nothing(self):
        watchdog.BOT_TOKEN = ""
        watchdog.CHAT_ID = ""
        self.seed(_terminal(finished_ago=600))
        summary = watchdog.handler()
        self.assertEqual(summary, _summary(scanned=1, pending=1))
        self.assertEqual(self.sent, [])
        self.assertEqual(self.s3.puts, [])

    # --- message shape ------------------------------------------------------
    def test_failed_message_carries_exit_code_and_error(self):
        text = watchdog.format_terminal_message(
            "ws-alpha", _terminal(state="failed", exit_code=3, error="boom " * 200), 148,
        )
        self.assertIn("❌ task 12345678 FAILED", text)
        self.assertIn("checkpoint: confirmed · duration: 161s", text)
        self.assertIn("exit code: 3", text)
        self.assertIn("error: boom", text)
        self.assertLess(len(text), 800)  # error truncated to 500 chars
        self.assertNotIn("[ws-alpha]", text)

    def test_timed_out_message_uses_the_timeout_badge(self):
        text = watchdog.format_terminal_message(
            "ws-alpha", _terminal(state="timed-out", exit_code=-9), None,
        )
        self.assertTrue(text.startswith("[ws-alpha] ❌ task 12345678 TIMEOUT"), text)
        self.assertIn("exit code: -9", text)

    def test_message_omits_unknown_duration(self):
        record = _terminal()
        record.pop("duration_s")
        text = watchdog.format_terminal_message("ws-alpha", record, 148)
        self.assertIn("checkpoint: confirmed\n", text)
        self.assertNotIn("duration", text)

    def test_missing_topic_mapping_falls_back_to_the_prefix(self):
        self.seed(_terminal(finished_ago=600))
        watchdog.handler()
        _method, payload = self.sent[0]
        self.assertNotIn("message_thread_id", payload)
        self.assertTrue(payload["text"].startswith("[ws-alpha] ✅"), payload["text"])

    def test_own_reconciled_record_keeps_the_watchdog_message(self):
        record = _terminal(
            state="interrupted", reconciled_by="task-watchdog",
            checkpoint_status="unknown", heartbeat_utc=_stamp(1800),
        )
        text = watchdog.format_terminal_message("ws-alpha", record, 148)
        self.assertIn("💀 task 12345678 INTERRUPTED (watchdog)", text)
        self.assertIn("no heartbeat for 30m", text)
        self.assertNotIn("delivered by the watchdog", text)


class RobustnessTests(WatchdogTestCase):
    def test_one_unreadable_workspace_does_not_abort_the_sweep(self):
        self.seed(_running(heartbeat_ago=1800), workspace="ws-broken")
        self.s3.read_errors[self.status_key("ws-broken")] = FakeClientError(
            "InternalError", "s3 hiccup"
        )
        self.seed(_running(heartbeat_ago=1800), workspace="ws-ok")
        summary = watchdog.handler()
        self.assertEqual(summary["scanned"], 2)
        self.assertEqual(summary["reconciled"], 1)
        self.assertEqual(
            self.s3.stored_json(self.status_key("ws-ok"))["state"], "interrupted"
        )

    def test_workspace_without_a_task_status_object_is_skipped(self):
        self.s3.store("checkpoints/ws-empty/manifest.json", {"generation": 1})
        summary = watchdog.handler()
        self.assertEqual(summary, _summary(scanned=1))

    def test_unparsable_status_object_is_skipped(self):
        self.s3.store(self.status_key(), b"{not json")
        summary = watchdog.handler()
        self.assertEqual(summary["stale"], 0)
        self.assertEqual(self.s3.puts, [])

    def test_listing_failure_returns_an_empty_summary_without_raising(self):
        def boom(**_kwargs):
            raise FakeClientError("AccessDenied", "no list for you")

        self.s3.list_objects_v2 = boom
        summary = watchdog.handler()
        self.assertEqual(summary, _summary())

    def test_a_status_without_an_etag_is_never_overwritten(self):
        self.seed(_running(heartbeat_ago=1800))
        self.s3.objects[self.status_key()]["etag"] = ""
        summary = watchdog.handler()
        self.assertEqual(summary["stale"], 1)
        self.assertEqual(summary["reconciled"], 0)
        self.assertEqual(self.s3.puts, [])
        self.assertEqual(self.sent, [])

    def test_write_failure_other_than_a_lost_race_is_not_reported_as_reconciled(self):
        self.seed(_running(heartbeat_ago=1800))

        def boom(**_kwargs):
            raise FakeClientError("AccessDenied", "no put for you")

        self.s3.put_object = boom
        summary = watchdog.handler()
        self.assertEqual(summary["stale"], 1)
        self.assertEqual(summary["reconciled"], 0)
        self.assertEqual(self.sent, [])

    def test_paginated_listing_visits_every_workspace(self):
        self.s3.list_pages = [
            {
                "CommonPrefixes": [{"Prefix": "checkpoints/ws-a/"}],
                "IsTruncated": True,
                "NextContinuationToken": "page2",
            },
            {
                "_token": "page2",
                "CommonPrefixes": [{"Prefix": "checkpoints/ws-b/"}],
                "IsTruncated": False,
            },
        ]
        self.seed(_running(heartbeat_ago=1800), workspace="ws-a")
        self.seed(_running(heartbeat_ago=10), workspace="ws-b")
        summary = watchdog.handler()
        self.assertEqual(summary["scanned"], 2)
        self.assertEqual(summary["reconciled"], 1)

    def test_unset_bucket_scans_nothing(self):
        original = watchdog.CHECKPOINT_BUCKET
        watchdog.CHECKPOINT_BUCKET = ""
        try:
            summary = watchdog.handler()
        finally:
            watchdog.CHECKPOINT_BUCKET = original
        self.assertEqual(summary, _summary())


if __name__ == "__main__":
    unittest.main()
