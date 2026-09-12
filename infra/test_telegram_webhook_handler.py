"""Isolated Telegram webhook handler tests — no DynamoDB, no network
(add-telegram-interaction, task 1.5, pattern of test_workspace_registry_handler).
"""

import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("TELEGRAM_COMMANDS_TABLE", "commands-tests")
os.environ.setdefault("TELEGRAM_ROUTING_TABLE", "routing-tests")
os.environ.setdefault("TELEGRAM_WEBHOOK_SECRET", "s3cr3t")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "token-tests")
os.environ.setdefault("TELEGRAM_CHAT_ID", "-10042")

_MODULE = Path(__file__).with_name("telegram_webhook_handler.py")
_SPEC = importlib.util.spec_from_file_location("telegram_webhook_handler", _MODULE)
handler = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = handler
_SPEC.loader.exec_module(handler)


class FakeDdb:
    """Just enough of the low-level DynamoDB client for the handler."""

    def __init__(self, routing=None):
        self.routing = routing or {}
        self.put_items = []

    def get_item(self, TableName, Key, **_kwargs):  # noqa: N803
        workspace = self.routing.get(Key["threadId"]["S"])
        if workspace is None:
            return {}
        return {"Item": {"threadId": Key["threadId"], "workspace": {"S": workspace}}}

    def put_item(self, TableName, Item):  # noqa: N803
        self.put_items.append((TableName, Item))


def _event(update, secret="s3cr3t"):
    headers = {}
    if secret is not None:
        headers["X-Telegram-Bot-Api-Secret-Token"] = secret
    return {"headers": headers, "body": json.dumps(update)}


def _message_update(text="hello", chat_id=-10042, thread_id=77, update_id=5):
    return {
        "update_id": update_id,
        "message": {
            "message_id": 1,
            "chat": {"id": chat_id},
            "from": {"id": 1, "is_bot": False},
            "message_thread_id": thread_id,
            "text": text,
        },
    }


class WebhookTests(unittest.TestCase):
    def setUp(self):
        self.ddb = FakeDdb(routing={"77": "my-project"})
        handler._DDB = self.ddb
        self.courtesy = []
        self._orig_call = handler._telegram_call
        handler._telegram_call = lambda method, payload: self.courtesy.append((method, payload))

    def tearDown(self):
        handler._DDB = None
        handler._telegram_call = self._orig_call

    # --- scenario: wrong secret token ---------------------------------------
    def test_wrong_secret_is_dropped_without_effects_or_detail(self):
        for secret in (None, "", "wrong"):
            response = handler.handler(_event(_message_update(), secret=secret), None)
            self.assertEqual(response["statusCode"], 200)
            self.assertEqual(response["body"], "{}")
        self.assertEqual(self.ddb.put_items, [])
        self.assertEqual(self.courtesy, [])

    # --- scenario: unauthorized chat -----------------------------------------
    def test_unauthorized_chat_enqueues_nothing(self):
        response = handler.handler(_event(_message_update(chat_id=999)), None)
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(self.ddb.put_items, [])
        self.assertEqual(self.courtesy, [])

    # --- scenario: unmapped topic --------------------------------------------
    def test_unmapped_topic_gets_courtesy_reply_and_no_command(self):
        response = handler.handler(_event(_message_update(thread_id=12345)), None)
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(self.ddb.put_items, [])
        self.assertEqual(len(self.courtesy), 1)
        method, payload = self.courtesy[0]
        self.assertEqual(method, "sendMessage")
        self.assertEqual(payload["message_thread_id"], 12345)

    # --- scenario: authentic update routed -----------------------------------
    def test_valid_text_update_is_enqueued_for_the_workspace(self):
        response = handler.handler(_event(_message_update(text="  do X  ")), None)
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(len(self.ddb.put_items), 1)
        table, item = self.ddb.put_items[0]
        self.assertEqual(table, "commands-tests")
        self.assertEqual(item["workspace"]["S"], "my-project")
        self.assertEqual(item["type"]["S"], "text")
        self.assertEqual(item["text"]["S"], "do X")
        self.assertEqual(item["threadId"]["S"], "77")
        self.assertIn("#5", item["sk"]["S"])
        ttl = int(item["expiresAt"]["N"]) - int(item["createdAt"]["N"])
        self.assertEqual(ttl, handler.COMMAND_TTL_SECONDS)

    def test_plain_chat_fallback_uses_the_chat_routing_key(self):
        self.ddb.routing[handler.PLAIN_CHAT_THREAD_KEY] = "solo-ws"
        update = _message_update(thread_id=None)
        del update["message"]["message_thread_id"]
        handler.handler(_event(update), None)
        self.assertEqual(self.ddb.put_items[0][1]["workspace"]["S"], "solo-ws")

    def test_bot_and_non_text_messages_are_dropped_silently(self):
        bot_update = _message_update()
        bot_update["message"]["from"]["is_bot"] = True
        sticker_update = _message_update()
        del sticker_update["message"]["text"]
        for update in (bot_update, sticker_update):
            handler.handler(_event(update), None)
        self.assertEqual(self.ddb.put_items, [])
        self.assertEqual(self.courtesy, [])

    # --- callback query (decisioni) ------------------------------------------
    def _callback_update(self, data="req42:approve", chat_id=-10042, thread_id=77):
        return {
            "update_id": 9,
            "callback_query": {
                "id": "cbq-1",
                "data": data,
                "message": {
                    "message_id": 33,
                    "chat": {"id": chat_id},
                    "message_thread_id": thread_id,
                },
            },
        }

    def test_valid_callback_is_enqueued_as_decision(self):
        handler.handler(_event(self._callback_update()), None)
        self.assertEqual(len(self.ddb.put_items), 1)
        _table, item = self.ddb.put_items[0]
        self.assertEqual(item["type"]["S"], "decision")
        self.assertEqual(item["requestId"]["S"], "req42")
        self.assertEqual(item["decision"]["S"], "approve")
        self.assertEqual(item["callbackQueryId"]["S"], "cbq-1")
        self.assertEqual(item["messageId"]["N"], "33")
        # No premature answerCallbackQuery: the microVM answers with the outcome.
        self.assertEqual(self.courtesy, [])

    def test_callback_from_unauthorized_chat_is_dropped(self):
        handler.handler(_event(self._callback_update(chat_id=1)), None)
        self.assertEqual(self.ddb.put_items, [])
        self.assertEqual(self.courtesy, [])

    def test_malformed_callback_data_is_answered_and_not_enqueued(self):
        handler.handler(_event(self._callback_update(data="garbage")), None)
        self.assertEqual(self.ddb.put_items, [])
        self.assertEqual([m for m, _p in self.courtesy], ["answerCallbackQuery"])

    def test_unroutable_callback_gets_courtesy_answer(self):
        handler.handler(_event(self._callback_update(thread_id=555)), None)
        self.assertEqual(self.ddb.put_items, [])
        self.assertEqual([m for m, _p in self.courtesy], ["answerCallbackQuery"])

    # --- robustness -----------------------------------------------------------
    def test_malformed_body_and_internal_errors_stay_silent_200(self):
        response = handler.handler({"headers": {"x-telegram-bot-api-secret-token": "s3cr3t"}, "body": "{not json"}, None)
        self.assertEqual(response["statusCode"], 200)

        def boom(*_a, **_k):
            raise RuntimeError("table down")

        self.ddb.get_item = boom
        response = handler.handler(_event(_message_update()), None)
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(response["body"], "{}")


if __name__ == "__main__":
    unittest.main()
