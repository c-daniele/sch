"""Telegram webhook router Lambda (add-telegram-interaction, design D1/D6).

Single central consumer of the bot's inbound updates (the Bot API allows one
webhook per token). The Lambda never executes anything: it validates, routes
and enqueues — execution happens only in the microVM of the resolved
workspace, which polls its own command queue.

Validation order (design D1):
1. ``X-Telegram-Bot-Api-Secret-Token`` header equals the secret registered
   with setWebhook — anything else is dropped silently (never a talking
   error toward the outside);
2. chat id equals the single configured chat (single user);
3. ``message_thread_id`` (or the plain-chat fallback key) resolves to a
   workspace through the routing table.

Valid updates become command items:
  text messages    -> {type: "text", text}
  callback queries -> {type: "decision", request_id, decision, ...}
                      (callback_data = "<request_id>:approve|deny")

The bot token in the environment is used ONLY for courtesy replies on the
discard path (unmapped topic, unroutable callback): the operator learns why
nothing happened, an attacker learns nothing (courtesy replies go only to
the allowlisted chat, after the secret check).
"""

import hmac
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request

logger = logging.getLogger()
logger.setLevel(logging.INFO)

COMMANDS_TABLE = os.environ.get("TELEGRAM_COMMANDS_TABLE", "")
ROUTING_TABLE = os.environ.get("TELEGRAM_ROUTING_TABLE", "")
WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
COMMAND_TTL_SECONDS = int(os.environ.get("COMMAND_TTL_SECONDS", "900"))

# The routing key for messages arriving outside any forum topic (change 1
# fallback mode: chat without Topics). One workspace at most can own it.
PLAIN_CHAT_THREAD_KEY = "chat"

CALLBACK_RE = re.compile(r"^([A-Za-z0-9_-]{1,64}):(approve|deny)$")

_DDB = None


def _ddb():
    """Lazy boto3 client so unit tests can run without boto3 installed."""
    global _DDB  # noqa: PLW0603
    if _DDB is None:
        import boto3

        _DDB = boto3.client("dynamodb")
    return _DDB


OK = {"statusCode": 200, "headers": {"content-type": "application/json"}, "body": "{}"}


def _telegram_call(method, payload):
    """Best-effort Bot API call for courtesy replies; never raises."""
    if not BOT_TOKEN:
        return
    try:
        request = urllib.request.Request(
            "https://api.telegram.org/bot{}/{}".format(BOT_TOKEN, method),
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as resp:
            resp.read()
    except Exception:  # noqa: BLE001 — courtesy only, never a hard failure
        logger.warning("telegram %s courtesy call failed", method)


def _secret_ok(event):
    headers = event.get("headers") or {}
    provided = ""
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == "x-telegram-bot-api-secret-token":
            provided = value or ""
            break
    return bool(WEBHOOK_SECRET) and hmac.compare_digest(provided, WEBHOOK_SECRET)


def _thread_key(message):
    thread_id = message.get("message_thread_id")
    return str(thread_id) if isinstance(thread_id, int) else PLAIN_CHAT_THREAD_KEY


def _lookup_workspace(thread_key):
    response = _ddb().get_item(
        TableName=ROUTING_TABLE,
        Key={"threadId": {"S": thread_key}},
        ConsistentRead=True,
    )
    item = response.get("Item") or {}
    workspace = (item.get("workspace") or {}).get("S")
    return workspace or None


def _put_command(workspace, update_id, thread_key, command):
    now = int(time.time())
    item = {
        "workspace": {"S": workspace},
        # Millisecond timestamp + update_id: per-workspace ordering with a
        # unique SK even for updates landing in the same millisecond.
        "sk": {"S": "{:013d}#{}".format(int(time.time() * 1000), update_id)},
        "type": {"S": command["type"]},
        "updateId": {"N": str(update_id)},
        "threadId": {"S": thread_key},
        "createdAt": {"N": str(now)},
        "expiresAt": {"N": str(now + COMMAND_TTL_SECONDS)},
    }
    for key, value in command.items():
        if key == "type":
            continue
        if isinstance(value, int):
            item[key] = {"N": str(value)}
        elif isinstance(value, str) and value:
            item[key] = {"S": value}
    _ddb().put_item(TableName=COMMANDS_TABLE, Item=item)


def _courtesy_unmapped(message, thread_key):
    """A topic without routing (design D7): explain instead of staying mute."""
    payload = {
        "chat_id": message.get("chat", {}).get("id"),
        "text": (
            "This topic is not linked to any workspace: the link is created "
            "by the workspace's first notification. Your message was not "
            "forwarded."
        ),
    }
    if thread_key != PLAIN_CHAT_THREAD_KEY:
        payload["message_thread_id"] = int(thread_key)
    _telegram_call("sendMessage", payload)


def _handle_message(update):
    message = update.get("message") or {}
    chat_id = message.get("chat", {}).get("id")
    if str(chat_id) != CHAT_ID:
        logger.info("dropping update from unauthorized chat")
        return
    if (message.get("from") or {}).get("is_bot"):
        return
    text = message.get("text")
    if not isinstance(text, str) or not text.strip():
        # Service messages, stickers, media: nothing routable, drop silently.
        return
    thread_key = _thread_key(message)
    workspace = _lookup_workspace(thread_key)
    if not workspace:
        _courtesy_unmapped(message, thread_key)
        return
    _put_command(workspace, update.get("update_id", 0), thread_key, {
        "type": "text",
        "text": text.strip(),
    })


def _handle_callback_query(update):
    callback = update.get("callback_query") or {}
    message = callback.get("message") or {}
    chat_id = message.get("chat", {}).get("id")
    if str(chat_id) != CHAT_ID:
        logger.info("dropping callback from unauthorized chat")
        return
    match = CALLBACK_RE.match(callback.get("data") or "")
    if not match:
        _telegram_call("answerCallbackQuery", {
            "callback_query_id": callback.get("id"),
        })
        return
    request_id, decision = match.group(1), match.group(2)
    thread_key = _thread_key(message)
    workspace = _lookup_workspace(thread_key)
    if not workspace:
        _telegram_call("answerCallbackQuery", {
            "callback_query_id": callback.get("id"),
            "text": "Request cannot be routed: topic has no workspace.",
        })
        return
    _put_command(workspace, update.get("update_id", 0), thread_key, {
        "type": "decision",
        "requestId": request_id,
        "decision": decision,
        "callbackQueryId": callback.get("id") or "",
        "messageId": message.get("message_id") or 0,
    })
    # No answerCallbackQuery here: the microVM answers with the real outcome
    # after depositing the decision (or with "already resolved" when late).


def handler(event, _context):
    try:
        if not _secret_ok(event):
            # Wrong/absent secret: 200 empty, no detail leaks, no retry storm.
            logger.info("dropping request with missing/invalid webhook secret")
            return OK
        try:
            update = json.loads(event.get("body") or "{}")
        except (TypeError, ValueError):
            return OK
        if not isinstance(update, dict):
            return OK
        if "message" in update:
            _handle_message(update)
        elif "callback_query" in update:
            _handle_callback_query(update)
        # Any other update type (edits, reactions, joins): ignored by design.
    except Exception:  # noqa: BLE001 — never a talking error toward Telegram
        logger.exception("telegram webhook processing failed")
    return OK
