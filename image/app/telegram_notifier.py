"""SCH Telegram notifier (add-telegram-notifications).

Outbound-only push channel from the microVM to a Telegram chat (design
D1/D3/D5): one daemon thread owned by the shim collects events from two
sources — direct in-process lifecycle calls (task submit/terminal/stall,
made by main.py) and milestone JSON files dropped by the harness hooks
into a local spool dir — and publishes them via the Bot API with plain
stdlib ``urllib`` (no new dependencies).

Contract highlights (spec: telegram-notifications):

- opt-in: fully inert unless BOTH ``SCH_TELEGRAM_BOT_TOKEN`` and
  ``SCH_TELEGRAM_CHAT_ID`` are set (``enabled()``); with the feature off
  the shim never imports side effects from here beyond module load;
- best-effort: nothing here ever raises into the caller; the enqueue is
  non-blocking and the send loop logs (token-sanitized) and moves on;
- one forum topic per WORKSPACE (design D2): ``createForumTopic`` at the
  first notification, mapping persisted via injectable load/save
  callbacks (main.py wires them to the S3 checkpoint prefix,
  ``checkpoints/<ws>/telegram-topic.json``); graceful fallback to a
  ``[<workspace>]`` text prefix for chats without Topics, persisted so
  it is not retried on every message; recreation when a persisted
  ``message_thread_id`` is rejected (topic deleted by the operator);
- bounded queue with priorities (terminal > await-input > turn-end >
  digest): on saturation the lowest-priority pending events are dropped
  first, never the terminal ones;
- coalescing: consecutive low-relevance events (tool/todo digests) of
  the same workspace within a short window collapse into one digest
  message, and a minimum per-topic send interval is enforced;
- 429 handling: respect ``retry_after``, re-queue the event;
- 4096-char hard truncation with an explicit marker;
- every successful send is logged at INFO (workspace, event type, chat,
  thread, digest count) so a delivered notification and a never-processed
  event are distinguishable from the shim's logs alone;
- an injectable ``on_sent(event)`` callback runs after every successful
  send (TASK-28): main.py uses it to mark the terminal task record as
  delivered, so the external watchdog can re-send the terminal
  notifications this process failed to deliver (at-least-once terminal
  delivery; the callback is never allowed to raise into the loop);
- the bot token never reaches the logs (URLs are built with it, but all
  logged strings pass through ``_sanitize``).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger("sch_telegram")

SPOOL_DIR = Path(os.environ.get("SCH_TELEGRAM_SPOOL_DIR", "/tmp/sch-telegram-spool"))

# Telegram API hard limit for message text.
MAX_MESSAGE_CHARS = 4096
TRUNCATION_MARKER = "\n[…truncated]"

# Priorities (lower = more important). Design D5.
PRIO_TERMINAL = 0      # task terminal states (+ submit: cheap, rare, must not be lost)
PRIO_AWAIT_INPUT = 1   # agent waiting for operator input / permission request / stall
PRIO_TURN_END = 2      # end-of-turn assistant text
PRIO_DIGEST = 3        # tool/todo high-frequency events (coalesced)

# Event type -> priority. Unknown types default to PRIO_DIGEST.
EVENT_PRIORITY = {
    "task-submitted": PRIO_TERMINAL,
    "task-terminal": PRIO_TERMINAL,
    "task-stall": PRIO_AWAIT_INPUT,
    "await-input": PRIO_AWAIT_INPUT,
    "permission-request": PRIO_AWAIT_INPUT,
    "turn-end": PRIO_TURN_END,
    "todo": PRIO_DIGEST,
    "tool": PRIO_DIGEST,
    # add-telegram-interaction: inbound-channel feedback (courtesy replies,
    # dispatch outcomes, expired-command discards) — operator-facing, never
    # coalesced away.
    "interaction": PRIO_AWAIT_INPUT,
    "command-expired": PRIO_AWAIT_INPUT,
    # add-interactive-busy-keepalive: keep-alive released while a turn may
    # still be in flight — operator should know the microVM can idle out.
    "busy-cap": PRIO_AWAIT_INPUT,
    "busy-stale": PRIO_AWAIT_INPUT,
}

QUEUE_MAX = 256
SPOOL_POLL_SECONDS = 1.5
MIN_TOPIC_INTERVAL_SECONDS = 3.0
COALESCE_WINDOW_SECONDS = 5.0

# Only spontaneous harness milestones are presence-gated. Lifecycle, inbound,
# actionable error, and administrative events bypass this set by construction.
PRESENCE_GATED_TYPES = frozenset({
    "turn-end",
    "todo",
    "tool",
    "await-input",
    "permission-request",
})


def is_presence_gated(event: dict) -> bool:
    return (
        event.get("execution_mode", "interactive") != "headless"
        and event.get("type") in PRESENCE_GATED_TYPES
    )


def config() -> tuple[str, str]:
    """(token, chat_id) from the environment — empty strings when unset."""
    return (
        os.environ.get("SCH_TELEGRAM_BOT_TOKEN", "").strip(),
        os.environ.get("SCH_TELEGRAM_CHAT_ID", "").strip(),
    )


def enabled() -> bool:
    token, chat_id = config()
    return bool(token and chat_id)


def _sanitize(text: str, token: str) -> str:
    """The token must never reach the logs (spec: 'Token never in clear')."""
    return text.replace(token, "***") if token else text


def truncate(text: str, limit: int = MAX_MESSAGE_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER


class RetryAfter(Exception):
    """Telegram 429: retry after ``seconds``."""

    def __init__(self, seconds: float):
        super().__init__(f"retry after {seconds}s")
        self.seconds = seconds


class BadThreadId(Exception):
    """The persisted message_thread_id was rejected (topic deleted)."""


class TopicsUnsupported(Exception):
    """The configured chat cannot host forum topics (private chat/group)."""


class TelegramClient:
    """Thin stdlib client for the two Bot API methods the notifier needs."""

    def __init__(self, token: str, timeout: float = 10.0):
        self._token = token
        self._timeout = timeout

    def _call(self, method: str, payload: dict) -> dict:
        url = f"https://api.telegram.org/bot{self._token}/{method}"
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read().decode("utf-8"))
            except Exception:  # noqa: BLE001
                body = {}
            description = str(body.get("description", ""))
            if exc.code == 429:
                retry_after = (
                    body.get("parameters", {}).get("retry_after", 5)
                    if isinstance(body.get("parameters"), dict) else 5
                )
                raise RetryAfter(float(retry_after)) from None
            if exc.code == 400 and "thread not found" in description.lower():
                raise BadThreadId(description) from None
            lowered = description.lower()
            if exc.code == 400 and (
                "forum" in lowered or "topics" in lowered or "not enough rights" in lowered
            ):
                raise TopicsUnsupported(description) from None
            raise RuntimeError(
                f"telegram {method} failed: HTTP {exc.code} {_sanitize(description, self._token)}"
            ) from None
        except Exception as exc:  # noqa: BLE001 — network errors etc.
            raise RuntimeError(
                f"telegram {method} failed: {_sanitize(str(exc), self._token)}"
            ) from None
        if not body.get("ok"):
            description = _sanitize(str(body.get("description", "unknown error")), self._token)
            raise RuntimeError(f"telegram {method} not ok: {description}")
        return body.get("result", {})

    def send_message(
        self,
        chat_id: str,
        text: str,
        message_thread_id: int | None = None,
        reply_markup: dict | None = None,
    ) -> dict:
        payload: dict = {"chat_id": chat_id, "text": truncate(text)}
        if message_thread_id is not None:
            payload["message_thread_id"] = message_thread_id
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self._call("sendMessage", payload)

    def edit_message_text(self, chat_id: str, message_id: int, text: str) -> dict:
        """add-telegram-interaction (task 3.2): update an approval-request
        message with its outcome; editing without reply_markup also removes
        the inline keyboard."""
        return self._call("editMessageText", {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": truncate(text),
        })

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> dict:
        payload: dict = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        return self._call("answerCallbackQuery", payload)

    def create_forum_topic(self, chat_id: str, name: str) -> int:
        result = self._call("createForumTopic", {"chat_id": chat_id, "name": name[:128]})
        thread_id = result.get("message_thread_id")
        if not isinstance(thread_id, int):
            raise RuntimeError("createForumTopic returned no message_thread_id")
        return thread_id


# --- Message formatting (OQ-N2: plain text — robust, no parse_mode escaping) ---

_STATE_BADGE = {
    "succeeded": "OK",
    "failed": "FAILED",
    "timed-out": "TIMEOUT",
    "interrupted": "INTERRUPTED",
}


def format_event(event: dict) -> str:
    """Render one spool/lifecycle event as a plain-text Telegram message."""
    etype = event.get("type", "event")
    payload = event.get("payload") or {}
    if etype == "task-submitted":
        lines = [
            f"▶️ task {payload.get('task_id', '')[:8]} submitted",
            f"harness: {payload.get('harness', '?')}"
            + (f" · model: {payload['model']}" if payload.get("model") else ""),
        ]
        prompt = (payload.get("prompt") or "").strip()
        if prompt:
            lines.append(f"prompt: {prompt[:300]}{'…' if len(prompt) > 300 else ''}")
        return "\n".join(lines)
    if etype == "task-terminal":
        state = payload.get("state", "?")
        badge = _STATE_BADGE.get(state, state.upper())
        icon = "✅" if state == "succeeded" else "❌"
        lines = [
            f"{icon} task {payload.get('task_id', '')[:8]} {badge}",
            f"checkpoint: {payload.get('checkpoint_status', '?')} · "
            f"duration: {payload.get('duration_s', '?')}s",
        ]
        if state != "succeeded" and payload.get("exit_code") is not None:
            lines.append(f"exit code: {payload['exit_code']}")
        error = (payload.get("error") or "").strip()
        if state != "succeeded" and error:
            lines.append(f"error: {error[:500]}")
        return "\n".join(lines)
    if etype == "task-stall":
        return (
            f"⚠️ task {payload.get('task_id', '')[:8]} possible stall: "
            f"no progress for {payload.get('stalled_s', '?')}s"
        )
    if etype == "await-input":
        message = (payload.get("message") or "the session is waiting for operator input").strip()
        return f"⏸️ waiting for the operator\n{message}"
    if etype == "permission-request":
        tool = payload.get("tool") or "?"
        if payload.get("request_id"):
            # add-telegram-interaction: decisional request — the buttons on
            # this message decide (design D5, case 1).
            detail = (payload.get("detail") or "").strip()
            timeout_s = payload.get("timeout_s")
            lines = [f"🔐 tool approval request: {tool}"]
            if detail:
                lines.append(detail[:500])
            minutes = int(timeout_s // 60) if isinstance(timeout_s, (int, float)) and timeout_s else None
            timeout_note = (
                f" (timeout {minutes} min, then the harness decides)." if minutes else "."
            )
            lines.append("Approve or deny with the buttons" + timeout_note)
            return "\n".join(lines)
        return f"🔐 tool approval request: {tool}\n(reply from the session — remote approval not active)"
    if etype == "interaction":
        return f"ℹ️ {(payload.get('text') or '').strip()}"
    if etype == "command-expired":
        preview = (payload.get("preview") or "").strip()
        lines = ["⌛ Telegram command discarded: it sat in the queue past its deadline and was NOT applied."]
        if preview:
            lines.append(f"content: {preview}")
        return "\n".join(lines)
    if etype == "busy-cap":
        return (
            f"⚠️ interactive keep-alive released after {payload.get('held_s', '?')}s: "
            "the maximum cap was reached. If the turn is still running the microVM "
            "may now shut down for inactivity — send a message to check the state."
        )
    if etype == "busy-stale":
        return (
            f"⚠️ no activity signal for {payload.get('quiet_s', '?')}s: "
            "interactive keep-alive released. The turn may have been "
            "interrupted before completing."
        )
    if etype == "turn-end":
        text = (payload.get("text") or "").strip() or "(turn completed)"
        return f"💬 {text}"
    if etype == "todo":
        return f"📝 todo\n{(payload.get('summary') or '').strip()}"
    if etype == "tool":
        return f"🔧 {(payload.get('summary') or payload.get('tool') or 'tool').strip()}"
    # Unknown event types degrade to a JSON dump (still truncated on send).
    return json.dumps(event, ensure_ascii=False)


class Notifier:
    """Single-owner event pump: bounded prio queue -> coalesce -> topic -> send.

    ``load_topic_state`` / ``save_topic_state`` are injected by main.py and
    persist the per-workspace mapping across microVMs (S3 checkpoint prefix).
    ``default_workspace`` resolves the workspace for spool events that do not
    carry one (the hooks don't know the workspace identity; the shim does).
    ``on_sent(event)`` is invoked after every successful send with the event
    that was published (the coalesced digest for a batch); main.py uses it to
    mark terminal task records as delivered (TASK-28).
    """

    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        client: TelegramClient | None = None,
        load_topic_state=None,
        save_topic_state=None,
        default_workspace=None,
        publish_routing=None,
        presence_at_emission=None,
        current_presence=None,
        on_sent=None,
        spool_dir: Path | None = None,
        queue_max: int = QUEUE_MAX,
        min_topic_interval: float = MIN_TOPIC_INTERVAL_SECONDS,
        coalesce_window: float = COALESCE_WINDOW_SECONDS,
        spool_poll: float = SPOOL_POLL_SECONDS,
    ):
        self._token = token
        self._chat_id = chat_id
        self._client = client or TelegramClient(token)
        self._load_topic_state = load_topic_state or (lambda workspace: None)
        self._save_topic_state = save_topic_state or (lambda workspace, state: None)
        self._default_workspace = default_workspace or (lambda: None)
        # add-telegram-interaction (task 2.2): callable(workspace, thread_id)
        # publishing the reverse thread->workspace mapping to the routing
        # table — called on topic creation AND when a persisted mapping is
        # first loaded this boot (re-verify at notifier restart, design D2).
        self._publish_routing = publish_routing or (lambda workspace, thread_id: None)
        # Defaults preserve pre-presence behavior for direct callers and tests.
        self._presence_at_emission = presence_at_emission or (lambda emitted_at: False)
        self._current_presence = current_presence or (lambda: False)
        # TASK-28: success hook (delivered-mark for terminal task records).
        self._on_sent = on_sent or (lambda event: None)
        # add-telegram-interaction: optional InteractionManager ticked from
        # the notifier's own loop (design D3: no new threads).
        self._interaction = None
        self._spool_dir = spool_dir if spool_dir is not None else SPOOL_DIR
        self._queue_max = queue_max
        self._min_topic_interval = min_topic_interval
        self._coalesce_window = coalesce_window
        self._spool_poll = spool_poll

        self._lock = threading.Lock()
        self._queue: list[tuple[int, int, dict]] = []  # (priority, seq, event)
        self._seq = 0
        self._wakeup = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # workspace -> {"thread_id": int|None, "fallback": bool} (in-memory cache
        # of the persisted state; None thread_id + fallback=True == plain chat).
        self._topics: dict[str, dict] = {}
        self._last_send: dict[str, float] = {}  # topic key -> monotonic ts
        self._last_activity: dict[str, float] = {}  # workspace -> monotonic ts
        self._last_spool_poll = 0.0

    # -- public API (all non-blocking, never raising) ---------------------------

    @property
    def client(self) -> TelegramClient:
        return self._client

    def set_interaction(self, interaction) -> None:
        """Attach the inbound InteractionManager (add-telegram-interaction)."""
        self._interaction = interaction

    def on_presence_attached(self) -> None:
        """Transfer pending approvals and wake the notifier to sync messages."""
        if self._interaction is None:
            return
        try:
            self._interaction.resolve_pending_remote_approvals()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "approval ownership transfer failed: %s",
                _sanitize(str(exc), self._token),
            )
        self._wakeup.set()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="sch-telegram-notifier", daemon=True
        )
        self._thread.start()

    def notify(self, etype: str, payload: dict, workspace: str | None = None) -> None:
        """Non-blocking enqueue from the shim's own threads."""
        try:
            self._enqueue({
                "type": etype,
                "workspace": workspace,
                "payload": payload,
                "ts": time.time(),
            })
        except Exception as exc:  # noqa: BLE001 — never propagate to primary flows
            logger.warning("telegram notify failed: %s", _sanitize(str(exc), self._token))

    def last_activity_age(self, workspace: str) -> float | None:
        """Seconds since the last milestone event seen for the workspace, or
        None when no milestone was ever observed (used by the stall detector:
        no-milestones-ever must not count as a stall — hooks may be absent)."""
        with self._lock:
            ts = self._last_activity.get(workspace)
        return (time.monotonic() - ts) if ts is not None else None

    def flush(self, timeout: float = 5.0) -> None:
        """Best-effort synchronous drain of pending PRIO_TERMINAL events
        (ordered shutdown, same spot as the final checkpoint)."""
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                with self._lock:
                    pending = [item for item in self._queue if item[0] == PRIO_TERMINAL]
                    if not pending:
                        return
                    self._queue = [i for i in self._queue if i[0] != PRIO_TERMINAL]
                for _prio, _seq, event in sorted(pending, key=lambda i: i[1]):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return
                    try:
                        self._send_event(event, respect_interval=False)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "telegram flush send failed: %s",
                            _sanitize(str(exc), self._token),
                        )
        except Exception as exc:  # noqa: BLE001
            logger.warning("telegram flush failed: %s", _sanitize(str(exc), self._token))

    # -- queue -------------------------------------------------------------------

    def _enqueue(self, event: dict) -> None:
        priority = EVENT_PRIORITY.get(event.get("type"), PRIO_DIGEST)
        with self._lock:
            if len(self._queue) >= self._queue_max:
                # Drop the lowest-priority (then newest) pending event — never
                # a terminal one (spec: 'Saturated event queue').
                victim_idx = None
                for idx, (prio, seq, _ev) in enumerate(self._queue):
                    if prio == PRIO_TERMINAL:
                        continue
                    if victim_idx is None:
                        victim_idx = idx
                        continue
                    v_prio, v_seq, _ = self._queue[victim_idx]
                    if (prio, seq) > (v_prio, v_seq):
                        victim_idx = idx
                if victim_idx is None:
                    if priority == PRIO_TERMINAL:
                        # Queue full of terminals (pathological): drop the oldest.
                        self._queue.pop(0)
                    else:
                        logger.warning("telegram queue saturated; dropping %s", event.get("type"))
                        return
                else:
                    dropped = self._queue.pop(victim_idx)
                    if EVENT_PRIORITY.get(event.get("type"), PRIO_DIGEST) > dropped[0]:
                        # The newcomer is even less important than the victim.
                        self._queue.append(dropped)
                        return
            self._seq += 1
            self._queue.append((priority, self._seq, event))
            if event.get("type") not in ("task-submitted", "task-terminal", "task-stall"):
                ws = event.get("workspace") or self._default_workspace() or ""
                self._last_activity[ws] = time.monotonic()
        self._wakeup.set()

    # -- worker loop ---------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._poll_spool()
                self._drain_once()
            except Exception as exc:  # noqa: BLE001 — loop must never die
                logger.error(
                    "telegram notifier iteration failed: %s",
                    _sanitize(str(exc), self._token),
                )
            # add-telegram-interaction: inbound tick (command poll + approval
            # message sync). The manager's tick() traps its own errors, this
            # guard is belt-and-braces for the loop's survival.
            if self._interaction is not None:
                try:
                    self._interaction.tick()
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "telegram interaction tick failed: %s",
                        _sanitize(str(exc), self._token),
                    )
            self._wakeup.wait(self._spool_poll)
            self._wakeup.clear()

    def _poll_spool(self) -> None:
        """Consume milestone JSON files dropped by the harness hooks."""
        try:
            if not self._spool_dir.is_dir():
                return
            entries = sorted(self._spool_dir.iterdir())
        except OSError:
            return
        for entry in entries:
            if not entry.name.endswith(".json"):
                continue
            try:
                raw = entry.read_text(encoding="utf-8")
                event = json.loads(raw)
                if not isinstance(event, dict) or "type" not in event:
                    raise ValueError("event is not an object with a 'type'")
            except Exception as exc:  # noqa: BLE001 — malformed: log and discard
                logger.warning("discarding malformed spool event %s: %s", entry.name, exc)
                try:
                    entry.unlink()
                except OSError:
                    pass
                continue
            if not self._suppress_for_presence(event, current_only=False):
                self._enqueue(event)
            try:
                entry.unlink()
            except OSError:
                pass

    def _permission_fallback(self, event: dict) -> bool:
        if self._interaction is None:
            return False
        try:
            return bool(self._interaction.fallback_permission_request(event))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "permission native fallback failed: %s",
                _sanitize(str(exc), self._token),
            )
            return False

    def _permission_resolved(self, event: dict) -> bool:
        if self._interaction is None:
            return False
        check = getattr(self._interaction, "permission_request_resolved", None)
        if check is None:
            return False
        try:
            return bool(check(event))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "permission resolution check failed: %s",
                _sanitize(str(exc), self._token),
            )
            return False

    def _suppress_for_presence(self, event: dict, *, current_only: bool) -> bool:
        if not is_presence_gated(event):
            return False
        actionable_permission = (
            event.get("type") == "permission-request"
            and bool((event.get("payload") or {}).get("request_id"))
        )
        if current_only and not actionable_permission:
            return False
        if actionable_permission and self._permission_resolved(event):
            return True
        try:
            attached = False
            if not current_only:
                attached = bool(self._presence_at_emission(event.get("ts")))
            if actionable_permission:
                attached = attached or bool(self._current_presence())
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "presence evaluation failed open to detached: %s",
                _sanitize(str(exc), self._token),
            )
            return False
        if not attached:
            return False
        if actionable_permission:
            return self._permission_fallback(event)
        return True

    def _drain_once(self) -> None:
        while True:
            batch = self._take_batch()
            if not batch:
                return
            event = self._coalesce(batch)
            try:
                self._send_event(event)
            except RetryAfter as exc:
                # Re-queue and back off (spec: 429/retry_after, terminal
                # events are never lost).
                with self._lock:
                    self._seq += 1
                    self._queue.append(
                        (EVENT_PRIORITY.get(event.get("type"), PRIO_DIGEST), self._seq, event)
                    )
                time.sleep(min(exc.seconds, 60.0))
            except Exception as exc:  # noqa: BLE001
                logger.warning("telegram send failed: %s", _sanitize(str(exc), self._token))

    def _take_batch(self) -> list[dict]:
        """Pop the highest-priority event; when it is a digest-class event,
        also pop the other coalesceable events of the same workspace queued
        within the coalescing window (design D5)."""
        with self._lock:
            if not self._queue:
                return []
            self._queue.sort(key=lambda item: (item[0], item[1]))
            prio, _seq, head = self._queue.pop(0)
            if prio != PRIO_DIGEST:
                return [head]
            ws = head.get("workspace")
            head_ts = head.get("ts") or 0
            batch = [head]
            remaining = []
            for item in self._queue:
                i_prio, _i_seq, ev = item
                if (
                    i_prio == PRIO_DIGEST
                    and ev.get("workspace") == ws
                    and abs((ev.get("ts") or 0) - head_ts) <= self._coalesce_window
                ):
                    batch.append(ev)
                else:
                    remaining.append(item)
            self._queue = remaining
            return batch

    @staticmethod
    def _coalesce(batch: list[dict]) -> dict:
        if len(batch) == 1:
            return batch[0]
        lines = []
        for ev in batch:
            payload = ev.get("payload") or {}
            summary = (payload.get("summary") or payload.get("tool") or ev.get("type") or "").strip()
            if summary:
                lines.append(f"• {summary}")
        return {
            "type": "digest",
            "workspace": batch[0].get("workspace"),
            "payload": {"text": "\n".join(lines)},
            "ts": batch[-1].get("ts"),
            "_digest_count": len(batch),
        }

    # -- topic resolution + send -----------------------------------------------

    def _topic_state(self, workspace: str) -> dict:
        state = self._topics.get(workspace)
        if state is not None:
            return state
        persisted = None
        try:
            persisted = self._load_topic_state(workspace)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "telegram topic mapping load failed for '%s': %s",
                workspace, _sanitize(str(exc), self._token),
            )
        if (
            isinstance(persisted, dict)
            and str(persisted.get("chat_id", self._chat_id)) == str(self._chat_id)
            and (isinstance(persisted.get("thread_id"), int) or persisted.get("fallback"))
        ):
            state = {
                "thread_id": persisted.get("thread_id"),
                "fallback": bool(persisted.get("fallback")),
            }
            # Re-verify the inbound routing on every (re)start (task 2.2):
            # the S3 mapping and the DynamoDB routing item are written by the
            # same owner but are not transactional — a failed routing put in
            # a previous life is healed here.
            self._notify_routing(workspace, state)
        else:
            state = self._create_topic(workspace)
        self._topics[workspace] = state
        return state

    def _notify_routing(self, workspace: str, state: dict) -> None:
        try:
            self._publish_routing(workspace, state.get("thread_id"))
        except Exception as exc:  # noqa: BLE001 — retried on the next boot/create
            logger.warning(
                "telegram routing publish failed for '%s': %s",
                workspace, _sanitize(str(exc), self._token),
            )

    def _create_topic(self, workspace: str) -> dict:
        try:
            thread_id = self._client.create_forum_topic(self._chat_id, workspace)
            state = {"thread_id": thread_id, "fallback": False}
        except (TopicsUnsupported, RuntimeError) as exc:
            # Chat without Topics (or bot without rights): degrade to the plain
            # chat with a [<workspace>] prefix, persisted so createForumTopic
            # is not retried on every message (design D2). RetryAfter/network
            # errors bubble up instead (retried by the caller).
            logger.info(
                "telegram forum topic unavailable for '%s' (%s); using [workspace] prefix",
                workspace, _sanitize(str(exc), self._token),
            )
            state = {"thread_id": None, "fallback": True}
        try:
            self._save_topic_state(workspace, {
                "chat_id": self._chat_id,
                "thread_id": state["thread_id"],
                "fallback": state["fallback"],
            })
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "telegram topic mapping save failed for '%s': %s",
                workspace, _sanitize(str(exc), self._token),
            )
        # add-telegram-interaction (task 2.2): the same owner that creates
        # the topic publishes the reverse thread->workspace routing item.
        self._notify_routing(workspace, state)
        return state

    def _send_event(self, event: dict, respect_interval: bool = True) -> None:
        # Close the attach race after spool admission but before publication.
        if self._suppress_for_presence(event, current_only=True):
            return
        workspace = event.get("workspace") or self._default_workspace() or "workspace"
        state = self._topic_state(workspace)
        if event.get("type") == "digest":
            count = event.get("_digest_count", 0)
            text = f"🔧 digest ({count} events)\n{(event.get('payload') or {}).get('text', '')}"
        else:
            text = format_event(event)
        if state.get("fallback") or state.get("thread_id") is None:
            text = f"[{workspace}] {text}"

        if respect_interval:
            key = f"{workspace}:{state.get('thread_id')}"
            last = self._last_send.get(key, 0.0)
            wait = self._min_topic_interval - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)

        # add-telegram-interaction (task 3.2): decisional permission requests
        # carry the Approve/Deny inline keyboard, and the sent message id is
        # handed to the InteractionManager for the later outcome edit.
        reply_markup = None
        if self._interaction is not None:
            try:
                reply_markup = self._interaction.reply_markup_for(event)
            except Exception:  # noqa: BLE001
                reply_markup = None

        # Topic lookup and rate limiting can take long enough for a reconnect.
        # Recheck actionable approval ownership immediately before publishing.
        if self._suppress_for_presence(event, current_only=True):
            return

        send_kwargs = {"reply_markup": reply_markup} if reply_markup is not None else {}
        try:
            result = self._client.send_message(
                self._chat_id, text, state.get("thread_id"), **send_kwargs
            )
        except BadThreadId:
            # Persisted topic was deleted: recreate and retry once (spec:
            # 'Topic reuse after microVM recreation').
            logger.info("telegram topic for '%s' was deleted; recreating", workspace)
            state = self._create_topic(workspace)
            self._topics[workspace] = state
            retry_text = text
            if state.get("fallback") or state.get("thread_id") is None:
                retry_text = f"[{workspace}] {format_event(event)}"
            if self._suppress_for_presence(event, current_only=True):
                return
            result = self._client.send_message(
                self._chat_id, retry_text, state.get("thread_id"), **send_kwargs
            )
            text = retry_text
        self._last_send[f"{workspace}:{state.get('thread_id')}"] = time.monotonic()
        # add-task-liveness-safety (task 4.3, design D5): the SUCCESS half of
        # the send outcome. The incident investigation had to infer delivery
        # from a routing side-effect log — "sent" and "never processed" must
        # never be indistinguishable again. Sanitized like every logged
        # string, even though no field here carries the token.
        digest_count = event.get("_digest_count")
        logger.info("%s", _sanitize(
            f"telegram sent: workspace={workspace} type={event.get('type')} "
            f"chat={self._chat_id} thread={state.get('thread_id')}"
            + (f" digest_count={digest_count}" if digest_count else ""),
            self._token,
        ))
        # TASK-28: the delivered-mark hook runs only here, after the Bot API
        # accepted the message — never on a failed or suppressed send. Its
        # failure must not turn a delivered notification into a loop error.
        try:
            self._on_sent(event)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "telegram on_sent hook failed for %s: %s",
                event.get("type"), _sanitize(str(exc), self._token),
            )
        if self._interaction is not None and reply_markup is not None:
            try:
                self._interaction.on_permission_published(event, result, workspace, text)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "interaction publish tracking failed: %s",
                    _sanitize(str(exc), self._token),
                )


def build_from_env(**kwargs) -> Notifier | None:
    """Notifier when the feature is configured, else None (opt-in contract)."""
    token, chat_id = config()
    if not token or not chat_id:
        return None
    return Notifier(token, chat_id, **kwargs)
