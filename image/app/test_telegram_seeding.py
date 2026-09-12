"""Container-free tests for the Telegram hook/plugin seeding
(add-telegram-notifications, task 6.3): additive settings.json merge with
pre-existing operator hooks, opencode plugin idempotence, hook no-op without
Telegram config."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "init-workspace.sh"
CLAUDE_TEMPLATES = ROOT / "claude-templates"
OPENCODE_TEMPLATES = ROOT / "opencode-templates"
HOOK_SCRIPT = CLAUDE_TEMPLATES / "hooks" / "telegram-hook.py"
HOOK_MARKER = "hooks/telegram-hook.py"


def _hook_commands(settings: dict, event: str) -> list[str]:
    commands = []
    for group in settings.get("hooks", {}).get(event, []):
        for hook in group.get("hooks", []):
            commands.append(hook.get("command", ""))
    return commands


class ClaudeHookSeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.workspace = root / "workspace"
        self.config = root / "claude"

    def run_init(self):
        env = os.environ.copy()
        env.update({
            "SCH_HARNESS": "claude",
            "SCH_WORKSPACE_ROOT": str(self.workspace),
            "XDG_DATA_HOME": str(self.workspace / "state" / "data"),
            "XDG_CONFIG_HOME": str(self.workspace / "state" / "config"),
            "CLAUDE_CONFIG_DIR": str(self.config),
            "SCH_CLAUDE_TEMPLATE_DIR": str(CLAUDE_TEMPLATES),
        })
        return subprocess.run(
            ["bash", str(SCRIPT)], env=env, text=True, capture_output=True, check=True
        )

    def read_settings(self) -> dict:
        return json.loads((self.config / "settings.json").read_text())

    def test_fresh_seed_registers_all_hooks(self):
        self.run_init()
        settings = self.read_settings()
        # One marked entry per kind (add-telegram-interaction added the
        # decisional pretooluse + the dual-control native kinds;
        # add-task-liveness-safety added the in-turn `tool` milestone).
        expectations = {
            "Stop": ["stop"],
            "Notification": ["notification"],
            "PostToolUse": ["todo", "native", "tool"],
            "PreToolUse": ["pretooluse"],
        }
        for event, kinds in expectations.items():
            commands = _hook_commands(settings, event)
            marked = [c for c in commands if HOOK_MARKER in c]
            self.assertEqual(len(marked), len(kinds), f"{event}: {commands}")
            for kind in kinds:
                self.assertTrue(
                    any(c.endswith(f" {kind}") for c in marked), f"{event}/{kind}"
                )
        todo_groups = settings["hooks"]["PostToolUse"]
        self.assertEqual(todo_groups[0].get("matcher"), "TodoWrite")
        # The decisional hook is matcher-scoped to tools claude prompts for,
        # and its hook timeout stays ABOVE the remote wait (fail-safe path).
        pre_groups = settings["hooks"]["PreToolUse"]
        self.assertIn("Bash", pre_groups[0].get("matcher", ""))
        self.assertGreater(pre_groups[0]["hooks"][0].get("timeout", 0), 600)

    def test_tool_milestone_hook_matches_every_tool(self):
        # add-task-liveness-safety (task 4.1): the in-turn liveness signal is
        # only a signal if it fires for EVERY tool call.
        self.run_init()
        settings = self.read_settings()
        groups = [
            g for g in settings["hooks"]["PostToolUse"]
            if any(
                str(h.get("command", "")).endswith(f"{HOOK_MARKER} tool")
                for h in g.get("hooks", [])
            )
        ]
        self.assertEqual(len(groups), 1, settings["hooks"]["PostToolUse"])
        self.assertEqual(groups[0].get("matcher"), ".*")
        self.assertEqual(groups[0]["hooks"][0].get("timeout"), 10)

    def test_reseed_is_idempotent(self):
        self.run_init()
        self.run_init()
        settings = self.read_settings()
        for event, expected in (("Stop", 1), ("Notification", 1),
                                ("PostToolUse", 3), ("PreToolUse", 1)):
            marked = [c for c in _hook_commands(settings, event) if HOOK_MARKER in c]
            self.assertEqual(len(marked), expected, event)

    def test_operator_hooks_and_keys_survive_merge(self):
        self.config.mkdir(parents=True)
        (self.config / "settings.json").write_text(json.dumps({
            "agent": "operator-agent",
            "custom": {"keep": True},
            "hooks": {
                "Stop": [{"hooks": [{"type": "command", "command": "my-own-stop.sh"}]}],
                "PreToolUse": [{"matcher": "Bash",
                                "hooks": [{"type": "command", "command": "guard.sh"}]}],
            },
        }))
        self.run_init()
        settings = self.read_settings()
        # Operator entries untouched, ours appended.
        stop_commands = _hook_commands(settings, "Stop")
        self.assertIn("my-own-stop.sh", stop_commands)
        self.assertEqual(len([c for c in stop_commands if HOOK_MARKER in c]), 1)
        pre_commands = _hook_commands(settings, "PreToolUse")
        self.assertIn("guard.sh", pre_commands)
        self.assertEqual(len([c for c in pre_commands if HOOK_MARKER in c]), 1)
        self.assertEqual(settings["agent"], "operator-agent")
        self.assertEqual(settings["custom"], {"keep": True})

    def test_unparseable_settings_left_byte_identical(self):
        self.config.mkdir(parents=True)
        original = b"{ not json at all\n"
        (self.config / "settings.json").write_bytes(original)
        result = self.run_init()
        self.assertEqual((self.config / "settings.json").read_bytes(), original)
        self.assertIn("not parseable", result.stdout)


class OpencodePluginSeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.workspace = root / "workspace"
        self.config = self.workspace / "state" / "config"
        self.plugin = self.config / "opencode" / "plugin" / "sch-telegram.js"
        self.sidecar = self.plugin.parent / "sch-telegram.js.sch-seeded"

    def run_init(self, template_dir: Path = OPENCODE_TEMPLATES):
        env = os.environ.copy()
        env.update({
            "SCH_HARNESS": "opencode",
            "SCH_WORKSPACE_ROOT": str(self.workspace),
            "XDG_DATA_HOME": str(self.workspace / "state" / "data"),
            "XDG_CONFIG_HOME": str(self.config),
            "SCH_OPENCODE_TEMPLATE_DIR": str(template_dir),
        })
        return subprocess.run(
            ["bash", str(SCRIPT)], env=env, text=True, capture_output=True, check=True
        )

    def make_template_dir(self, plugin_body: str) -> Path:
        tdir = Path(self.tmp.name) / "templates"
        (tdir / "plugin").mkdir(parents=True, exist_ok=True)
        (tdir / "plugin" / "sch-telegram.js").write_text(plugin_body)
        return tdir

    def test_plugin_seeded_fresh(self):
        self.run_init()
        self.assertTrue(self.plugin.is_file())
        self.assertEqual(
            self.plugin.read_text(),
            (OPENCODE_TEMPLATES / "plugin" / "sch-telegram.js").read_text(),
        )
        # Ownership sidecar written at seed time (refresh-on-upgrade support).
        self.assertTrue(self.sidecar.is_file())

    def test_operator_plugin_never_overwritten(self):
        self.plugin.parent.mkdir(parents=True)
        self.plugin.write_text("// operator version\n")
        self.run_init()
        self.run_init()
        self.assertEqual(self.plugin.read_text(), "// operator version\n")
        self.assertFalse(self.sidecar.exists())

    def test_sch_managed_plugin_refreshed_on_template_upgrade(self):
        # Seed v1 from a synthetic template dir, then upgrade the template:
        # the untouched (sidecar-matching) destination must be refreshed.
        v1 = self.make_template_dir("// v1 plugin\n")
        self.run_init(template_dir=v1)
        self.assertEqual(self.plugin.read_text(), "// v1 plugin\n")
        v2 = self.make_template_dir("// v2 plugin\n")
        result = self.run_init(template_dir=v2)
        self.assertEqual(self.plugin.read_text(), "// v2 plugin\n")
        self.assertIn("refreshed sch-managed OpenCode plugin", result.stdout)

    def test_operator_modified_plugin_survives_template_upgrade(self):
        v1 = self.make_template_dir("// v1 plugin\n")
        self.run_init(template_dir=v1)
        # Operator edits the seeded file: the sidecar no longer matches.
        self.plugin.write_text("// v1 plugin + operator patch\n")
        v2 = self.make_template_dir("// v2 plugin\n")
        result = self.run_init(template_dir=v2)
        self.assertEqual(self.plugin.read_text(), "// v1 plugin + operator patch\n")
        self.assertIn("left operator OpenCode plugin", result.stdout)

    def test_legacy_sch_seeded_plugin_adopted_and_refreshed(self):
        # Workspace seeded by a pre-sidecar image: no sidecar, but the file
        # carries the template header marker -> adopted and refreshed.
        self.plugin.parent.mkdir(parents=True)
        self.plugin.write_text(
            "/* old template: seeded by init-workspace.sh into config */\n"
        )
        v2 = self.make_template_dir("// v2 plugin\n")
        result = self.run_init(template_dir=v2)
        self.assertEqual(self.plugin.read_text(), "// v2 plugin\n")
        self.assertTrue(self.sidecar.is_file())
        self.assertIn("refreshed sch-managed OpenCode plugin", result.stdout)

    def test_current_plugin_gains_sidecar_without_rewrite(self):
        # Legacy workspace whose plugin happens to be byte-identical to the
        # template: graduate to ownership tracking silently.
        template = OPENCODE_TEMPLATES / "plugin" / "sch-telegram.js"
        self.plugin.parent.mkdir(parents=True)
        self.plugin.write_text(template.read_text())
        self.run_init()
        self.assertTrue(self.sidecar.is_file())
        self.assertEqual(self.plugin.read_text(), template.read_text())


class HookScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.spool = Path(self.tmp.name) / "spool"

    def run_hook(
        self, kind: str, stdin_payload: dict, configured: bool,
        execution_mode: str | None = None,
    ):
        env = os.environ.copy()
        env["SCH_TELEGRAM_SPOOL_DIR"] = str(self.spool)
        marker = Path(self.tmp.name) / "telegram-enabled"
        env["SCH_TELEGRAM_ENABLED_MARKER"] = str(marker)
        if execution_mode is not None:
            env["SCH_EXECUTION_MODE"] = execution_mode
        else:
            env.pop("SCH_EXECUTION_MODE", None)
        if configured:
            marker.write_text("{}")
        else:
            marker.unlink(missing_ok=True)
        env.pop("SCH_TELEGRAM_BOT_TOKEN", None)
        env.pop("SCH_TELEGRAM_CHAT_ID", None)
        return subprocess.run(
            ["python3", str(HOOK_SCRIPT), kind],
            input=json.dumps(stdin_payload), env=env,
            text=True, capture_output=True,
        )

    def spool_events(self):
        if not self.spool.is_dir():
            return []
        return [json.loads(p.read_text()) for p in sorted(self.spool.iterdir())]

    def test_noop_without_config(self):
        result = self.run_hook("stop", {"transcript_path": "/nonexistent"}, configured=False)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")
        self.assertFalse(self.spool.exists())

    def test_stop_extracts_last_assistant_text(self):
        transcript = Path(self.tmp.name) / "t.jsonl"
        lines = [
            {"type": "user", "message": {"role": "user", "content": "domanda"}},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "prima risposta"}]}},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Bash"}]}},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "risposta finale"}]}},
        ]
        transcript.write_text("\n".join(json.dumps(line) for line in lines))
        result = self.run_hook("stop", {"transcript_path": str(transcript)}, configured=True)
        self.assertEqual(result.returncode, 0)
        events = self.spool_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "turn-end")
        self.assertEqual(events[0]["payload"]["text"], "risposta finale")
        self.assertEqual(events[0]["source"], "claude")
        self.assertEqual(events[0]["execution_mode"], "interactive")
        self.assertIsInstance(events[0]["ts"], float)

    def test_headless_origin_is_propagated_without_telegram_secrets(self):
        result = self.run_hook(
            "notification", {"message": "waiting"}, configured=True,
            execution_mode="headless",
        )
        self.assertEqual(result.returncode, 0)
        event = self.spool_events()[0]
        self.assertEqual(event["execution_mode"], "headless")
        self.assertEqual(event["source"], "claude")

    def test_notification_maps_permission_and_idle(self):
        r = self.run_hook("notification",
                          {"message": "Claude needs your permission to use Bash"},
                          configured=True)
        self.assertEqual(r.returncode, 0)
        r = self.run_hook("notification",
                          {"message": "Claude is waiting for your input"},
                          configured=True)
        self.assertEqual(r.returncode, 0)
        types = [e["type"] for e in self.spool_events()]
        self.assertEqual(sorted(types), ["await-input", "permission-request"])

    def test_todo_summary(self):
        payload = {"tool_input": {"todos": [
            {"content": "step 1", "status": "completed"},
            {"content": "step 2", "status": "in_progress"},
            {"content": "step 3", "status": "pending"},
        ]}}
        result = self.run_hook("todo", payload, configured=True)
        self.assertEqual(result.returncode, 0)
        events = self.spool_events()
        self.assertEqual(events[0]["type"], "todo")
        self.assertIn("step 2", events[0]["payload"]["summary"])
        self.assertEqual(len(events[0]["payload"]["summary"].splitlines()), 3)

    # --- in-turn `tool` milestones (add-task-liveness-safety, task 4.2) ------

    def test_tool_event_reports_the_written_path(self):
        result = self.run_hook(
            "tool", {"tool_name": "Write", "tool_input": {"file_path": "/w/app.py"}},
            configured=True,
        )
        self.assertEqual(result.returncode, 0)
        events = self.spool_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "tool")
        self.assertEqual(events[0]["payload"]["summary"], "Write: /w/app.py")
        self.assertEqual(events[0]["source"], "claude")

    def test_tool_event_reports_the_first_line_of_a_bash_command(self):
        result = self.run_hook(
            "tool",
            {"tool_name": "Bash",
             "tool_input": {"command": "\n  make test\nmake lint\n"}},
            configured=True,
        )
        self.assertEqual(result.returncode, 0)
        summary = self.spool_events()[0]["payload"]["summary"]
        self.assertEqual(summary, "Bash: make test")

    def test_tool_event_falls_back_to_the_tool_name_alone(self):
        self.run_hook("tool", {"tool_name": "Grep", "tool_input": {"pattern": "x"}},
                      configured=True)
        self.assertEqual(self.spool_events()[0]["payload"]["summary"], "Grep")

    def test_tool_event_target_is_hard_truncated(self):
        self.run_hook(
            "tool", {"tool_name": "Read", "tool_input": {"file_path": "/" + "a" * 500}},
            configured=True,
        )
        summary = self.spool_events()[0]["payload"]["summary"]
        self.assertLess(len(summary), 200)
        self.assertTrue(summary.endswith("…"))

    def test_tool_event_is_not_emitted_for_todowrite(self):
        # Design D4: the `todo` kind already fires on the same tool call —
        # a second event here would double every todo message.
        result = self.run_hook(
            "tool",
            {"tool_name": "TodoWrite",
             "tool_input": {"todos": [{"content": "c", "status": "pending"}]}},
            configured=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.spool_events(), [])

    def test_tool_event_absent_without_a_tool_name(self):
        result = self.run_hook("tool", {}, configured=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.spool_events(), [])

    def test_tool_event_noop_without_config(self):
        result = self.run_hook(
            "tool", {"tool_name": "Write", "tool_input": {"file_path": "/w/app.py"}},
            configured=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertFalse(self.spool.exists())

    def test_garbage_stdin_never_fails(self):
        env = os.environ.copy()
        env.update({
            "SCH_TELEGRAM_SPOOL_DIR": str(self.spool),
            "SCH_TELEGRAM_ENABLED_MARKER": str(Path(self.tmp.name) / "enabled"),
        })
        Path(env["SCH_TELEGRAM_ENABLED_MARKER"]).write_text("{}")
        result = subprocess.run(
            ["python3", str(HOOK_SCRIPT), "stop"],
            input="not json {{{", env=env, text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
