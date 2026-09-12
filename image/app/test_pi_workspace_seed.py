"""Local, container-free tests for Pi configuration seeding (add-pi-harness 2.4).

Same shape as test_claude_workspace_seed.py: drive scripts/init-workspace.sh
with SCH_HARNESS=pi against temporary directories and assert the seeding
contract (spec: runtime-image, "Seeding idempotente del workspace per harness
pi") — fresh workspace fully seeded, operator files never overwritten, a
deleted seeded file re-seeded on the next boot, trust pre-decided for the
canonical worktree, and the sch-managed extension refreshed on image upgrades
while an operator's own extension is left alone.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "init-workspace.sh"
TEMPLATES = ROOT / "pi-templates"


class PiWorkspaceSeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.workspace = root / "workspace"
        self.config = root / "pi-agent"
        self.repo = self.workspace / "repo"

    def run_init(self, region: str = "eu-west-1", **extra):
        env = os.environ.copy()
        # A stray SCH_PI_DEFAULT_MODEL in the ambient environment would make the
        # region-derivation assertions meaningless.
        env.pop("SCH_PI_DEFAULT_MODEL", None)
        env.update(
            {
                "SCH_HARNESS": "pi",
                "SCH_WORKSPACE_ROOT": str(self.workspace),
                "XDG_DATA_HOME": str(self.workspace / "state" / "data"),
                "XDG_CONFIG_HOME": str(self.workspace / "state" / "config"),
                "PI_CODING_AGENT_DIR": str(self.config),
                "SCH_PI_TEMPLATE_DIR": str(TEMPLATES),
                "AWS_REGION": region,
                "AWS_DEFAULT_REGION": region,
            }
        )
        env.update(extra)
        return subprocess.run(
            ["bash", str(SCRIPT)], env=env, text=True, capture_output=True, check=True
        )

    # -- fresh seed ------------------------------------------------------------

    def test_fresh_seed_creates_every_artifact(self):
        self.run_init()

        settings = json.loads((self.config / "settings.json").read_text())
        # Pi's own --provider default is `google`: seeding Bedrock is mandatory.
        self.assertEqual(settings["defaultProvider"], "amazon-bedrock")
        self.assertTrue(settings["defaultModel"])
        # No non-interactive path may ever park on Pi's project-trust prompt.
        self.assertEqual(settings["defaultProjectTrust"], "always")
        self.assertFalse(settings["enableInstallTelemetry"])
        self.assertNotIn("__SCH_BEDROCK_MODEL__", (self.config / "settings.json").read_text())

        for relative in (
            "AGENTS.md",
            "roles/remote-auto.md",
            "roles/remote-interactive.md",
            "extensions/sch-pi.ts",
        ):
            self.assertTrue((self.config / relative).is_file(), relative)

        # The L2 mirror target exists even before the first checkpoint tick.
        self.assertTrue((self.workspace / "state" / "pi").is_dir())

    def test_default_model_follows_the_deployment_region(self):
        self.run_init(region="us-east-1")
        settings = json.loads((self.config / "settings.json").read_text())
        self.assertTrue(
            settings["defaultModel"].startswith("us."),
            settings["defaultModel"],
        )

    def test_unknown_region_falls_back_to_a_globally_invokable_profile(self):
        self.run_init(region="me-central-1")
        settings = json.loads((self.config / "settings.json").read_text())
        self.assertTrue(
            settings["defaultModel"].startswith("global."),
            settings["defaultModel"],
        )

    def test_default_model_override_is_forwarded_opaquely(self):
        self.run_init(SCH_PI_DEFAULT_MODEL="some-vendor/some-model:v9")
        settings = json.loads((self.config / "settings.json").read_text())
        self.assertEqual(settings["defaultModel"], "some-vendor/some-model:v9")

    # -- idempotence -----------------------------------------------------------

    def test_operator_files_are_never_overwritten(self):
        self.config.mkdir(parents=True)
        (self.config / "roles").mkdir()
        (self.config / "settings.json").write_text(
            json.dumps({"defaultProvider": "anthropic", "mine": True})
        )
        (self.config / "AGENTS.md").write_text("operator brief\n")
        (self.config / "roles" / "remote-auto.md").write_text("operator role\n")

        self.run_init()
        self.run_init()

        settings = json.loads((self.config / "settings.json").read_text())
        self.assertEqual(settings, {"defaultProvider": "anthropic", "mine": True})
        self.assertEqual((self.config / "AGENTS.md").read_text(), "operator brief\n")
        self.assertEqual(
            (self.config / "roles" / "remote-auto.md").read_text(), "operator role\n"
        )
        # The absent sibling role is still seeded.
        self.assertTrue((self.config / "roles" / "remote-interactive.md").is_file())

    def test_deleting_a_seeded_file_re_seeds_it_on_the_next_boot(self):
        self.run_init()
        (self.config / "AGENTS.md").unlink()
        (self.config / "roles" / "remote-auto.md").unlink()

        self.run_init()

        self.assertTrue((self.config / "AGENTS.md").is_file())
        self.assertTrue((self.config / "roles" / "remote-auto.md").is_file())

    # -- trust pre-seed (design D8) -------------------------------------------

    def test_trust_is_pre_decided_for_the_canonical_worktree(self):
        self.run_init()
        trust = json.loads((self.config / "trust.json").read_text())
        self.assertIs(trust[os.path.realpath(self.repo)], True)

    def test_existing_trust_decisions_are_preserved(self):
        self.config.mkdir(parents=True)
        other = "/some/other/project"
        (self.config / "trust.json").write_text(
            json.dumps({os.path.realpath(str(self.repo)): False, other: True})
        )

        self.run_init()

        trust = json.loads((self.config / "trust.json").read_text())
        # A deliberate `false` for the worktree is an operator decision.
        self.assertIs(trust[os.path.realpath(self.repo)], False)
        self.assertIs(trust[other], True)

    def test_unparseable_trust_file_is_byte_preserved_with_warning(self):
        self.config.mkdir(parents=True)
        original = b"{ not json at all\n"
        (self.config / "trust.json").write_bytes(original)

        result = self.run_init()

        self.assertEqual((self.config / "trust.json").read_bytes(), original)
        self.assertIn("not parseable", result.stdout)
        # Seeding of everything else still happened.
        self.assertTrue((self.config / "settings.json").is_file())

    # -- extension ownership (sha256 sidecar) ---------------------------------

    def test_sch_managed_extension_is_refreshed_on_image_upgrade(self):
        self.run_init()
        dest = self.config / "extensions" / "sch-pi.ts"
        sidecar = self.config / "extensions" / "sch-pi.ts.sch-seeded"
        stale = "// stale sch version\n"
        dest.write_text(stale)
        sidecar.write_text(hashlib.sha256(stale.encode()).hexdigest() + "\n")

        self.run_init()

        self.assertEqual(
            dest.read_text(), (TEMPLATES / "extensions" / "sch-pi.ts").read_text()
        )

    def test_operator_extension_without_sidecar_is_left_untouched(self):
        self.run_init()
        dest = self.config / "extensions" / "sch-pi.ts"
        (self.config / "extensions" / "sch-pi.ts.sch-seeded").unlink()
        dest.write_text("// my own extension\n")

        result = self.run_init()

        self.assertEqual(dest.read_text(), "// my own extension\n")
        self.assertIn("left operator Pi extension", result.stdout)

    def test_pre_sidecar_workspace_is_adopted_via_the_header_marker(self):
        """A workspace seeded by an image without sidecars carries the
        template's header marker: it is sch-managed and safe to refresh."""
        self.run_init()
        dest = self.config / "extensions" / "sch-pi.ts"
        (self.config / "extensions" / "sch-pi.ts.sch-seeded").unlink()
        dest.write_text("/**\n * SCH extension for Pi (older version).\n */\n")

        self.run_init()

        self.assertEqual(
            dest.read_text(), (TEMPLATES / "extensions" / "sch-pi.ts").read_text()
        )

    # -- cross-harness isolation ---------------------------------------------

    def test_other_harnesses_do_not_seed_the_pi_config_dir(self):
        env_extra = {"SCH_CLAUDE_TEMPLATE_DIR": str(ROOT / "claude-templates")}
        env = os.environ.copy()
        env.update(
            {
                "SCH_HARNESS": "claude",
                "SCH_WORKSPACE_ROOT": str(self.workspace),
                "XDG_DATA_HOME": str(self.workspace / "state" / "data"),
                "XDG_CONFIG_HOME": str(self.workspace / "state" / "config"),
                "CLAUDE_CONFIG_DIR": str(self.workspace / "claude"),
                "PI_CODING_AGENT_DIR": str(self.config),
                "SCH_PI_TEMPLATE_DIR": str(TEMPLATES),
            }
        )
        env.update(env_extra)
        subprocess.run(
            ["bash", str(SCRIPT)], env=env, text=True, capture_output=True, check=True
        )

        self.assertFalse((self.config / "settings.json").exists())
        self.assertFalse((self.config / "extensions").exists())


if __name__ == "__main__":
    unittest.main()
