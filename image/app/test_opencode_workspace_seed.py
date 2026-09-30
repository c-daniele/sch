"""Container-free tests for the seeded OpenCode config (TASK-9).

Drive scripts/init-workspace.sh with SCH_HARNESS=opencode against temporary
directories and assert the opencode.json contract (spec: runtime-image R20):
the native OpenCode 2 provider shape carrying the seed region, an explicit
Bedrock output cap for the listed Claude inference profiles, and the
never-overwrite rule for an existing config.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "init-workspace.sh"
OPENCODE_TEMPLATES = ROOT / "opencode-templates"

# Max output tokens per model, from the Amazon Bedrock model cards. Bedrock
# rejects a request whose maxTokens exceeds the model maximum, so a seeded
# model without an entry here must be checked against its card first.
AWS_MAX_OUTPUT = {
    "claude-sonnet-4-6": 64000,
    "claude-fable-5": 128000,
    "claude-fable-5-1": 128000,
    "claude-opus-5": 128000,
    "claude-opus-5-5": 128000,
}

# Profiles and values agreed with the maintainer (TASK-9).
AGREED_MAX_TOKENS = {
    "eu.anthropic.claude-sonnet-4-6": 64000,
    "global.anthropic.claude-fable-5": 64000,
    "global.anthropic.claude-fable-5-1": 128000,
    "global.anthropic.claude-opus-5": 64000,
    "global.anthropic.claude-opus-5-5": 128000,
    "eu.anthropic.claude-fable-5": 64000,
    "eu.anthropic.claude-opus-5": 64000,
    "eu.anthropic.claude-opus-5-5": 128000,
}


def _max_tokens(entry: dict):
    return entry.get("body", {}).get("inferenceConfig", {}).get("maxTokens")


class OpencodeConfigSeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.workspace = root / "workspace"
        self.config_home = self.workspace / "state" / "config"
        self.config_file = self.config_home / "opencode" / "opencode.json"
        self.claude_dir = root / "claude"
        self.pi_dir = root / "pi-agent"

    def run_init(self, region: str = "eu-central-1"):
        env = os.environ.copy()
        # A repo URL in the ambient environment would make the script clone it
        # into the temporary workspace, with SCH_REPO_TOKEN if set.
        env.pop("SCH_REPO_URL", None)
        env.pop("SCH_REPO_TOKEN", None)
        env.update({
            "SCH_HARNESS": "opencode",
            "SCH_WORKSPACE_ROOT": str(self.workspace),
            "XDG_DATA_HOME": str(self.workspace / "state" / "data"),
            "XDG_CONFIG_HOME": str(self.config_home),
            "CLAUDE_CONFIG_DIR": str(self.claude_dir),
            "PI_CODING_AGENT_DIR": str(self.pi_dir),
            "SCH_OPENCODE_TEMPLATE_DIR": str(OPENCODE_TEMPLATES),
            "AWS_REGION": region,
            "AWS_DEFAULT_REGION": region,
        })
        return subprocess.run(
            ["bash", str(SCRIPT)], env=env, text=True, capture_output=True, check=True
        )

    def seeded(self) -> dict:
        self.run_init()
        return json.loads(self.config_file.read_text())

    def test_fresh_seed_uses_native_v2_provider_shape(self):
        config = self.seeded()
        bedrock = config["providers"]["amazon-bedrock"]
        self.assertEqual(bedrock["settings"]["region"], "eu-central-1")
        # A V1 `provider` entry next to the V2 one makes OpenCode 2 drop the
        # V1 block, region included: the seed must use one shape only.
        self.assertNotIn("provider", config)

    def test_default_model_has_explicit_max_tokens(self):
        config = self.seeded()
        provider, model_id = config["model"].split("/", 1)
        self.assertEqual(provider, "amazon-bedrock")
        models = config["providers"]["amazon-bedrock"]["models"]
        self.assertIn(model_id, models)
        self.assertIsInstance(_max_tokens(models[model_id]), int)

    def test_agreed_claude_profiles_are_covered(self):
        models = self.seeded()["providers"]["amazon-bedrock"]["models"]
        seeded = {model_id: _max_tokens(entry) for model_id, entry in models.items()}
        for model_id, expected in AGREED_MAX_TOKENS.items():
            self.assertEqual(seeded.get(model_id), expected, model_id)

    def test_max_tokens_within_aws_model_max(self):
        models = self.seeded()["providers"]["amazon-bedrock"]["models"]
        for model_id, entry in models.items():
            family = re.sub(r"^(?:[a-z]+\.)?anthropic\.", "", model_id)
            self.assertIn(family, AWS_MAX_OUTPUT, f"{model_id}: add its AWS max output")
            value = _max_tokens(entry)
            self.assertIsInstance(value, int, model_id)
            self.assertGreater(value, 0, model_id)
            self.assertLessEqual(value, AWS_MAX_OUTPUT[family], model_id)

    def test_rest_of_seed_is_unchanged(self):
        config = self.seeded()
        self.assertEqual(config["default_agent"], "remote-interactive")
        self.assertTrue(config["small_model"].startswith("amazon-bedrock/"))
        self.assertIs(config["mcp"]["aws-docs"]["enabled"], True)
        self.assertIs(config["mcp"]["aws-mcp"]["enabled"], True)
        self.assertIn("AWS_REGION=eu-central-1", config["mcp"]["aws-mcp"]["command"])
        self.assertIs(config["mcp"]["context7"]["enabled"], False)

    def test_existing_config_is_never_overwritten(self):
        self.config_file.parent.mkdir(parents=True)
        self.config_file.write_text('{"custom": true}\n')
        self.run_init()
        self.assertEqual(self.config_file.read_text(), '{"custom": true}\n')


if __name__ == "__main__":
    unittest.main()
