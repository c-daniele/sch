"""External task watchdog Lambda (add-task-liveness-safety, design D2).

The only component that can observe the death of a microVM, because it is the
only one that does not live inside it. A persisted ``running`` task status is
rewritten exclusively by the shim that owns it, so a microVM killed by the
platform (incident 2026-08-21: instance superseded by a runtime version
update) leaves ``state: running`` behind forever, with an ever-aging
``heartbeat_utc`` nobody looks at.

On an EventBridge schedule the handler scans ``checkpoints/*/task-status.json``
and, for every object whose state is ``running`` with a heartbeat older than
``SCH_WATCHDOG_STALE_S`` (default 600s = 20 missed beats — deliberately far
more tolerant than the 150s used by ``sch status``, because rewriting remote
state must never false-positive on an S3 hiccup or clock skew), it:

1. rewrites the state to ``interrupted``, **preserving the object's own
   ``writer_token`` and ``session_epoch``** instead of minting new ones. Both
   consequences are required by design:
     - a newer writer (post-rotation session) is never regressed;
     - a slow-but-alive task self-heals — its shim still holds the same token,
       so its next heartbeat or its terminal record overwrites this rewrite
       without being rejected by the shim's own writer fence
       (``main.py::_upload_task_status``);
2. does so with a **conditional** ``PutObject`` (``If-Match`` on the ETag read
   in the same tick). A lost race leaves the newer object authoritative and
   the watchdog simply retries on the next tick;
3. notifies the operator on Telegram **only when that conditional write
   succeeds**, then marks the record ``notification_status: delivered`` with
   a second conditional write. The per-workspace topic mapping written by
   the notifier (``checkpoints/<ws>/telegram-topic.json``) is reused as-is,
   with the same ``[<workspace>]`` prefix fallback for chats without Topics.

Second job (TASK-28, spec telegram-notifications R10): **at-least-once
terminal notifications**. The shim inside the microVM writes every terminal
record with ``notification_status: "pending"`` when Telegram is configured
and flips it to ``"delivered"`` only after the Bot API accepted the message.
A record still ``pending`` once ``finished_utc`` is older than
``SCH_WATCHDOG_NOTIFY_AFTER_S`` (default 300s: the in-VM path normally
delivers within seconds, a 429 back-off within a minute or two) means the
microVM's own notification was lost — died before the shutdown flush,
Telegram unreachable from inside, notifier bug — and this Lambda re-sends
the terminal message from outside, marking ``delivered`` only on success.
Records older than ``NOTIFY_MAX_AGE_S`` (24h) are no longer retried; records
without the field (older images, channel disabled) are never touched, so a
stack-only deploy or a mixed image/Lambda rollout can neither duplicate nor
burst. A duplicate is possible only when a sender fails between the send
and the mark; a lost terminal notification is not.

The watchdog never contacts the runtime: probing liveness through
``InvokeAgentRuntime`` would wake dead sessions (billing, orphan-boot side
effects) and re-introduce a dependency on the very thing being monitored.

A ``running`` record whose heartbeat is absent or unparsable is deliberately
left alone here, unlike in ``sch status`` which flags it as suspect: see
:func:`is_stale`.

Telegram is optional: with the notification channel unconfigured the state
reconciliation still runs (it is the load-bearing half), only silently, and
no ``pending`` promise is ever written.
"""

import datetime
import json
import logging
import os
import urllib.request

logger = logging.getLogger()
logger.setLevel(logging.INFO)

CHECKPOINT_BUCKET = os.environ.get("SCH_CHECKPOINT_BUCKET", "")
# 600s == 20 missed beats (design D1): 10 minutes of undetected death is an
# acceptable worst case; the incident's was unbounded.
STALE_AFTER_S = int(os.environ.get("SCH_WATCHDOG_STALE_S", "600") or "600")
# Grace for the in-VM notifier before a pending terminal record is re-sent
# from here (TASK-28). Worst-case delivery delay: this + one 2-minute tick.
NOTIFY_AFTER_S = int(os.environ.get("SCH_WATCHDOG_NOTIFY_AFTER_S", "300") or "300")
# Safety valve: a record nobody could deliver for a day stops being retried
# every tick (it stays visibly `pending` in `sch status --json`).
NOTIFY_MAX_AGE_S = 86400
BOT_TOKEN = os.environ.get("SCH_TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("SCH_TELEGRAM_CHAT_ID", "")

CHECKPOINT_PREFIX = "checkpoints/"
TASK_STATUS_OBJECT = "task-status.json"
TELEGRAM_TOPIC_OBJECT = "telegram-topic.json"

RECONCILED_BY = "task-watchdog"
HEARTBEAT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# S3 codes meaning "somebody else wrote between our GET and our PUT".
LOST_RACE_CODES = ("PreconditionFailed", "412", "ConditionalRequestConflict", "409")

# Terminal-notification protocol shared with image/app/main.py (TASK-28).
TERMINAL_STATES = ("succeeded", "failed", "timed-out", "interrupted")
NOTIFICATION_STATUS_FIELD = "notification_status"
NOTIFICATION_PENDING = "pending"
NOTIFICATION_DELIVERED = "delivered"

_S3 = None


def _s3():
    """Lazy boto3 client so unit tests can run without boto3 installed."""
    global _S3  # noqa: PLW0603
    if _S3 is None:
        import boto3

        _S3 = boto3.client("s3")
    return _S3


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc).strftime(HEARTBEAT_FORMAT)


def _sanitize(text):
    """The bot token must never reach the logs."""
    return text.replace(BOT_TOKEN, "***") if BOT_TOKEN else text


def _error_code(exc):
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return ""
    return (response.get("Error") or {}).get("Code", "")


def heartbeat_age_seconds(value, now=None):
    """Age in seconds of a shim-written ``heartbeat_utc`` stamp.

    ``None`` when the value is absent, not a string or not parsable — see
    :func:`is_stale` for what the caller does with that (deliberately less
    than what ``sch status`` does).
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        stamp = datetime.datetime.strptime(value.strip(), HEARTBEAT_FORMAT)
    except (ValueError, TypeError):
        return None
    reference = now or datetime.datetime.now(datetime.timezone.utc)
    return (reference - stamp.replace(tzinfo=datetime.timezone.utc)).total_seconds()


def humanize_age(seconds):
    """``45s`` / ``31m 12s`` / ``2h 05m`` — same shape as ``sch status``."""
    total = int(seconds) if seconds and seconds > 0 else 0
    if total < 60:
        return "{}s".format(total)
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return "{}m {}s".format(minutes, secs)
    hours, minutes = divmod(minutes, 60)
    return "{}h {:02d}m".format(hours, minutes)


def is_stale(data, now=None, threshold=None):
    """True when ``data`` is a ``running`` record whose heartbeat is too old.

    A missing or unparsable heartbeat is deliberately NOT stale here, unlike
    in ``sch status``: the CLI only degrades a rendering, this rewrites remote
    state. A writer that keeps refreshing ``running`` without ever writing a
    parsable ``heartbeat_utc`` would otherwise be reconciled — and notified —
    on every single tick. The suspect-but-unprovable case stays where it is
    harmless: the operator sees it as ``STALE: no heartbeat`` (exit code
    3) in ``sch status``, and the spec scopes this component to records whose
    *heartbeat* is older than the threshold.
    """
    if not isinstance(data, dict) or data.get("state") != "running":
        return False
    age = heartbeat_age_seconds(data.get("heartbeat_utc"), now)
    if age is None:
        return False
    return age > (STALE_AFTER_S if threshold is None else threshold)


def is_pending_terminal(data, now=None, grace=None, max_age=None):
    """True when ``data`` is a terminal record whose notification is still
    ``pending`` and whose ``finished_utc`` is old enough that the in-VM
    notifier is presumed to have failed (TASK-28).

    Three deliberate exclusions: no ``pending`` field (older image, channel
    disabled, or already ``delivered``) — nothing was promised, nothing is
    re-sent; ``finished_utc`` absent or unparsable — no bounded window can be
    computed, and this path sends messages on every tick; older than the
    max age — a notification nobody could deliver for a day is not worth a
    Bot API call every two minutes forever.
    """
    if not isinstance(data, dict) or data.get("state") not in TERMINAL_STATES:
        return False
    if data.get(NOTIFICATION_STATUS_FIELD) != NOTIFICATION_PENDING:
        return False
    age = heartbeat_age_seconds(data.get("finished_utc"), now)  # same stamp format
    if age is None:
        return False
    lower = NOTIFY_AFTER_S if grace is None else grace
    upper = NOTIFY_MAX_AGE_S if max_age is None else max_age
    return lower < age <= upper


def list_workspaces():
    """Workspace identities owning a checkpoint prefix (``ListObjectsV2`` on
    the delimiter — one call per page, no per-object listing)."""
    workspaces = []
    token = None
    while True:
        kwargs = {"Bucket": CHECKPOINT_BUCKET, "Prefix": CHECKPOINT_PREFIX, "Delimiter": "/"}
        if token:
            kwargs["ContinuationToken"] = token
        page = _s3().list_objects_v2(**kwargs)
        for entry in page.get("CommonPrefixes") or []:
            prefix = entry.get("Prefix") or ""
            name = prefix[len(CHECKPOINT_PREFIX):].rstrip("/")
            if name:
                workspaces.append(name)
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")
        if not token:
            break
    return workspaces


def _key(workspace, name):
    return "{}{}/{}".format(CHECKPOINT_PREFIX, workspace, name)


def read_status(workspace):
    """``(data, etag)`` for a workspace's task status, ``(None, None)`` when
    the object is absent or not a JSON object."""
    try:
        obj = _s3().get_object(
            Bucket=CHECKPOINT_BUCKET, Key=_key(workspace, TASK_STATUS_OBJECT)
        )
    except Exception as exc:  # noqa: BLE001
        if _error_code(exc) not in ("NoSuchKey", "404"):
            logger.warning("task status read failed for '%s': %s", workspace, exc)
        return None, None
    try:
        data = json.loads(obj["Body"].read().decode())
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        logger.warning("task status of '%s' is not readable JSON: %s", workspace, exc)
        return None, None
    if not isinstance(data, dict):
        return None, None
    return data, obj.get("ETag")


def reconciled_body(data, now=None):
    """The ``interrupted`` rewrite of a stale ``running`` record.

    ``writer_token`` and ``session_epoch`` are carried over untouched (design
    D2, fencing): this object claims no ownership of the workspace, it only
    states what is observably true — nobody has beaten the heartbeat for a
    long time. ``finished_utc`` is the reconciliation instant (same choice as
    the shim's own orphan reconciliation); the real death is bracketed by the
    preserved ``heartbeat_utc`` and ``reconciled_utc``.

    With Telegram configured the body also promises a notification
    (``notification_status: pending``, TASK-28): the send that follows the
    write flips it to ``delivered``; if that send fails, the next ticks
    re-send it like any other pending terminal record.
    """
    stamp = now or _utcnow()
    body = dict(data)
    body.update({
        "state": "interrupted",
        "outcome": "unknown",
        "finished_utc": stamp,
        "checkpoint_status": "unknown",
        "reconciled_by": RECONCILED_BY,
        "reconciled_utc": stamp,
    })
    if telegram_enabled():
        body[NOTIFICATION_STATUS_FIELD] = NOTIFICATION_PENDING
    return body


def delivered_body(data, now=None):
    """``data`` with its notification marked delivered by this component."""
    body = dict(data)
    body.update({
        NOTIFICATION_STATUS_FIELD: NOTIFICATION_DELIVERED,
        "notified_utc": now or _utcnow(),
        "notified_by": RECONCILED_BY,
    })
    return body


def conditional_rewrite(workspace, body, etag, what):
    """``If-Match`` rewrite of a workspace's task status. Returns the new
    ETag (truthy) when the write landed, ``None`` on a lost race or error.

    The only write primitive of this component: every rewrite is fenced on
    the ETag observed in the same tick, so a newer object (a live shim, a
    new submit, a concurrent sweep) always stays authoritative.
    """
    if not etag:
        # Never overwrite unconditionally: without the ETag observed in this
        # same tick there is no fence, and this component is the only one
        # allowed to rewrite a status it does not own.
        logger.warning("no ETag for the task status of '%s'; refusing to %s", workspace, what)
        return None
    try:
        response = _s3().put_object(
            Bucket=CHECKPOINT_BUCKET,
            Key=_key(workspace, TASK_STATUS_OBJECT),
            Body=json.dumps(body).encode(),
            ContentType="application/json",
            IfMatch=etag,
        )
    except Exception as exc:  # noqa: BLE001
        if _error_code(exc) in LOST_RACE_CODES or "precondition" in str(exc).lower():
            # A newer/live writer won the race: its object stays authoritative.
            # Retried on the next tick if still applicable.
            logger.info(
                "task status of '%s' changed under the watchdog; skipping %s", workspace, what
            )
            return None
        logger.warning("task status %s failed for '%s': %s", what, workspace, exc)
        return None
    return (response or {}).get("ETag") or "written"


def reconcile(workspace, body, etag):
    """Conditional ``running -> interrupted`` rewrite of ``body`` (built by
    :func:`reconciled_body`). Returns the new ETag only on a real transition
    (i.e. when the ``If-Match`` write actually landed), ``None`` otherwise."""
    return conditional_rewrite(workspace, body, etag, "reconcile")


def mark_delivered(workspace, data, etag):
    """Conditional ``pending -> delivered`` rewrite after a successful send.
    A lost race here (the shim marked it itself, or a new submit replaced
    the record) costs at most one duplicate message, never a lost one."""
    if not conditional_rewrite(workspace, delivered_body(data), etag, "mark delivered"):
        return False
    logger.info(
        "terminal notification of task %s marked delivered for '%s'",
        str(data.get("task_id") or "?")[:8], workspace,
    )
    return True


# --- Telegram notification (optional half) -------------------------------------

def telegram_enabled():
    return bool(BOT_TOKEN and CHAT_ID)


def read_topic_state(workspace):
    """The notifier's persisted per-workspace topic mapping, or ``None``.

    The watchdog only ever READS it: creating a forum topic is the notifier's
    job (it owns the reverse routing publication too), so an unmapped
    workspace degrades to the ``[<workspace>]`` prefix instead.
    """
    try:
        obj = _s3().get_object(
            Bucket=CHECKPOINT_BUCKET, Key=_key(workspace, TELEGRAM_TOPIC_OBJECT)
        )
        data = json.loads(obj["Body"].read().decode())
    except Exception as exc:  # noqa: BLE001
        if _error_code(exc) not in ("NoSuchKey", "404"):
            logger.warning("telegram topic read failed for '%s': %s", workspace, exc)
        return None
    if not isinstance(data, dict):
        return None
    if str(data.get("chat_id", CHAT_ID)) != str(CHAT_ID):
        # Mapping from another deployment/chat: unusable, fall back to prefix.
        return None
    return data


def format_message(workspace, data, thread_id):
    task_id = str(data.get("task_id") or "")[:8]
    age = heartbeat_age_seconds(data.get("heartbeat_utc"))
    lines = ["💀 task {} INTERRUPTED (watchdog)".format(task_id or "?")]
    if age is None:
        lines.append("no readable heartbeat: microVM presumed dead")
    else:
        lines.append(
            "no heartbeat for {}: microVM presumed dead".format(humanize_age(age))
        )
    lines.append("state reconciled from running to interrupted, no manual intervention needed")
    if data.get("harness"):
        lines.append("harness: {}".format(data["harness"]))
    text = "\n".join(lines)
    if thread_id is None:
        text = "[{}] {}".format(workspace, text)
    return text


_STATE_BADGE = {
    "succeeded": "OK",
    "failed": "FAILED",
    "timed-out": "TIMEOUT",
    "interrupted": "INTERRUPTED",
}


def format_terminal_message(workspace, data, thread_id):
    """The re-sent terminal notification (TASK-28): same shape as the shim's
    own ``task-terminal`` message (badge, checkpoint outcome, duration, exit
    code and error on failure) plus a line saying who delivered it, so the
    operator knows the microVM's own notification was lost and where to look.
    A pending record this component reconciled itself keeps its own message."""
    if data.get("reconciled_by") == RECONCILED_BY:
        return format_message(workspace, data, thread_id)
    state = str(data.get("state") or "?")
    task_id = str(data.get("task_id") or "")[:8]
    icon = "✅" if state == "succeeded" else "❌"
    lines = ["{} task {} {}".format(icon, task_id or "?", _STATE_BADGE.get(state, state.upper()))]
    details = ["checkpoint: {}".format(data.get("checkpoint_status") or "?")]
    if isinstance(data.get("duration_s"), (int, float)):
        details.append("duration: {}s".format(int(data["duration_s"])))
    lines.append(" · ".join(details))
    if state != "succeeded" and data.get("exit_code") is not None:
        lines.append("exit code: {}".format(data["exit_code"]))
    error = str(data.get("error") or "").strip()
    if state != "succeeded" and error:
        lines.append("error: {}".format(error[:500]))
    lines.append("delivered by the watchdog: the microVM's own notification was not confirmed")
    text = "\n".join(lines)
    if thread_id is None:
        text = "[{}] {}".format(workspace, text)
    return text


def send_telegram(workspace, data, formatter=format_message):
    """Best-effort notification; never raises. ``formatter`` renders the
    message from the record (the reconciliation message by default)."""
    if not telegram_enabled():
        return False
    state = read_topic_state(workspace) or {}
    thread_id = state.get("thread_id")
    if state.get("fallback") or not isinstance(thread_id, int):
        thread_id = None
    payload = {"chat_id": CHAT_ID, "text": formatter(workspace, data, thread_id)}
    if thread_id is not None:
        payload["message_thread_id"] = thread_id
    if not _telegram_call("sendMessage", payload):
        return False
    logger.info(
        "watchdog telegram notification sent for '%s' (chat %s, thread %s)",
        workspace, CHAT_ID, thread_id,
    )
    return True


def _telegram_call(method, payload):
    """Bot API call for the watchdog notification; never raises (the state
    rewrite is the load-bearing half and has already landed)."""
    request = urllib.request.Request(
        "https://api.telegram.org/bot{}/{}".format(BOT_TOKEN, method),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            response.read()
    except Exception as exc:  # noqa: BLE001
        logger.warning("watchdog telegram %s failed: %s", method, _sanitize(str(exc)))
        return False
    return True


# --- Entry point ---------------------------------------------------------------

def handler(_event=None, _context=None):
    """Scan once. Returns a small summary (visible in the CloudWatch logs of
    the invocation) and never raises: a scheduled invocation that throws only
    buys retry noise, and the next tick is two minutes away.

    Per workspace, two mutually exclusive jobs on the same read: a stale
    ``running`` record is reconciled (and its notification sent + marked); a
    ``pending`` terminal record past the grace period is re-sent (and marked).
    """
    summary = {
        "scanned": 0, "stale": 0, "reconciled": 0, "notified": 0,
        "pending": 0, "resent": 0,
    }
    if not CHECKPOINT_BUCKET:
        logger.error("SCH_CHECKPOINT_BUCKET is unset; nothing to scan")
        return summary
    try:
        workspaces = list_workspaces()
    except Exception:  # noqa: BLE001
        logger.exception("watchdog listing failed")
        return summary
    for workspace in workspaces:
        summary["scanned"] += 1
        try:
            data, etag = read_status(workspace)
            if data is None:
                continue
            if is_stale(data):
                summary["stale"] += 1
                age = heartbeat_age_seconds(data.get("heartbeat_utc"))
                logger.info(
                    "workspace '%s': running task %s stale (heartbeat age %s); reconciling",
                    workspace, str(data.get("task_id") or "?")[:8],
                    humanize_age(age) if age is not None else "unknown",
                )
                body = reconciled_body(data)
                new_etag = reconcile(workspace, body, etag)
                if not new_etag:
                    continue
                summary["reconciled"] += 1
                if send_telegram(workspace, body):
                    summary["notified"] += 1
                    mark_delivered(workspace, body, new_etag)
                continue
            if is_pending_terminal(data):
                summary["pending"] += 1
                if not etag:
                    # Without a fence the delivered mark could never land and
                    # this path would re-send on every tick: skip instead.
                    logger.warning(
                        "no ETag for the task status of '%s'; not re-sending its "
                        "terminal notification", workspace,
                    )
                    continue
                age = heartbeat_age_seconds(data.get("finished_utc"))
                logger.info(
                    "workspace '%s': terminal notification of task %s still pending "
                    "%s after the end; re-sending",
                    workspace, str(data.get("task_id") or "?")[:8],
                    humanize_age(age) if age is not None else "unknown",
                )
                if send_telegram(workspace, data, format_terminal_message):
                    summary["resent"] += 1
                    mark_delivered(workspace, data, etag)
        except Exception as exc:  # noqa: BLE001 — one bad workspace must not
            # abort the sweep of the others.
            logger.warning("watchdog failed on workspace '%s': %s", workspace, exc)
    logger.info(
        "watchdog sweep: scanned=%(scanned)d stale=%(stale)d "
        "reconciled=%(reconciled)d notified=%(notified)d "
        "pending=%(pending)d resent=%(resent)d", summary,
    )
    return summary
