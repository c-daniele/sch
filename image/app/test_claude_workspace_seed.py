"""Local, container-free tests for Claude configuration seeding."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "init-workspace.sh"
TEMPLATES = ROOT / "claude-templates"


class ClaudeWorkspaceSeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.workspace = root / "workspace"
        self.config = root / "claude"

    def run_init(self):
        env = os.environ.copy()
        env.update(
            {
                "SCH_HARNESS": "claude",
                "SCH_WORKSPACE_ROOT": str(self.workspace),
                "XDG_DATA_HOME": str(self.workspace / "state" / "data"),
                "XDG_CONFIG_HOME": str(self.workspace / "state" / "config"),
                "CLAUDE_CONFIG_DIR": str(self.config),
                "SCH_CLAUDE_TEMPLATE_DIR": str(TEMPLATES),
            }
        )
        return subprocess.run(
            ["bash", str(SCRIPT)], env=env, text=True, capture_output=True, check=True
        )

    def test_fresh_seed_creates_templates_and_interactive_default(self):
        self.run_init()
        settings = json.loads((self.config / "settings.json").read_text())
        self.assertEqual(settings["agent"], "remote-interactive")
        self.assertIn("context7", settings["disabledMcpjsonServers"])
        for relative in (
            "CLAUDE.md", "agents/remote-interactive.md", "agents/remote-auto.md"
        ):
            self.assertTrue((self.config / relative).is_file(), relative)

    def test_existing_templates_and_agent_are_never_overwritten(self):
        (self.config / "agents").mkdir(parents=True)
        (self.config / "CLAUDE.md").write_text("operator global\n")
        (self.config / "agents" / "remote-auto.md").write_text("operator agent\n")
        (self.config / "settings.json").write_text(
            json.dumps({"agent": "operator-agent", "custom": {"keep": True}})
        )

        self.run_init()
        self.run_init()

        self.assertEqual((self.config / "CLAUDE.md").read_text(), "operator global\n")
        self.assertEqual(
            (self.config / "agents" / "remote-auto.md").read_text(), "operator agent\n"
        )
        settings = json.loads((self.config / "settings.json").read_text())
        self.assertEqual(settings["agent"], "operator-agent")
        self.assertEqual(settings["custom"], {"keep": True})
        self.assertEqual(settings["disabledMcpjsonServers"].count("context7"), 1)

    def test_missing_agent_is_merged_while_custom_settings_survive(self):
        self.config.mkdir(parents=True)
        (self.config / "settings.json").write_text(
            json.dumps({"env": {"FOO": "bar"}, "cleanupPeriodDays": 30})
        )

        self.run_init()

        settings = json.loads((self.config / "settings.json").read_text())
        self.assertEqual(settings["agent"], "remote-interactive")
        self.assertEqual(settings["env"], {"FOO": "bar"})
        self.assertEqual(settings["cleanupPeriodDays"], 30)

    def test_unparseable_settings_are_byte_preserved_with_warning(self):
        self.config.mkdir(parents=True)
        original = b"{ definitely not json\n"
        (self.config / "settings.json").write_bytes(original)

        result = self.run_init()

        self.assertEqual((self.config / "settings.json").read_bytes(), original)
        self.assertIn("not parseable", result.stdout)
        self.assertTrue((self.config / "agents" / "remote-auto.md").is_file())


if __name__ == "__main__":
    unittest.main()
