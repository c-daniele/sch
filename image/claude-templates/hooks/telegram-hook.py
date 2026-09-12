#!/usr/bin/env python3
"""SCH Telegram hook for Claude Code.

Milestones (add-telegram-notifications): registered by init-workspace.sh in
the seeded settings.json for `Stop`, `Notification` and `PostToolUse`
(matcher TodoWrite). Ultra-thin by design (design D3): it reads the hook
payload from stdin, writes ONE event JSON file into the local spool dir
consumed by the shim's notifier thread, and exits 0. It NEVER talks to the
network and is an immediate no-op when the shim's local notifier marker is
absent.

In-turn milestones (add-task-liveness-safety, design D4): the `tool` kind is
registered on `PostToolUse` with a broad matcher and emits one digest-class
event per tool call — the in-turn silence of a headless claude turn was
indistinguishable from a dead microVM. It returns nothing for `TodoWrite`,
which the `todo` kind already covers on the same tool call.

Remote approval (add-telegram-interaction, task 3.3): the `pretooluse` kind
is DECISIONAL when the inbound channel is configured (Telegram env AND
SCH_TELEGRAM_COMMANDS_TABLE): it registers a request in the local approval
broker (requests/<id>.json), emits the permission-request milestone with the
request id (the notifier attaches the Approve/Deny inline keyboard), then
waits for decisions/<id>.json with a bounded timeout BELOW the hook timeout
configured in settings.json. Outcomes (design D4):

  approve -> permissionDecision allow ("approved via Telegram")
  deny    -> permissionDecision deny  ("denied via Telegram")
  timeout -> NO output: the native permission flow stays fully in charge
             (fail-safe — never auto-approval); the timeout decision is
             deposited atomically so a late remote decision finds the slot
             taken.

The `native` kind (PostToolUse on the same matcher) closes the dual-control
loop (task 3.5): a tool that RAN after its request timed out was approved
natively — mark it so the shim updates the Telegram message as "resolved
elsewhere".

Never-fail contract: any error exits 0 silently — a broken integration must
not degrade the harness turn (claude hooks are blocking).

Usage (wired by settings.json):
  telegram-hook.py <stop|notification|todo|tool|pretooluse|native>
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid


SPOOL_DIR = os.environ.get("SCH_TELEGRAM_SPOOL_DIR", "/tmp/sch-telegram-spool")
APPROVAL_DIR = os.environ.get("SCH_APPROVAL_DIR", "/tmp/sch-approval")
ENABLED_MARKER = os.environ.get(
    "SCH_TELEGRAM_ENABLED_MARKER", "/tmp/sch-telegram-enabled"
)
PRESENCE_SNAPSHOT = os.environ.get(
    "SCH_COMMAND_SHELL_PRESENCE_FILE", "/tmp/sch-command-shell-presence.json"
)
EXECUTION_MODE = (
    "headless" if os.environ.get("SCH_EXECUTION_MODE") == "headless" else "interactive"
)
PRESENCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
# OQ-I1: remote wait default 10 min — settings.json sets the hook timeout to
# 660s so claude never kills the hook before the fail-safe path runs.
APPROVAL_TIMEOUT_S = int(os.environ.get("SCH_APPROVAL_TIMEOUT_S", "600"))
DECISION_POLL_S = 0.5
# add-task-liveness-safety (design D4): in-turn `tool` milestones are a
# liveness signal, not a transcript — the target is hard-truncated.
TOOL_SUMMARY_MAX_CHARS = 120


def _last_assistant_text(transcript_path: str) -> str:
    """Final assistant message text from the session transcript JSONL."""
    text = ""
    with open(transcript_path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("type") != "assistant":
                continue
            message = entry.get("message") or {}
            content = message.get("content")
            if isinstance(content, str):
                candidate = content
            elif isinstance(content, list):
                candidate = "\n".join(
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                )
            else:
                candidate = ""
            if candidate.strip():
                text = candidate.strip()
    return text


def _todo_summary(tool_input: dict) -> str:
    todos = tool_input.get("todos") or []
    icons = {"completed": "✔", "in_progress": "▸", "pending": "·"}
    lines = []
    for todo in todos:
        if not isinstance(todo, dict):
            continue
        status = todo.get("status", "pending")
        lines.append(f"{icons.get(status, '·')} {todo.get('content', '')}")
    return "\n".join(lines)


def _tool_summary(tool_name: str, tool_input: dict) -> str:
    """Compact one-line milestone target (design D4).

    Bash -> first non-empty line of the command; path-bearing tools
    (Write/Edit/Read/NotebookEdit/...) -> the path; anything else -> the tool
    name alone. Hard-truncated: this text is a liveness heartbeat, not a log.
    """
    detail = ""
    if not isinstance(tool_input, dict):
        tool_input = {}
    if tool_name == "Bash":
        command = tool_input.get("command")
        if isinstance(command, str):
            for line in command.splitlines():
                if line.strip():
                    detail = line.strip()
                    break
    else:
        for key in ("file_path", "path", "notebook_path"):
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                detail = value.strip()
                break
    if not detail:
        return tool_name
    if len(detail) > TOOL_SUMMARY_MAX_CHARS:
        detail = detail[: TOOL_SUMMARY_MAX_CHARS - 1] + "…"
    return f"{tool_name}: {detail}"


def _build_event(kind: str, hook_input: dict) -> dict | None:
    if kind == "stop":
        transcript = hook_input.get("transcript_path") or ""
        text = ""
        if transcript and os.path.isfile(transcript):
            text = _last_assistant_text(transcript)
        return {"type": "turn-end", "payload": {"text": text}}
    if kind == "notification":
        message = (hook_input.get("message") or "").strip()
        if "permission" in message.lower():
            return {"type": "permission-request", "payload": {"tool": message}}
        return {"type": "await-input", "payload": {"message": message}}
    if kind == "todo":
        summary = _todo_summary(hook_input.get("tool_input") or {})
        if not summary:
            return None
        return {"type": "todo", "payload": {"summary": summary}}
    if kind == "tool":
        # add-task-liveness-safety (task 4.2, design D4): in-turn milestone
        # for EVERY tool call, so a long single-turn headless run stops being
        # indistinguishable from a dead microVM. The notifier already maps
        # `tool` to the digest priority class, so the coalescing window and
        # the per-topic min interval are the flood control.
        tool_name = (hook_input.get("tool_name") or "").strip()
        if not tool_name:
            return None
        if tool_name == "TodoWrite":
            # Already covered by the `todo` kind on the same tool call: a
            # second event here would double every todo message (design D4).
            return None
        return {
            "type": "tool",
            "payload": {"summary": _tool_summary(tool_name, hook_input.get("tool_input") or {})},
        }
    return None


# --- Remote approval broker protocol (add-telegram-interaction) ----------------
# Mirror of image/app/telegram_interaction.ApprovalBroker — kept inline so the
# hook stays a single dependency-free file.

def _read_json(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _write_json_atomic(path: str, payload: dict) -> None:
    tmp = f"{path}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    os.replace(tmp, path)


def _deposit_decision(rid: str, outcome: str, source: str) -> bool:
    """Atomically claim the single decision slot; False when already decided."""
    decisions = os.path.join(APPROVAL_DIR, "decisions")
    os.makedirs(decisions, exist_ok=True)
    final = os.path.join(decisions, f"{rid}.json")
    tmp = f"{final}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    _write_json_atomic(tmp, {
        "id": rid, "outcome": outcome, "source": source, "decided_ts": time.time(),
    })
    try:
        os.link(tmp, final)
        return True
    except FileExistsError:
        return False
    except OSError:
        try:
            with open(final, "x", encoding="utf-8") as handle:
                with open(tmp, "r", encoding="utf-8") as src:
                    handle.write(src.read())
            return True
        except FileExistsError:
            return False
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _interaction_enabled() -> bool:
    marker = _read_json(ENABLED_MARKER)
    return bool(marker and marker.get("interaction_enabled") is True)


def _locally_attached() -> bool:
    if EXECUTION_MODE == "headless":
        return False
    snapshot = _read_json(PRESENCE_SNAPSHOT)
    if (
        not snapshot
        or snapshot.get("version") != 1
        or snapshot.get("state") != "attached"
        or not isinstance(snapshot.get("leases"), list)
        or not snapshot["leases"]
    ):
        return False
    now = time.time()
    expires_at = snapshot.get("expires_at")
    if not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool):
        return False
    valid_expiries = []
    for lease in snapshot["leases"]:
        if not isinstance(lease, dict):
            return False
        shell_id = lease.get("shellId")
        attachment_id = lease.get("attachmentId")
        attached_at = lease.get("attached_at")
        expiry = lease.get("expires_at")
        if (
            not isinstance(shell_id, str)
            or not PRESENCE_ID_RE.fullmatch(shell_id)
            or not isinstance(attachment_id, str)
            or not PRESENCE_ID_RE.fullmatch(attachment_id)
            or not isinstance(attached_at, (int, float))
            or isinstance(attached_at, bool)
            or not isinstance(expiry, (int, float))
            or isinstance(expiry, bool)
            or not 0 <= attached_at < expiry
        ):
            return False
        valid_expiries.append(float(expiry))
    return float(expires_at) == max(valid_expiries) and any(
        expiry > now for expiry in valid_expiries
    )


def _tool_detail(tool_name: str, tool_input: dict) -> str:
    if not isinstance(tool_input, dict):
        return ""
    if tool_name == "Bash":
        return (tool_input.get("command") or "")[:300]
    for key in ("file_path", "path", "url", "notebook_path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value[:300]
    return ""


def _decide_pretooluse(hook_input: dict, wait_timeout_s: int = None) -> dict | None:
    """Decisional PreToolUse (design D4). Returns the hook JSON output for a
    remote allow/deny, or None to leave the native permission flow fully in
    charge (timeout, disabled channel, bypassed permissions)."""
    if not _interaction_enabled():
        return None
    if EXECUTION_MODE == "headless":
        return None
    if _locally_attached():
        return None
    # Headless tasks also run with --dangerously-skip-permissions: no human would
    # be waited for, so the remote wait must not exist either (design D4:
    # engage only where the harness would have awaited a human).
    if hook_input.get("permission_mode") == "bypassPermissions":
        return None
    tool_name = hook_input.get("tool_name") or "?"
    timeout_s = APPROVAL_TIMEOUT_S if wait_timeout_s is None else wait_timeout_s
    rid = uuid.uuid4().hex[:16]
    requests_dir = os.path.join(APPROVAL_DIR, "requests")
    os.makedirs(requests_dir, exist_ok=True)
    _write_json_atomic(os.path.join(requests_dir, f"{rid}.json"), {
        "id": rid,
        "tool": tool_name,
        "detail": _tool_detail(tool_name, hook_input.get("tool_input") or {}),
        "timeout_s": timeout_s,
        "source": "claude",
        "execution_mode": EXECUTION_MODE,
        "created_ts": time.time(),
    })
    _spool({
        "type": "permission-request",
        "payload": {
            "request_id": rid,
            "tool": tool_name,
            "detail": _tool_detail(tool_name, hook_input.get("tool_input") or {}),
            "timeout_s": timeout_s,
        },
    })
    decision_path = os.path.join(APPROVAL_DIR, "decisions", f"{rid}.json")
    deadline = time.monotonic() + timeout_s
    decision = None
    while time.monotonic() < deadline:
        decision = _read_json(decision_path)
        if decision is not None:
            break
        time.sleep(DECISION_POLL_S)
    if decision is None:
        # Fail-safe (design D4): deposit the timeout so a late remote
        # decision has no effect; return control to the native flow.
        if not _deposit_decision(rid, "timeout", "hook-timeout"):
            decision = _read_json(decision_path)  # lost the race: honor it
        if decision is None:
            return None
    outcome = decision.get("outcome")
    if outcome == "approve":
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": "approved via Telegram",
        }}
    if outcome == "deny":
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "denied via Telegram",
        }}
    return None  # timeout: native behavior, never auto-approval


def _mark_native_resolutions(hook_input: dict) -> None:
    """PostToolUse dual-control closure (task 3.5): the tool RAN after its
    request timed out -> the operator approved it natively; record the
    marker so the Telegram message is updated as 'resolved elsewhere'."""
    if not _interaction_enabled():
        return
    tool_name = hook_input.get("tool_name") or ""
    requests_dir = os.path.join(APPROVAL_DIR, "requests")
    native_dir = os.path.join(APPROVAL_DIR, "native")
    try:
        entries = sorted(os.listdir(requests_dir))
    except OSError:
        return
    for name in entries:
        if not name.endswith(".json") or ".tmp-" in name:
            continue
        rid = name[: -len(".json")]
        request = _read_json(os.path.join(requests_dir, name)) or {}
        if request.get("source") != "claude" or request.get("tool") != tool_name:
            continue
        decision = _read_json(os.path.join(APPROVAL_DIR, "decisions", f"{rid}.json"))
        if not decision or decision.get("outcome") != "timeout":
            continue
        native_path = os.path.join(native_dir, f"{rid}.json")
        if os.path.exists(native_path):
            continue
        os.makedirs(native_dir, exist_ok=True)
        _write_json_atomic(native_path, {
            "id": rid, "outcome": "allow", "ts": time.time(),
        })


def _spool(event: dict) -> None:
    event.setdefault("ts", time.time())
    event.setdefault("source", "claude")
    event.setdefault("execution_mode", EXECUTION_MODE)
    # tmp-then-rename so the notifier's *.json poll never reads a partial file.
    os.makedirs(SPOOL_DIR, exist_ok=True)
    final = os.path.join(SPOOL_DIR, f"{time.time_ns()}-{os.getpid()}.json")
    tmp = final + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(event, handle, ensure_ascii=False)
    os.replace(tmp, final)


def main() -> int:
    # Opt-in gate FIRST: without the local notifier marker this hook must be an
    # immediate silent no-op (no spool file, no error, no reads).
    if not os.path.isfile(ENABLED_MARKER):
        return 0
    kind = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        hook_input = json.load(sys.stdin)
        if not isinstance(hook_input, dict):
            hook_input = {}
    except Exception:  # noqa: BLE001
        hook_input = {}
    if kind == "pretooluse":
        output = _decide_pretooluse(hook_input)
        if output is not None:
            print(json.dumps(output))
        return 0
    if kind == "native":
        _mark_native_resolutions(hook_input)
        return 0
    event = _build_event(kind, hook_input)
    if event is None:
        return 0
    _spool(event)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 — never degrade the harness turn
        sys.exit(0)
