"""Executable contract tests for the OpenCode and Pi milestone templates."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
OPENCODE_PLUGIN = ROOT / "opencode-templates" / "plugin" / "sch-telegram.js"
PI_EXTENSION = ROOT / "pi-templates" / "extensions" / "sch-pi.ts"


class HarnessEventContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.spool = root / "spool"
        self.approval = root / "approval"
        self.marker = root / "telegram-enabled"
        self.presence = root / "presence.json"

    def _env(self, enabled: bool, execution_mode: str | None = None) -> dict[str, str]:
        env = os.environ.copy()
        env.update({
            "SCH_TELEGRAM_SPOOL_DIR": str(self.spool),
            "SCH_APPROVAL_DIR": str(self.approval),
            "SCH_TELEGRAM_ENABLED_MARKER": str(self.marker),
            "SCH_COMMAND_SHELL_PRESENCE_FILE": str(self.presence),
        })
        if execution_mode is None:
            env.pop("SCH_EXECUTION_MODE", None)
        else:
            env["SCH_EXECUTION_MODE"] = execution_mode
        for name in (
            "SCH_TELEGRAM_BOT_TOKEN",
            "SCH_TELEGRAM_CHAT_ID",
            "SCH_TELEGRAM_COMMANDS_TABLE",
        ):
            env.pop(name, None)
        if enabled:
            self.marker.write_text("{}")
        else:
            self.marker.unlink(missing_ok=True)
        return env

    def _run_node(self, script: str, env: dict[str, str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["node", "--input-type=module", "-e", script],
            env=env,
            text=True,
            capture_output=True,
        )

    def _events(self) -> list[dict]:
        if not self.spool.is_dir():
            return []
        return [json.loads(path.read_text()) for path in sorted(self.spool.glob("*.json"))]

    def test_opencode_marker_gates_collection_and_envelope_defaults_interactive(self):
        script = textwrap.dedent(f"""
            import {{ pathToFileURL }} from "node:url";
            const mod = await import(pathToFileURL({str(OPENCODE_PLUGIN)!r}));
            const hooks = await mod.SchTelegramPlugin();
            await hooks.event({{event: {{type: "message.part.updated", properties: {{
              part: {{type: "text", sessionID: "s1", text: "finished"}}
            }}}}}});
            await hooks.event({{event: {{type: "session.idle", properties: {{sessionID: "s1"}}}}}});
        """)
        disabled = self._run_node(script, self._env(enabled=False))
        self.assertEqual(disabled.returncode, 0, disabled.stderr)
        self.assertEqual(self._events(), [])

        enabled = self._run_node(script, self._env(enabled=True))
        self.assertEqual(enabled.returncode, 0, enabled.stderr)
        event = self._events()[0]
        self.assertEqual(event["source"], "opencode")
        self.assertEqual(event["execution_mode"], "interactive")
        self.assertIsInstance(event["ts"], (int, float))

    def test_opencode_attached_snapshot_keeps_native_approval(self):
        self.marker.write_text(json.dumps({"interaction_enabled": True}))
        self.presence.write_text(json.dumps({
            "version": 1, "state": "attached", "expires_at": 4102444800,
            "leases": [{
                "shellId": "shell-1", "attachmentId": "client-1",
                "attached_at": 1, "expires_at": 4102444800,
            }],
        }))
        script = textwrap.dedent(f"""
            import {{ pathToFileURL }} from "node:url";
            const mod = await import(pathToFileURL({str(OPENCODE_PLUGIN)!r}));
            const replies = [];
            const client = {{permission: {{reply: async (input) => replies.push(input)}}}};
            const hooks = await mod.SchTelegramPlugin({{client}});
            await hooks.event({{event: {{type: "permission.asked", properties: {{
              id: "p1", sessionID: "s1", permission: "bash",
              patterns: ["ls *"], metadata: {{command: "ls -la"}}
            }}}}}});
            console.log(JSON.stringify({{
              hasDeadHook: hooks["permission.ask"] !== undefined,
              replies,
            }}));
        """)
        env = self._env(enabled=True)
        self.marker.write_text(json.dumps({"interaction_enabled": True}))
        result = self._run_node(script, env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"hasDeadHook": False, "replies": []})
        self.assertEqual(self._events(), [])
        self.assertFalse(self.approval.exists())

    def test_pi_headless_envelope_and_no_approval_interception(self):
        script = textwrap.dedent(f"""
            import {{ pathToFileURL }} from "node:url";
            const mod = await import(pathToFileURL({str(PI_EXTENSION)!r}));
            const handlers = new Map();
            mod.default({{on: (name, callback) => handlers.set(name, callback)}});
            await handlers.get("turn_end")({{message: {{content: [{{type: "text", text: "done"}}]}}}}, {{}});
            await handlers.get("agent_settled")({{}}, {{}});
            console.log(JSON.stringify([...handlers.keys()]));
        """)
        result = self._run_node(script, self._env(enabled=True, execution_mode="headless"))
        self.assertEqual(result.returncode, 0, result.stderr)
        handlers = json.loads(result.stdout)
        self.assertNotIn("permission", handlers)
        self.assertNotIn("tool_call", handlers)
        event = self._events()[0]
        self.assertEqual(event["source"], "pi")
        self.assertEqual(event["execution_mode"], "headless")
        self.assertEqual(event["payload"]["text"], "done")

    def test_pi_without_marker_does_not_spool(self):
        script = textwrap.dedent(f"""
            import {{ pathToFileURL }} from "node:url";
            const mod = await import(pathToFileURL({str(PI_EXTENSION)!r}));
            const handlers = new Map();
            mod.default({{on: (name, callback) => handlers.set(name, callback)}});
            await handlers.get("turn_end")({{message: {{content: [{{type: "text", text: "done"}}]}}}}, {{}});
            await handlers.get("agent_settled")({{}}, {{}});
        """)
        result = self._run_node(script, self._env(enabled=False))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._events(), [])


if __name__ == "__main__":
    unittest.main()
