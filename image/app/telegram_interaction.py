"""SCH Telegram inbound interaction (add-telegram-interaction).

Three cooperating pieces, all owned by the shim's notifier thread (design
D3/D4 — no new threads, no inbound connections):

- ``ApprovalBroker``: file-based rendez-vous between the harness permission
  hooks (separate processes: the claude PreToolUse hook, the opencode plugin
  and the pi extension) and the remote decisions. Protocol under
  ``SCH_APPROVAL_DIR``:

  * ``requests/<id>.json``  — request record, written once by the hook;
  * ``decisions/<id>.json`` — THE decision, exactly one per request
    (atomic ``os.link`` create; later writers lose), outcome one of
    ``approve | deny | timeout``. What ``timeout`` MEANS is per harness: for
    opencode and claude control returns to the native prompt, for pi — which
    has none — the tool is refused (design D7 of add-pi-harness);
  * ``native/<id>.json``    — dual-control marker: the harness resolved the
    request natively (operator in the TUI) after the remote wait ended.

- ``CommandQueue``: poll + consume of the per-workspace DynamoDB command
  table. Consumption is a conditional Delete BEFORE applying (design D3):
  a lost race means someone else consumed it — skip, never re-apply.
  Commands past their ``expiresAt`` are consumed but flagged ``expired``
  so the caller can discard them WITH a notification.

- ``InteractionManager``: the glue ticked by the notifier loop — polls the
  queue while the gate is open (active session/task), routes decisions to
  the broker (answering the callback query with the outcome), hands free
  text to the injected dispatcher (design D5, implemented by the shim),
  and keeps the Telegram approval messages in sync with the broker state
  (``editMessageText``: approved/denied/timeout/resolved-natively).

Everything here is best-effort by contract: a broken inbound channel must
never degrade sessions, tasks or the outbound notifications (design D7).
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path

logger = logging.getLogger("sch_telegram_interaction")

APPROVAL_DIR = Path(os.environ.get("SCH_APPROVAL_DIR", "/tmp/sch-approval"))
# OQ-I1: default remote-approval wait 10 minutes, below the 11-minute hook
# timeout configured by the templates (claude hooks would otherwise kill the
# waiting hook process and lose the fail-safe path).
DEFAULT_APPROVAL_TIMEOUT_S = int(os.environ.get("SCH_APPROVAL_TIMEOUT_S", "600"))

VALID_OUTCOMES = ("approve", "deny", "timeout", "fallback")


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write_json_atomic(path: Path, payload: dict) -> None:
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


class ApprovalBroker:
    """File protocol accessor (shim side; the hooks speak the same protocol
    directly in their own languages — see the templates)."""

    def __init__(self, base_dir: Path | None = None):
        self.base = Path(base_dir) if base_dir is not None else APPROVAL_DIR
        self.requests_dir = self.base / "requests"
        self.decisions_dir = self.base / "decisions"
        self.native_dir = self.base / "native"

    def ensure_dirs(self) -> None:
        for directory in (self.requests_dir, self.decisions_dir, self.native_dir):
            directory.mkdir(parents=True, exist_ok=True)

    # -- requests ---------------------------------------------------------------

    def create_request(
        self,
        tool: str,
        detail: str = "",
        timeout_s: int = DEFAULT_APPROVAL_TIMEOUT_S,
        source: str = "shim",
        request_id: str | None = None,
    ) -> str:
        self.ensure_dirs()
        rid = request_id or uuid.uuid4().hex[:16]
        _write_json_atomic(self.requests_dir / f"{rid}.json", {
            "id": rid,
            "tool": tool,
            "detail": detail,
            "timeout_s": timeout_s,
            "source": source,
            "execution_mode": "interactive",
            "created_ts": time.time(),
        })
        return rid

    def has_request(self, rid: str) -> bool:
        return (self.requests_dir / f"{rid}.json").is_file()

    def get_request(self, rid: str) -> dict | None:
        return _read_json(self.requests_dir / f"{rid}.json")

    def pending_request_ids(self) -> list[str]:
        """Requests without a decision — the ones a text message must NOT
        answer (design D5, case 1: only the buttons decide)."""
        try:
            entries = sorted(self.requests_dir.iterdir())
        except OSError:
            return []
        pending = []
        for entry in entries:
            if not entry.name.endswith(".json") or ".tmp-" in entry.name:
                continue
            rid = entry.name[: -len(".json")]
            if not (self.decisions_dir / f"{rid}.json").is_file():
                pending.append(rid)
        return pending

    def has_pending_requests(self) -> bool:
        return bool(self.pending_request_ids())

    def resolve_pending_as_fallback(self, source: str = "presence-attached") -> list[str]:
        """Atomically return pending interactive approvals to native ownership."""
        resolved = []
        for rid in self.pending_request_ids():
            request = self.get_request(rid) or {}
            if request.get("source") not in ("opencode", "claude"):
                continue
            if request.get("execution_mode", "interactive") == "headless":
                continue
            if self.deposit_decision(rid, "fallback", source=source):
                resolved.append(rid)
        return resolved

    # -- decisions ---------------------------------------------------------------

    def deposit_decision(self, rid: str, outcome: str, source: str = "telegram") -> bool:
        """Atomically claim the single decision slot for ``rid``. Returns
        False when the request was already resolved (late/duplicate)."""
        if outcome not in VALID_OUTCOMES:
            return False
        self.ensure_dirs()
        final = self.decisions_dir / f"{rid}.json"
        tmp = final.with_name(final.name + f".tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
        _write_json_atomic(tmp, {
            "id": rid,
            "outcome": outcome,
            "source": source,
            "decided_ts": time.time(),
        })
        try:
            os.link(tmp, final)  # atomic create-or-fail: exactly one decision
            return True
        except FileExistsError:
            return False
        except OSError:
            # Filesystem without hard links (unexpected on tmpfs/ext4):
            # degrade to O_EXCL create.
            try:
                with open(final, "x", encoding="utf-8") as handle:
                    handle.write(tmp.read_text(encoding="utf-8"))
                return True
            except FileExistsError:
                return False
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    def get_decision(self, rid: str) -> dict | None:
        return _read_json(self.decisions_dir / f"{rid}.json")

    def wait_decision(self, rid: str, timeout_s: int, poll_s: float = 0.5) -> dict:
        """Hook-side wait (importable by tests; the claude template embeds
        the same loop): block until a decision exists or the timeout runs
        out — in which case the TIMEOUT decision is deposited here, so a
        late remote decision finds the slot taken (fail-safe, design D4)."""
        deadline = time.monotonic() + max(0, timeout_s)
        while time.monotonic() < deadline:
            decision = self.get_decision(rid)
            if decision is not None:
                return decision
            time.sleep(poll_s)
        if self.deposit_decision(rid, "timeout", source="broker-timeout"):
            return {"id": rid, "outcome": "timeout", "source": "broker-timeout"}
        # Lost the race with a real decision landing at the deadline: honor it.
        return self.get_decision(rid) or {"id": rid, "outcome": "timeout", "source": "broker-timeout"}

    # -- dual-control ---------------------------------------------------------------

    def mark_native_resolution(self, rid: str, outcome: str = "") -> None:
        """The harness resolved the request natively (operator in the TUI):
        recorded so the Telegram message can be updated as 'resolved
        elsewhere' (design D4, dual-control)."""
        self.ensure_dirs()
        _write_json_atomic(self.native_dir / f"{rid}.json", {
            "id": rid,
            "outcome": outcome,
            "ts": time.time(),
        })

    def get_native(self, rid: str) -> dict | None:
        return _read_json(self.native_dir / f"{rid}.json")

    # -- housekeeping ---------------------------------------------------------------

    def sweep(self, max_age_s: float = 86400.0) -> None:
        """Best-effort cleanup of stale protocol files (broker dirs live in
        /tmp: bounded by the microVM lifetime anyway)."""
        cutoff = time.time() - max_age_s
        for directory in (self.requests_dir, self.decisions_dir, self.native_dir):
            try:
                entries = list(directory.iterdir())
            except OSError:
                continue
            for entry in entries:
                try:
                    if entry.stat().st_mtime < cutoff:
                        entry.unlink()
                except OSError:
                    pass


def _plain_value(attr: dict):
    if not isinstance(attr, dict):
        return None
    if "S" in attr:
        return attr["S"]
    if "N" in attr:
        try:
            number = float(attr["N"])
            return int(number) if number.is_integer() else number
        except ValueError:
            return None
    return None


def _error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return str(response.get("Error", {}).get("Code", ""))
    return ""


class CommandQueue:
    """Poll + consume of the workspace's DynamoDB command queue (design D3)."""

    def __init__(self, client, table: str, workspace_resolver, now=time.time):
        self._client = client
        self._table = table
        self._workspace = workspace_resolver
        self._now = now

    def poll(self, limit: int = 10) -> list[dict]:
        """Consume up to ``limit`` commands. Each returned command was
        successfully claimed by a conditional Delete (exactly-once): a lost
        race is skipped silently. Commands past expiry carry
        ``expired=True`` (the caller notifies the discard)."""
        workspace = self._workspace()
        if not workspace:
            return []
        response = self._client.query(
            TableName=self._table,
            KeyConditionExpression="workspace = :ws",
            ExpressionAttributeValues={":ws": {"S": workspace}},
            ConsistentRead=True,
            Limit=limit,
        )
        consumed = []
        for item in response.get("Items", []):
            sk = _plain_value(item.get("sk", {}))
            if not sk:
                continue
            try:
                self._client.delete_item(
                    TableName=self._table,
                    Key={"workspace": {"S": workspace}, "sk": {"S": sk}},
                    ConditionExpression="attribute_exists(sk)",
                )
            except Exception as exc:  # noqa: BLE001
                if _error_code(exc) == "ConditionalCheckFailedException":
                    continue  # someone else consumed it: exactly-once holds
                raise
            command = {key: _plain_value(value) for key, value in item.items()}
            expires_at = command.get("expiresAt")
            command["expired"] = bool(
                isinstance(expires_at, (int, float)) and self._now() >= expires_at
            )
            consumed.append(command)
        return consumed


# -- Telegram message texts (plain text, same policy as the notifier) ----------

_DECISION_SUFFIX = {
    "approve": "\n\n✅ approved via Telegram",
    "deny": "\n\n🚫 denied via Telegram",
    "timeout": "\n\n⏱️ no decision within the timeout — the harness decides (native behavior)",
    "fallback": "\n\n↩️ resolved elsewhere — the prompt returns to the harness",
}
# add-pi-harness (design D7, task 7.2): pi has no native permission prompt, so
# "standing aside" on timeout would mean EXECUTING. Its extension therefore
# denies the tool, and the message must say so rather than claiming the harness
# decided natively.
_NATIVE_SUFFIX = "\n\n↩️ resolved by the TUI/harness"


def _decision_suffix(outcome: str, source: str) -> str | None:
    """Message suffix for a resolved request."""
    return _DECISION_SUFFIX.get(outcome)


class InteractionManager:
    """Notifier-thread companion: command poll + broker/message sync.

    Injected callables keep this testable container-free:
    - ``notify(etype, payload, workspace)``  — outbound events (the notifier);
    - ``gate()``                              — True while the poll must run
      (active interactive session / running task / live backend, design D3);
    - ``dispatch_text(text)``                 — the shim's D5 dispatcher.
    """

    def __init__(
        self,
        *,
        queue: CommandQueue,
        broker: ApprovalBroker,
        client,
        chat_id: str,
        notify,
        gate,
        dispatch_text,
        poll_interval: float = 3.0,
        now=time.monotonic,
    ):
        self._queue = queue
        self._broker = broker
        self._client = client
        self._chat_id = chat_id
        self._notify = notify
        self._gate = gate
        self._dispatch_text = dispatch_text
        self._poll_interval = poll_interval
        self._now = now
        self._last_poll = 0.0
        # rid -> {"message_id", "thread_id", "workspace", "base_text", "shown"}
        self._tracked: dict[str, dict] = {}

    # -- notifier integration (permission-request publishing) ----------------

    def reply_markup_for(self, event: dict) -> dict | None:
        rid = (event.get("payload") or {}).get("request_id")
        if not rid:
            return None
        return {"inline_keyboard": [[
            {"text": "✅ Approve", "callback_data": f"{rid}:approve"},
            {"text": "🚫 Deny", "callback_data": f"{rid}:deny"},
        ]]}

    def on_permission_published(self, event: dict, result: dict, workspace: str, text: str) -> None:
        """Called by the notifier right after sendMessage succeeded for a
        permission-request event: remember the message so the outcome can
        be edited in later (task 3.2)."""
        rid = (event.get("payload") or {}).get("request_id")
        message_id = (result or {}).get("message_id")
        if not rid or not isinstance(message_id, int):
            return
        self._tracked[rid] = {
            "message_id": message_id,
            "workspace": workspace,
            "base_text": text,
            "shown": None,
            "first_seen": self._now(),
            # add-pi-harness (task 7.2): the outcome wording depends on which
            # harness raised the request. The event carries it; the broker
            # request record is the fallback for a restarted shim.
            "source": str(event.get("source") or ""),
        }

    def fallback_permission_request(self, event: dict) -> bool:
        """Claim one request's decision slot for native approval."""
        rid = str((event.get("payload") or {}).get("request_id") or "")
        if not rid or not self._broker.has_request(rid):
            return False
        request = self._broker.get_request(rid) or {}
        if request.get("source") not in ("opencode", "claude"):
            return False
        if self._broker.deposit_decision(rid, "fallback", source="presence-attached"):
            return True
        return self._broker.get_decision(rid) is not None

    def permission_request_resolved(self, event: dict) -> bool:
        rid = str((event.get("payload") or {}).get("request_id") or "")
        return bool(rid and self._broker.get_decision(rid) is not None)

    def resolve_pending_remote_approvals(self) -> list[str]:
        """Transfer all pending OpenCode/Claude requests back to the TUI."""
        return self._broker.resolve_pending_as_fallback()

    # -- main tick (called from the notifier loop, never raises) ---------------

    def tick(self) -> None:
        try:
            self._watch_resolutions()
        except Exception as exc:  # noqa: BLE001
            logger.warning("approval message sync failed: %s", exc)
        try:
            if not self._gate():
                return
            now = self._now()
            if now - self._last_poll < self._poll_interval:
                return
            self._last_poll = now
            commands = self._queue.poll()
        except Exception as exc:  # noqa: BLE001 — design D7: never fatal
            logger.warning("telegram command poll failed: %s", exc)
            return
        for command in commands:
            try:
                self._handle(command)
            except Exception as exc:  # noqa: BLE001
                logger.warning("telegram command handling failed: %s", exc)

    # -- command handling --------------------------------------------------------

    def _handle(self, command: dict) -> None:
        logger.info(
            "consuming telegram command type=%s sk=%s expired=%s",
            command.get("type"), command.get("sk"), command.get("expired"),
        )
        if command.get("expired"):
            preview = str(command.get("text") or command.get("requestId") or "")[:120]
            self._notify("command-expired", {"preview": preview}, None)
            return
        ctype = command.get("type")
        if ctype == "decision":
            self._apply_decision(command)
        elif ctype == "text":
            self._dispatch_text(str(command.get("text") or ""))
        else:
            logger.warning("unknown telegram command type: %r", ctype)

    def _answer_callback(self, callback_query_id, text: str) -> None:
        if not callback_query_id:
            return
        try:
            self._client.answer_callback_query(str(callback_query_id), text)
        except Exception as exc:  # noqa: BLE001 — stale callback ids are normal
            logger.info("answerCallbackQuery failed (probably stale): %s", exc)

    def _apply_decision(self, command: dict) -> None:
        rid = str(command.get("requestId") or "")
        decision = str(command.get("decision") or "")
        callback_id = command.get("callbackQueryId")
        if decision not in ("approve", "deny") or not rid:
            return
        if not self._broker.has_request(rid):
            self._answer_callback(callback_id, "Unknown or expired request.")
            self._notify("interaction", {
                "text": "The received decision refers to an unknown or expired request: no effect.",
            }, None)
            return
        if self._broker.deposit_decision(rid, decision, source="telegram"):
            self._answer_callback(
                callback_id,
                "✅ Approved" if decision == "approve" else "🚫 Denied",
            )
            return
        # Late or duplicate (design D4): no effect on the harness, feedback in
        # the topic + on the button spinner.
        existing = self._broker.get_decision(rid) or {}
        outcome = existing.get("outcome", "unknown")
        self._answer_callback(callback_id, f"Already resolved ({outcome}).")
        self._notify("interaction", {
            "text": f"The approval request was already resolved (outcome: {outcome}); the decision was ignored.",
        }, None)

    # -- approval message sync (tasks 3.2/3.5) ------------------------------------

    def _edit_tracked(self, info: dict, suffix: str) -> bool:
        try:
            self._client.edit_message_text(
                self._chat_id,
                info["message_id"],
                info["base_text"] + suffix,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("editMessageText failed: %s", exc)
            return False

    def _watch_resolutions(self) -> None:
        for rid, info in list(self._tracked.items()):
            decision = self._broker.get_decision(rid)
            native = self._broker.get_native(rid)
            if native is not None and info.get("shown") != "native":
                # Dual-control: a native resolution supersedes whatever was
                # shown (typically the timeout note) — final state.
                outcome = str(native.get("outcome") or "").strip()
                suffix = _NATIVE_SUFFIX + (f" (outcome: {outcome})" if outcome else "")
                if self._edit_tracked(info, suffix):
                    info["shown"] = "native"
                    del self._tracked[rid]
                continue
            if decision is None:
                # Nothing yet; forget requests tracked for over an hour.
                if self._now() - info.get("first_seen", 0) > 3600:
                    del self._tracked[rid]
                continue
            outcome = decision.get("outcome")
            source = info.get("source") or (self._broker.get_request(rid) or {}).get("source") or ""
            suffix = _decision_suffix(str(outcome), str(source))
            if info.get("shown") == outcome or suffix is None:
                continue
            if self._edit_tracked(info, suffix):
                info["shown"] = outcome
                if outcome in ("approve", "deny", "fallback"):
                    del self._tracked[rid]
                # timeout on opencode/claude: keep tracking — a later native
                # resolution (the operator answered the harness prompt) still
                # gets reflected.
