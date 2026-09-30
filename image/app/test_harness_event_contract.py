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
        # Fake V2 plugin context: an event stream that yields two events then
        # ends, plus recording tool/permission hook registries.
        script = textwrap.dedent(f"""
            import {{ pathToFileURL }} from "node:url";
            const mod = await import(pathToFileURL({str(OPENCODE_PLUGIN)!r}));
            const events = [
              {{type: "session.text.ended", data: {{sessionID: "s1", text: "finished"}}}},
              {{type: "session.idle", data: {{sessionID: "s1"}}}},
            ];
            let done;
            const finished = new Promise((resolve) => {{ done = resolve; }});
            const ctx = {{
              event: {{ subscribe: async function* () {{ for (const e of events) yield e; done(); }} }},
              tool: {{ hook: async () => ({{ dispose: async () => {{}} }}) }},
              permission: {{ hook: async () => ({{ dispose: async () => {{}} }}) }},
              session: {{ context: async () => [] }},
            }};
            const cleanup = await mod.default.setup(ctx);
            await finished;
            await new Promise((r) => setTimeout(r, 50));
            cleanup();
            console.log(JSON.stringify({{id: mod.default.id}}));
        """)
        disabled = self._run_node(script, self._env(enabled=False))
        self.assertEqual(disabled.returncode, 0, disabled.stderr)
        self.assertEqual(json.loads(disabled.stdout), {"id": "sch-telegram"})
        self.assertEqual(self._events(), [])

        enabled = self._run_node(script, self._env(enabled=True))
        self.assertEqual(enabled.returncode, 0, enabled.stderr)
        event = self._events()[0]
        self.assertEqual(event["type"], "turn-end")
        self.assertEqual(event["payload"]["text"], "finished")
        self.assertEqual(event["source"], "opencode")
        self.assertEqual(event["execution_mode"], "interactive")
        self.assertIsInstance(event["ts"], (int, float))

    def test_opencode_turn_end_falls_back_to_session_context(self):
        # No streamed text: the plugin asks the typed client for the last
        # assistant text instead of relying on the stream shape.
        script = textwrap.dedent(f"""
            import {{ pathToFileURL }} from "node:url";
            const mod = await import(pathToFileURL({str(OPENCODE_PLUGIN)!r}));
            let done;
            const finished = new Promise((resolve) => {{ done = resolve; }});
            const ctx = {{
              event: {{ subscribe: async function* () {{
                yield {{type: "session.idle", data: {{sessionID: "s1"}}}}; done();
              }} }},
              tool: {{ hook: async () => ({{}}) }},
              permission: {{ hook: async () => ({{}}) }},
              session: {{ context: async ({{sessionID}}) => ({{ data: [
                {{type: "user", content: []}},
                {{type: "assistant", content: [{{type: "reasoning", text: "hmm"}}, {{type: "text", text: "from " + sessionID}}]}},
                {{type: "idle", outcome: "succeeded"}},
              ]}}) }},
            }};
            const cleanup = await mod.default.setup(ctx);
            await finished;
            await new Promise((r) => setTimeout(r, 50));
            cleanup();
        """)
        result = self._run_node(script, self._env(enabled=True))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._events()[0]["payload"]["text"], "from s1")

    def test_opencode_attached_snapshot_keeps_native_approval(self):
        self.marker.write_text(json.dumps({"interaction_enabled": True}))
        self.presence.write_text(json.dumps({
            "version": 1, "state": "attached", "expires_at": 4102444800,
            "leases": [{
                "shellId": "shell-1", "attachmentId": "client-1",
                "attached_at": 1, "expires_at": 4102444800,
            }],
        }))
        # The V2 `evaluate` permission hook is the only interception point:
        # with an operator attached locally the evaluation must be left as
        # `ask` (native prompt), with nothing spooled and no broker request.
        script = textwrap.dedent(f"""
            import {{ pathToFileURL }} from "node:url";
            const mod = await import(pathToFileURL({str(OPENCODE_PLUGIN)!r}));
            const hooks = {{}};
            const ctx = {{
              event: {{ subscribe: async function* () {{}} }},
              tool: {{ hook: async (name, cb) => {{ hooks["tool." + name] = cb; return {{}}; }} }},
              permission: {{ hook: async (name, cb) => {{ hooks["permission." + name] = cb; return {{}}; }} }},
              session: {{ context: async () => [] }},
            }};
            await mod.default.setup(ctx);
            const evaluation = {{
              sessionID: "s1", action: "shell", resources: ["ls *"],
              metadata: {{command: "ls -la"}}, effect: "ask",
            }};
            await hooks["permission.evaluate"](evaluation);
            console.log(JSON.stringify({{
              registered: Object.keys(hooks).sort(),
              effect: evaluation.effect,
            }}));
        """)
        env = self._env(enabled=True)
        self.marker.write_text(json.dumps({"interaction_enabled": True}))
        result = self._run_node(script, env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {
            "registered": ["permission.evaluate", "tool.execute.after", "tool.execute.before"],
            "effect": "ask",
        })
        self.assertEqual(self._events(), [])
        self.assertFalse(self.approval.exists())

    def test_opencode_remote_decision_resolves_evaluation(self):
        # Unattended: the evaluate hook registers the broker request, spools the
        # milestone with the request id, and a remote approve turns `ask`
        # into `allow` without a native prompt.
        script = textwrap.dedent(f"""
            import {{ pathToFileURL }} from "node:url";
            import fs from "node:fs";
            import path from "node:path";
            const mod = await import(pathToFileURL({str(OPENCODE_PLUGIN)!r}));
            const hooks = {{}};
            const ctx = {{
              event: {{ subscribe: async function* () {{}} }},
              tool: {{ hook: async () => ({{}}) }},
              permission: {{ hook: async (name, cb) => {{ hooks[name] = cb; return {{}}; }} }},
            }};
            await mod.default.setup(ctx);
            const evaluation = {{ sessionID: "s1", action: "edit", resources: ["/repo/a.py"], effect: "ask" }};
            const approvalDir = process.env.SCH_APPROVAL_DIR;
            const decided = hooks.evaluate(evaluation);
            // Remote side: wait for the request file, then deposit approve.
            const requestsDir = path.join(approvalDir, "requests");
            let rid = null;
            for (let i = 0; i < 100 && !rid; i++) {{
              await new Promise((r) => setTimeout(r, 20));
              if (fs.existsSync(requestsDir)) {{
                const files = fs.readdirSync(requestsDir).filter((f) => f.endsWith(".json"));
                if (files.length) rid = files[0].replace(/\\.json$/, "");
              }}
            }}
            fs.mkdirSync(path.join(approvalDir, "decisions"), {{recursive: true}});
            fs.writeFileSync(path.join(approvalDir, "decisions", rid + ".json"),
              JSON.stringify({{id: rid, outcome: "approve", source: "telegram"}}));
            await decided;
            console.log(JSON.stringify({{rid, effect: evaluation.effect}}));
        """)
        env = self._env(enabled=True)
        self.marker.write_text(json.dumps({"interaction_enabled": True}))
        result = self._run_node(script, env)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertEqual(out["effect"], "allow")
        self.assertTrue(out["rid"].startswith("per_"))
        milestone = self._events()[0]
        self.assertEqual(milestone["type"], "permission-request")
        self.assertEqual(milestone["payload"]["request_id"], out["rid"])
        self.assertEqual(milestone["payload"]["tool"], "edit")
        self.assertEqual(milestone["payload"]["detail"], "/repo/a.py")

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
