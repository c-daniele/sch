"""Tests for the DECISIONAL claude PreToolUse hook (add-telegram-interaction,
task 5.2): allow/deny/timeout via the broker files, wait below the harness
hook timeout, complete no-op without the inbound channel, dual-control
native marker."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
HOOK_SCRIPT = ROOT / "claude-templates" / "hooks" / "telegram-hook.py"

_SPEC = importlib.util.spec_from_file_location("sch_claude_hook", HOOK_SCRIPT)
hook = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = hook
_SPEC.loader.exec_module(hook)


class HookConstantsTests(unittest.TestCase):
    def test_default_wait_is_below_the_harness_hook_timeout(self):
        # settings.json registers the hook with timeout 660 (init-workspace.sh)
        # — the remote wait must stay strictly below it (design D4).
        self.assertLess(hook.APPROVAL_TIMEOUT_S, 660)


class PreToolUseHookTests(unittest.TestCase):
    """End-to-end via subprocess: the hook is a separate process in real
    life, and the broker protocol must work across processes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.approval = Path(self.tmp.name) / "approval"
        self.spool = Path(self.tmp.name) / "spool"
        self.marker = Path(self.tmp.name) / "telegram-enabled"
        self.presence = Path(self.tmp.name) / "presence.json"

    def _env(self, enabled=True, timeout="10"):
        env = os.environ.copy()
        env.update({
            "SCH_TELEGRAM_SPOOL_DIR": str(self.spool),
            "SCH_APPROVAL_DIR": str(self.approval),
            "SCH_APPROVAL_TIMEOUT_S": timeout,
            "SCH_TELEGRAM_ENABLED_MARKER": str(self.marker),
            "SCH_COMMAND_SHELL_PRESENCE_FILE": str(self.presence),
        })
        if enabled:
            self.marker.write_text(json.dumps({"interaction_enabled": True}))
        else:
            self.marker.unlink(missing_ok=True)
        env.pop("SCH_TELEGRAM_BOT_TOKEN", None)
        env.pop("SCH_TELEGRAM_CHAT_ID", None)
        env.pop("SCH_TELEGRAM_COMMANDS_TABLE", None)
        return env

    def _run_hook(self, kind, payload, env):
        return subprocess.run(
            ["python3", str(HOOK_SCRIPT), kind],
            input=json.dumps(payload), env=env, text=True, capture_output=True,
        )

    def _decide_when_requested(self, outcome):
        """Background thread playing the remote side: waits for the request
        file, then deposits the decision."""

        def _worker():
            requests = self.approval / "requests"
            deadline = time.monotonic() + 8
            rid = None
            while time.monotonic() < deadline and rid is None:
                if requests.is_dir():
                    files = [p for p in requests.iterdir() if p.suffix == ".json"]
                    if files:
                        rid = files[0].stem
                        break
                time.sleep(0.05)
            if rid is None:
                return
            decisions = self.approval / "decisions"
            decisions.mkdir(parents=True, exist_ok=True)
            (decisions / f"{rid}.json").write_text(json.dumps({
                "id": rid, "outcome": outcome, "source": "telegram",
            }))

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()
        return thread

    def spool_events(self):
        if not self.spool.is_dir():
            return []
        return [json.loads(p.read_text()) for p in sorted(self.spool.iterdir())]

    def test_approve_decision_allows_the_tool(self):
        decider = self._decide_when_requested("approve")
        result = self._run_hook(
            "pretooluse",
            {"tool_name": "Bash", "tool_input": {"command": "ls"}},
            self._env(),
        )
        decider.join(10)
        self.assertEqual(result.returncode, 0)
        output = json.loads(result.stdout)
        self.assertEqual(
            output["hookSpecificOutput"]["permissionDecision"], "allow")
        self.assertIn("Telegram", output["hookSpecificOutput"]["permissionDecisionReason"])
        # The permission-request milestone carried the request id + detail.
        events = self.spool_events()
        self.assertEqual(events[0]["type"], "permission-request")
        self.assertTrue(events[0]["payload"]["request_id"])
        self.assertEqual(events[0]["payload"]["detail"], "ls")

    def test_deny_decision_denies_the_tool(self):
        decider = self._decide_when_requested("deny")
        result = self._run_hook(
            "pretooluse", {"tool_name": "Write", "tool_input": {"file_path": "/x"}},
            self._env(),
        )
        decider.join(10)
        output = json.loads(result.stdout)
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_timeout_returns_native_control_and_claims_the_slot(self):
        result = self._run_hook(
            "pretooluse", {"tool_name": "Bash", "tool_input": {"command": "ls"}},
            self._env(timeout="0"),
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")  # no decision: native behavior
        decisions = list((self.approval / "decisions").iterdir())
        self.assertEqual(len(decisions), 1)
        self.assertEqual(json.loads(decisions[0].read_text())["outcome"], "timeout")

    def test_noop_without_inbound_channel(self):
        result = self._run_hook(
            "pretooluse", {"tool_name": "Bash", "tool_input": {"command": "ls"}},
            self._env(enabled=False),
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertFalse(self.approval.exists())
        self.assertEqual(self.spool_events(), [])

    def test_bypass_permissions_never_waits(self):
        started = time.monotonic()
        result = self._run_hook(
            "pretooluse",
            {"tool_name": "Bash", "tool_input": {"command": "ls"},
             "permission_mode": "bypassPermissions"},
            self._env(timeout="30"),
        )
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(result.stdout, "")
        self.assertFalse(self.approval.exists())

    def test_attached_snapshot_returns_immediately_to_native_approval(self):
        expires_at = time.time() + 60
        self.presence.write_text(json.dumps({
            "version": 1, "state": "attached", "expires_at": expires_at,
            "leases": [{
                "shellId": "shell-1", "attachmentId": "client-1",
                "attached_at": time.time(), "expires_at": expires_at,
            }],
        }))
        started = time.monotonic()
        result = self._run_hook(
            "pretooluse", {"tool_name": "Bash", "tool_input": {"command": "ls"}},
            self._env(timeout="30"),
        )
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(result.stdout, "")
        self.assertFalse(self.approval.exists())
        self.assertEqual(self.spool_events(), [])

    def test_headless_never_creates_remote_approval(self):
        expires_at = time.time() + 60
        self.presence.write_text(json.dumps({
            "version": 1, "state": "attached", "expires_at": expires_at,
            "leases": [{
                "shellId": "shell-1", "attachmentId": "client-1",
                "attached_at": time.time(), "expires_at": expires_at,
            }],
        }))
        env = self._env(timeout="0")
        env["SCH_EXECUTION_MODE"] = "headless"
        result = self._run_hook(
            "pretooluse", {"tool_name": "Bash", "tool_input": {"command": "ls"}}, env,
        )
        self.assertEqual(result.returncode, 0)
        self.assertFalse(self.approval.exists())

    def test_malformed_attached_snapshot_fails_open_to_remote_approval(self):
        expires_at = time.time() + 60
        self.presence.write_text(json.dumps({
            "version": 1, "state": "attached", "expires_at": expires_at,
            "leases": [{
                "shellId": "bad shell", "attachmentId": "client-1",
                "attached_at": time.time(), "expires_at": expires_at,
            }],
        }))
        result = self._run_hook(
            "pretooluse", {"tool_name": "Bash", "tool_input": {"command": "ls"}},
            self._env(timeout="0"),
        )
        self.assertEqual(result.stdout, "")
        self.assertTrue(self.approval.exists())
        self.assertEqual(len(self.spool_events()), 1)

    def test_fallback_decision_returns_to_native_without_waiting_for_timeout(self):
        decider = self._decide_when_requested("fallback")
        started = time.monotonic()
        result = self._run_hook(
            "pretooluse", {"tool_name": "Bash", "tool_input": {"command": "ls"}},
            self._env(timeout="30"),
        )
        decider.join(10)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_native_kind_marks_timed_out_request_as_resolved_elsewhere(self):
        # A request that timed out, then the tool ran (native approval).
        env = self._env()
        (self.approval / "requests").mkdir(parents=True)
        (self.approval / "decisions").mkdir(parents=True)
        (self.approval / "requests" / "r1.json").write_text(json.dumps({
            "id": "r1", "tool": "Bash", "source": "claude", "created_ts": time.time(),
        }))
        (self.approval / "decisions" / "r1.json").write_text(json.dumps({
            "id": "r1", "outcome": "timeout", "source": "hook-timeout",
        }))
        result = self._run_hook("native", {"tool_name": "Bash"}, env)
        self.assertEqual(result.returncode, 0)
        native = json.loads((self.approval / "native" / "r1.json").read_text())
        self.assertEqual(native["outcome"], "allow")

    def test_native_kind_ignores_remotely_decided_requests(self):
        env = self._env()
        (self.approval / "requests").mkdir(parents=True)
        (self.approval / "decisions").mkdir(parents=True)
        (self.approval / "requests" / "r2.json").write_text(json.dumps({
            "id": "r2", "tool": "Bash", "source": "claude", "created_ts": time.time(),
        }))
        (self.approval / "decisions" / "r2.json").write_text(json.dumps({
            "id": "r2", "outcome": "approve", "source": "telegram",
        }))
        self._run_hook("native", {"tool_name": "Bash"}, env)
        self.assertFalse((self.approval / "native" / "r2.json").exists())


if __name__ == "__main__":
    unittest.main()
