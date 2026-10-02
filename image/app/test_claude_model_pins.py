"""Claude model pins and launch environment (TASK-26).

A fresh workspace must not show Claude Code's "Newer <tier> model available"
dialog: its "Yes" restarts Claude Code, and in the AgentCore microVM that
restart ends the session. These tests cover:

* the three definitions of the Claude launch environment (Dockerfile ENV,
  the generated /etc/profile.d/sch-env.sh, the claude branch of
  harness-wrapper.sh) carry the same values, because login shells opened by
  ``agentcore exec`` do not inherit the container ENV;
* an explicit value in the environment still wins on every path;
* ``check-claude-model-pins.mjs`` (run at image build time) on synthetic
  catalogs, and on the installed Claude Code binary when one is present.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
DOCKERFILE = ROOT / "Dockerfile"
WRAPPER = ROOT / "scripts" / "harness-wrapper.sh"
CHECKER = ROOT / "scripts" / "check-claude-model-pins.mjs"
NODE = shutil.which("node")

EXPECTED = {
    "CLAUDE_CODE_USE_BEDROCK": "1",
    "ANTHROPIC_DEFAULT_FABLE_MODEL": "global.anthropic.claude-fable-5",
    "ANTHROPIC_DEFAULT_FABLE_MODEL_NAME": "Fable 5 (Global)",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "eu.anthropic.claude-opus-5-5",
    "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME": "Opus 5.5 (EU)",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "global.anthropic.claude-sonnet-5",
    "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME": "Sonnet 5 (Global)",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME": "Haiku 4.5 (EU)",
    "ANTHROPIC_CUSTOM_MODEL_OPTION": "global.anthropic.claude-opus-5-5",
    "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME": "Opus 5.5 (Global)",
    "ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION": "Opus 5.5 via the global cross-region inference profile",
    "DISABLE_AUTOUPDATER": "1",
}


def dockerfile_env() -> dict:
    """Values of the EXPECTED names in the Dockerfile ENV instruction."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    found = {}
    for name, value in re.findall(r"^\s+([A-Z_]+)=(\"[^\"]*\"|\S+) \\$", text, re.MULTILINE):
        if name in EXPECTED:
            found[name] = value.strip('"')
    return found


def sch_env_lines() -> list[str]:
    """The sch-env.sh export lines for the EXPECTED names, as written."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    lines = re.findall(r"^\s+'(export ([A-Z_]+)=.*)' \\$", text, re.MULTILINE)
    return [line for line, name in lines if name in EXPECTED]


def source_sch_env(extra_env: dict | None = None) -> dict:
    """Source the sch-env.sh lines in a clean bash and return the result."""
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as fh:
        fh.write("\n".join(sch_env_lines()) + "\n")
        path = fh.name
    try:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **(extra_env or {})}
        out = subprocess.run(
            ["bash", "-c", f'. "{path}" && python3 -c "import json,os; print(json.dumps(dict(os.environ)))"'],
            env=env, text=True, capture_output=True, check=True,
        ).stdout
        return json.loads(out)
    finally:
        os.unlink(path)


class LaunchEnvironmentTests(unittest.TestCase):
    """Every Claude launch path sees the same pins and the updater switch."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.claude = root / "bin" / "claude"
        self.claude.parent.mkdir()
        self.claude.write_text(WRAPPER.read_text())
        self.claude.chmod(0o755)
        self.env_dump = root / "env.json"
        self.real = root / "fake-claude"
        self.real.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os\n"
            f"json.dump(dict(os.environ), open({str(self.env_dump)!r}, 'w'))\n"
        )
        self.real.chmod(0o755)
        self.workspace = root / "workspace"
        (self.workspace / "state").mkdir(parents=True)
        self.staging = root / "provider-keys.env"

    def run_wrapper(self, extra_env: dict | None = None) -> dict:
        # A clean environment, as in a login shell that skipped profile.d:
        # the wrapper alone must supply every default.
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/home/sch"),
            "SCH_HARNESS": "claude",
            "SCH_HARNESS_REAL": str(self.real),
            "SCH_WORKSPACE_ROOT": str(self.workspace),
            "SCH_PROVIDER_KEYS_FILE": str(self.staging),
            "SCH_HARNESS_WAIT": "0",
            **(extra_env or {}),
        }
        proc = subprocess.run([str(self.claude)], env=env, text=True, capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(self.env_dump.read_text())

    def test_dockerfile_env_carries_the_expected_values(self):
        self.assertEqual(dockerfile_env(), EXPECTED)

    def test_login_shell_profile_matches_the_dockerfile_env(self):
        self.assertEqual(len(sch_env_lines()), len(EXPECTED))
        env = source_sch_env()
        self.assertEqual({k: env.get(k) for k in EXPECTED}, EXPECTED)

    def test_wrapper_defaults_match_the_dockerfile_env(self):
        env = self.run_wrapper()
        self.assertEqual({k: env.get(k) for k in EXPECTED}, EXPECTED)

    def test_explicit_values_still_win(self):
        # AC3: an operator value in the environment is never replaced. (A
        # value in the workspace settings.json wins inside Claude Code, which
        # applies settings `env` on top of the process environment.)
        override = {
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "eu.anthropic.claude-opus-5",
            "ANTHROPIC_CUSTOM_MODEL_OPTION": "global.anthropic.claude-opus-5",
            "DISABLE_AUTOUPDATER": "0",
        }
        for env in (self.run_wrapper(override), source_sch_env(override)):
            self.assertEqual({k: env.get(k) for k in override}, override)

    def test_build_runs_the_pin_check_on_both_environments(self):
        text = DOCKERFILE.read_text(encoding="utf-8")
        self.assertIn("COPY scripts/check-claude-model-pins.mjs /usr/local/lib/sch/check-claude-model-pins.mjs", text)
        self.assertIn("RUN node /usr/local/lib/sch/check-claude-model-pins.mjs", text)
        self.assertIn("bash -lc \\\n        'node /usr/local/lib/sch/check-claude-model-pins.mjs", text)
        # The check must run after sch-env.sh exists, or the login-shell half
        # would test nothing.
        self.assertLess(text.index("> /etc/profile.d/sch-env.sh"),
                        text.index("RUN node /usr/local/lib/sch/check-claude-model-pins.mjs"))


def model(mid: str, family: str, name: str, bedrock: str | None) -> dict:
    return {"id": mid, "family": family, "display_name": name, "provider_ids": {"bedrock": bedrock}}


def catalog(opus_target: str = "claude-opus-5-5") -> dict:
    return {
        "//": "Hand-maintained baked-in model catalog — synthetic test copy",
        "schema_version": 1,
        "models": [
            model("claude-haiku-4-5", "haiku", "Haiku 4.5", "us.anthropic.claude-haiku-4-5-20251001-v1:0"),
            model("claude-sonnet-4-0", "sonnet", "Sonnet 4", "us.anthropic.claude-sonnet-4-20250514-v1:0"),
            model("claude-sonnet-4-5", "sonnet", "Sonnet 4.5", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"),
            model("claude-sonnet-5", "sonnet", "Sonnet 5", "us.anthropic.claude-sonnet-5"),
            model("claude-sonnet-5-5", "sonnet", "Sonnet 5.5", "us.anthropic.claude-sonnet-5-5"),
            model("claude-opus-5", "opus", "Opus 5", "us.anthropic.claude-opus-5"),
            model("claude-opus-5-5", "opus", "Opus 5.5", "us.anthropic.claude-opus-5-5"),
            model("claude-opus-6", "opus", "Opus 6", "us.anthropic.claude-opus-6"),
            model("claude-mythos-5", "mythos", "Mythos 5", None),
        ],
        "aliases": {
            "opus": {"default": "claude-opus-6", "per_provider": {"bedrock": opus_target}},
            "sonnet": {"default": "claude-sonnet-5-5", "per_provider": {"bedrock": "claude-sonnet-4-5"}},
            "haiku": {"default": "claude-haiku-4-5"},
        },
        "latest_per_family": {"opus": "claude-opus-6", "sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-4-5"},
    }


IMAGE_PINS = {k: EXPECTED[k] for k in (
    "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL")}


@unittest.skipUnless(NODE, "node is required for check-claude-model-pins.mjs")
class PinCheckTests(unittest.TestCase):
    """The build-time check flags exactly the pins that trigger the dialog."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.binary = Path(self.tmp.name) / "claude.exe"

    def write_binary(self, cat: dict | None) -> None:
        # Shaped like the real thing: binary noise around a minified chunk that
        # assigns the catalog literal to a variable.
        literal = json.dumps(cat, ensure_ascii=False, separators=(",", ":")) if cat else "{}"
        body = b"\x7fELF\x00\x01junk\xff" + f'import{{a}}from"x";var qYn={literal};var z=1;'.encode() + b"\x00tail"
        self.binary.write_bytes(body)

    def run_check(self, pins: dict, binary: Path | None = None):
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **pins}
        return subprocess.run(
            [NODE, str(CHECKER), "--binary", str(binary or self.binary)],
            env=env, text=True, capture_output=True,
        )

    def test_image_pins_pass(self):
        self.write_binary(catalog())
        proc = self.run_check(IMAGE_PINS)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        # Newer models above the Bedrock alias target are notes, not failures.
        self.assertIn("note: newer Sonnet 5.5 is known", proc.stdout)
        self.assertIn("note: newer Opus 6 is known", proc.stdout)

    def test_pin_older_than_the_bedrock_target_fails(self):
        # The 2026-10-02 incident: Opus 5 pinned, Opus 5.5 is the Bedrock target.
        self.write_binary(catalog())
        proc = self.run_check({**IMAGE_PINS, "ANTHROPIC_DEFAULT_OPUS_MODEL": "eu.anthropic.claude-opus-5"})
        self.assertEqual(proc.returncode, 1)
        self.assertIn("FAIL opus", proc.stdout)
        self.assertIn('"Newer Opus model available"', proc.stdout)
        self.assertIn("ok sonnet", proc.stdout)

    def test_a_claude_code_bump_that_moves_the_target_fails(self):
        self.write_binary(catalog(opus_target="claude-opus-6"))
        proc = self.run_check(IMAGE_PINS)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("pin Opus 6 or newer", proc.stdout)

    def test_sonnet_below_target_fails_and_region_prefix_is_ignored(self):
        self.write_binary(catalog())
        proc = self.run_check({**IMAGE_PINS,
                               "ANTHROPIC_DEFAULT_SONNET_MODEL": "eu.anthropic.claude-sonnet-4-20250514-v1:0"})
        self.assertEqual(proc.returncode, 1)
        self.assertIn("FAIL sonnet", proc.stdout)
        proc = self.run_check({**IMAGE_PINS,
                               "ANTHROPIC_DEFAULT_SONNET_MODEL": "global.anthropic.claude-sonnet-4-5-20250929-v1:0"})
        self.assertEqual(proc.returncode, 0, proc.stdout)

    def test_alias_without_bedrock_entry_uses_the_default(self):
        cat = catalog()
        cat["aliases"]["opus"] = {"default": "claude-opus-6"}
        self.write_binary(cat)
        proc = self.run_check(IMAGE_PINS)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("FAIL opus", proc.stdout)

    def test_unknown_missing_or_wrong_family_pin_fails(self):
        self.write_binary(catalog())
        for pins in (
            {**IMAGE_PINS, "ANTHROPIC_DEFAULT_HAIKU_MODEL": "eu.anthropic.claude-haiku-9"},
            {k: v for k, v in IMAGE_PINS.items() if k != "ANTHROPIC_DEFAULT_HAIKU_MODEL"},
            {**IMAGE_PINS, "ANTHROPIC_DEFAULT_HAIKU_MODEL": "eu.anthropic.claude-opus-5-5"},
        ):
            proc = self.run_check(pins)
            self.assertEqual(proc.returncode, 1, proc.stdout)
            self.assertIn("FAIL haiku", proc.stdout)

    def test_missing_catalog_is_a_distinct_error(self):
        self.write_binary(None)
        proc = self.run_check(IMAGE_PINS)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("no model catalog found", proc.stderr)

    def test_installed_claude_code_accepts_the_image_pins(self):
        # Runs where the pinned Claude Code is installed (inside the image).
        root = subprocess.run(["npm", "root", "-g"], text=True, capture_output=True).stdout.strip() \
            if shutil.which("npm") else ""
        binary = Path(root) / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        if not root or not binary.is_file():
            self.skipTest("Claude Code is not installed here")
        proc = self.run_check(IMAGE_PINS, binary=binary)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        stale = self.run_check({**IMAGE_PINS, "ANTHROPIC_DEFAULT_OPUS_MODEL": "eu.anthropic.claude-opus-5"},
                               binary=binary)
        self.assertEqual(stale.returncode, 1, stale.stdout)


if __name__ == "__main__":
    unittest.main()
