"""Container-free tests for the provider API key plumbing
(add-provider-api-keys, tasks 2.1-2.3 / 3.2 / 3.3; add-user-provider-keys,
task 3.3).

Two halves, both driven by running the real shell scripts out of the image:

* ``ProviderKeyBridgeTests`` exercise ``scripts/harness-wrapper.sh`` with a
  stub "real binary" that dumps its environment, asserting the per-harness
  mapping of the ``SCH_*`` keys onto the canonical provider names (all staged
  keys for opencode, Anthropic + the Bedrock bearer token for claude, and the
  same additive set for pi).

  add-user-provider-keys (task 3.1/3.3): the SOURCE of those ``SCH_*`` is now
  the tmpfs staging file the shim writes from the invoke payload — not the
  container environment (no provider secret is deployed any more) and not
  ``/proc/1/environ`` (the retired recovery path). Every mapping test therefore
  drives the wrapper through the staging file, and the per-harness branches are
  asserted to be byte-for-byte the same behaviour as before (task 3.2). The
  inherited-environment path is kept in its own test: processes SPAWNED by the
  shim (headless task, serve/ACP) get the set in their spawn environment.
* ``ClaudeApiKeyApprovalSeedTests`` exercise ``scripts/init-workspace.sh``,
  asserting the ``customApiKeyResponses.approved`` pre-approval (design D4) and
  the ``env.CLAUDE_CODE_USE_BEDROCK`` reconciliation in ``settings.json``
  (task 3.3 — Claude Code applies settings ``env`` ON TOP of the process
  environment, so a stale "1" there would defeat the dispatcher's override).
  The withdrawal direction (no key in the session) lives in
  ``test_user_provider_keys.py`` with the rest of design D6.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
WRAPPER = ROOT / "scripts" / "harness-wrapper.sh"
INIT_SCRIPT = ROOT / "scripts" / "init-workspace.sh"
CLAUDE_TEMPLATES = ROOT / "claude-templates"
PI_TEMPLATES = ROOT / "pi-templates"

# A key shaped like the real thing; only its last 20 characters may ever be
# persisted (Claude Code's approval bookkeeping).
ANTHROPIC_KEY = "sk-ant-api03-0123456789abcdefghijKLMNOPQRSTUV"
ANTHROPIC_SUFFIX = ANTHROPIC_KEY[-20:]

# Image-baked Bedrock model defaults the claude branch must neutralize when the
# Anthropic key is present (they are invalid on the Anthropic API).
BEDROCK_MODEL_VARS = (
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL_NAME",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME",
    "ANTHROPIC_CUSTOM_MODEL_OPTION",
    "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME",
    "ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION",
)

GATEWAY_KEYS = ("OPENCODE_API_KEY", "OPENROUTER_API_KEY", "KILO_API_KEY")

# Amazon Bedrock API key (bearer token) issued by a different account (TASK-19):
# the canonical name it maps onto is AWS_BEARER_TOKEN_BEDROCK.
BEARER_TOKEN = "bedrock-api-key-verify-0123456789abcdef"


class ProviderKeyBridgeTests(unittest.TestCase):
    """The dispatcher is the single decision point for provider keys."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.bindir = root / "bin"
        self.bindir.mkdir()
        # One wrapper copy per binary name: the dispatcher resolves the harness
        # from $0 and refuses a mismatch, exactly like the image install.
        for name in ("opencode", "claude", "pi"):
            dest = self.bindir / name
            dest.write_text(WRAPPER.read_text())
            dest.chmod(0o755)
        # Stub "real binary": dumps its environment where the test can read it.
        self.env_dump = root / "env.json"
        self.argv_dump = root / "argv.json"
        self.real = root / "fake-harness"
        self.real.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            f"json.dump(dict(os.environ), open({str(self.env_dump)!r}, 'w'))\n"
            f"json.dump(sys.argv[1:], open({str(self.argv_dump)!r}, 'w'))\n"
        )
        self.real.chmod(0o755)
        self.workspace = root / "workspace"
        (self.workspace / "state").mkdir(parents=True)
        # The tmpfs staging file the shim writes (add-user-provider-keys D3),
        # redirected here so the test never depends on a real /run/sch and can
        # never be polluted by the environment it runs in.
        self.staging_file = root / "provider-keys.env"

    def stage(self, keys: dict) -> None:
        """Write the staging file exactly as the shim does (0600, SCH_ prefix)."""
        self.staging_file.write_text(
            "".join(f"SCH_{name}={value}\n" for name, value in keys.items())
        )
        self.staging_file.chmod(0o600)

    def run_wrapper(
        self,
        harness: str,
        provider_keys: dict | None = None,
        extra_env: dict | None = None,
        inherited: bool = False,
        args: list[str] | None = None,
    ) -> dict:
        """Run the dispatcher with ``provider_keys`` as the session's key set.

        By default the keys arrive the way every path except the shim's own
        children does: through the staging file. ``inherited=True`` instead puts
        them straight in the environment, which is what a process spawned by the
        shim (headless task, serve, ACP) sees.
        """
        if provider_keys and not inherited:
            self.stage(provider_keys)
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/home/sch"),
            "SCH_HARNESS": harness,
            "SCH_HARNESS_REAL": str(self.real),
            "SCH_WORKSPACE_ROOT": str(self.workspace),
            "SCH_PROVIDER_KEYS_FILE": str(self.staging_file),
            # Never block on the readiness marker: this test is about the ENV
            # bridge, not about the shim's seeding handshake.
            "SCH_HARNESS_WAIT": "0",
            # Image ENV the branch has to override rather than default.
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "ANTHROPIC_DEFAULT_FABLE_MODEL": "global.anthropic.claude-fable-5",
            "ANTHROPIC_DEFAULT_FABLE_MODEL_NAME": "Fable 5 (Global)",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "eu.anthropic.claude-opus-5",
            "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME": "Opus 5 (EU)",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "global.anthropic.claude-sonnet-5",
            "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME": "Sonnet 5 (Global)",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME": "Haiku 4.5 (EU)",
            "ANTHROPIC_CUSTOM_MODEL_OPTION": "global.anthropic.claude-opus-5",
            "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME": "Opus 5 (Global)",
            "ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION": "Opus 5 via the global profile",
        }
        if provider_keys and inherited:
            env.update({f"SCH_{k}": v for k, v in provider_keys.items()})
        env.update(extra_env or {})
        if self.env_dump.exists():
            self.env_dump.unlink()
        proc = subprocess.run(
            [str(self.bindir / harness), *(args or [])],
            env=env, text=True, capture_output=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.harness_stderr = proc.stderr
        return json.loads(self.env_dump.read_text())

    # --- opencode branch (task 2.1) -------------------------------------------

    def test_opencode_exports_every_provided_key(self):
        env = self.run_wrapper("opencode", extra_env={
            "ANTHROPIC_API_KEY": ANTHROPIC_KEY,
            "OPENCODE_API_KEY": "oc-key",
            "OPENROUTER_API_KEY": "or-key",
            "KILO_API_KEY": "kilo-key",
        })
        self.assertEqual(env["ANTHROPIC_API_KEY"], ANTHROPIC_KEY)
        # One key covers both the Zen and Go providers: opencode reads the same
        # variable for `opencode` and `opencode-go`.
        self.assertEqual(env["OPENCODE_API_KEY"], "oc-key")
        self.assertEqual(env["OPENROUTER_API_KEY"], "or-key")
        self.assertEqual(env["KILO_API_KEY"], "kilo-key")
        # Keys ADD providers; the harness signals of the branch are untouched.
        self.assertEqual(env["OPENCODE_ENABLE_EXA"], "1")

    def test_opencode_absent_keys_stay_absent(self):
        env = self.run_wrapper("opencode", {"OPENCODE_API_KEY": "oc-key"})
        self.assertEqual(env["OPENCODE_API_KEY"], "oc-key")
        for name in ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "KILO_API_KEY"):
            self.assertNotIn(name, env)

    def test_opencode_empty_key_is_not_exported(self):
        # An empty value must not become an empty credential: the CLI and the
        # shim both drop empty values, but a hand-written staging line or a
        # hand-exported empty string must be inert too.
        env = self.run_wrapper("opencode", {"KILO_API_KEY": ""})
        self.assertNotIn("KILO_API_KEY", env)
        env = self.run_wrapper("opencode", {"KILO_API_KEY": ""}, inherited=True)
        self.assertNotIn("KILO_API_KEY", env)

    def test_opencode_without_any_key_is_unchanged(self):
        env = self.run_wrapper("opencode")
        for name in ("ANTHROPIC_API_KEY", *GATEWAY_KEYS):
            self.assertNotIn(name, env)

    def test_wrapper_defaults_to_interactive_and_canonical_local_paths(self):
        env = self.run_wrapper("opencode", {
            "SCH_TELEGRAM_SPOOL_DIR": "/operator/spool",
            "SCH_TELEGRAM_ENABLED_MARKER": "/operator/enabled",
            "SCH_COMMAND_SHELL_PRESENCE_FILE": "/operator/presence",
        })
        self.assertEqual(env["SCH_EXECUTION_MODE"], "interactive")
        self.assertEqual(env["SCH_TELEGRAM_SPOOL_DIR"], "/tmp/sch-telegram-spool")
        self.assertEqual(env["SCH_TELEGRAM_ENABLED_MARKER"], "/tmp/sch-telegram-enabled")
        self.assertEqual(
            env["SCH_COMMAND_SHELL_PRESENCE_FILE"],
            "/tmp/sch-command-shell-presence.json",
        )

    def test_wrapper_preserves_explicit_headless_mode(self):
        env = self.run_wrapper("claude", extra_env={"SCH_EXECUTION_MODE": "headless"})
        self.assertEqual(env["SCH_EXECUTION_MODE"], "headless")

    def test_wrapper_strips_telegram_secrets_before_real_harness(self):
        env = self.run_wrapper("opencode", extra_env={
            "SCH_TELEGRAM_BOT_TOKEN": "secret-token",
            "SCH_TELEGRAM_CHAT_ID": "-100123",
            "SCH_TELEGRAM_COMMANDS_TABLE": "commands",
        })
        self.assertNotIn("SCH_TELEGRAM_BOT_TOKEN", env)
        self.assertNotIn("SCH_TELEGRAM_CHAT_ID", env)
        self.assertNotIn("SCH_TELEGRAM_COMMANDS_TABLE", env)

    # --- claude branch, key present (task 2.2) --------------------------------

    def test_claude_switches_to_the_anthropic_api(self):
        env = self.run_wrapper("claude", {"ANTHROPIC_API_KEY": ANTHROPIC_KEY})
        self.assertEqual(env["ANTHROPIC_API_KEY"], ANTHROPIC_KEY)
        # Actively overridden, not defaulted: the image ENV says "1".
        self.assertEqual(env["CLAUDE_CODE_USE_BEDROCK"], "0")
        for name in BEDROCK_MODEL_VARS:
            self.assertNotIn(name, env, f"{name} should be neutralized")

    def test_claude_preserves_an_operator_model_override(self):
        env = self.run_wrapper("claude", {"ANTHROPIC_API_KEY": ANTHROPIC_KEY}, extra_env={
            # A name valid on the Anthropic API: not a Bedrock profile, so the
            # operator's intent survives the switch (design D3).
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-4-5-20250929",
            "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME": "Sonnet 4.5 (API)",
        })
        self.assertEqual(env["ANTHROPIC_DEFAULT_SONNET_MODEL"], "claude-sonnet-4-5-20250929")
        self.assertEqual(env["ANTHROPIC_DEFAULT_SONNET_MODEL_NAME"], "Sonnet 4.5 (API)")
        # The other slots are still Bedrock profiles and are still dropped.
        self.assertNotIn("ANTHROPIC_DEFAULT_OPUS_MODEL", env)

    def test_claude_drops_arn_shaped_model_defaults(self):
        env = self.run_wrapper("claude", {"ANTHROPIC_API_KEY": ANTHROPIC_KEY}, extra_env={
            "ANTHROPIC_DEFAULT_OPUS_MODEL":
                "arn:aws:bedrock:eu-west-1:1234:inference-profile/eu.anthropic.claude-opus-5",
        })
        self.assertNotIn("ANTHROPIC_DEFAULT_OPUS_MODEL", env)

    def test_claude_never_receives_gateway_keys(self):
        env = self.run_wrapper("claude", {
            "ANTHROPIC_API_KEY": ANTHROPIC_KEY,
            "OPENCODE_API_KEY": "oc-key",
            "OPENROUTER_API_KEY": "or-key",
            "KILO_API_KEY": "kilo-key",
        })
        for name in GATEWAY_KEYS:
            self.assertNotIn(name, env)
        # And the gateway endpoints are never wired in.
        self.assertNotIn("ANTHROPIC_BASE_URL", env)
        # The SCH_* originals stay visible for deliberate manual use.
        self.assertEqual(env["SCH_OPENCODE_API_KEY"], "oc-key")

    # --- claude branch, no key (task 2.3) -------------------------------------

    def test_claude_without_the_key_keeps_bedrock(self):
        env = self.run_wrapper("claude", {
            "OPENCODE_API_KEY": "oc-key",
            "OPENROUTER_API_KEY": "or-key",
            "KILO_API_KEY": "kilo-key",
        })
        self.assertEqual(env["CLAUDE_CODE_USE_BEDROCK"], "1")
        self.assertEqual(env["ANTHROPIC_DEFAULT_SONNET_MODEL"], "global.anthropic.claude-sonnet-5")
        self.assertEqual(env["ANTHROPIC_CUSTOM_MODEL_OPTION"], "global.anthropic.claude-opus-5")
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        for name in GATEWAY_KEYS:
            self.assertNotIn(name, env)
        self.assertNotIn("ANTHROPIC_BASE_URL", env)

    # --- pi branch (add-pi-harness task 4.8; extend-pi-gateway-keys TASK-18) --

    def test_pi_exports_all_four_keys_additively(self):
        env = self.run_wrapper("pi", {
            "ANTHROPIC_API_KEY": ANTHROPIC_KEY,
            "OPENROUTER_API_KEY": "or-key",
            "OPENCODE_API_KEY": "oc-key",
            "KILO_API_KEY": "kilo-key",
        })
        self.assertEqual(env["ANTHROPIC_API_KEY"], ANTHROPIC_KEY)
        self.assertEqual(env["OPENROUTER_API_KEY"], "or-key")
        self.assertEqual(env["OPENCODE_API_KEY"], "oc-key")
        self.assertEqual(env["KILO_API_KEY"], "kilo-key")
        self.assertEqual(env["PI_CODING_AGENT_DIR"], "/home/sch/.pi/agent")
        self.assertEqual(env["PI_TELEMETRY"], "0")
        self.assertEqual(env["PI_SKIP_VERSION_CHECK"], "1")

    def test_pi_gateway_keys_absent_stay_absent(self):
        # Only the staged gateways are exported: a session without, say, the
        # Kilo key must not see an empty KILO_API_KEY.
        env = self.run_wrapper("pi", {"OPENCODE_API_KEY": "oc-key"})
        self.assertEqual(env["OPENCODE_API_KEY"], "oc-key")
        self.assertNotIn("KILO_API_KEY", env)
        self.assertNotIn("ANTHROPIC_API_KEY", env)

    def test_pi_without_keys_keeps_bedrock_only(self):
        env = self.run_wrapper("pi")
        for name in ("ANTHROPIC_API_KEY", *GATEWAY_KEYS):
            self.assertNotIn(name, env)

    # --- cross-account Bedrock bearer token (TASK-19) --------------------------

    def test_opencode_receives_the_bedrock_bearer_token(self):
        env = self.run_wrapper("opencode", {"BEDROCK_API_KEY": BEARER_TOKEN})
        self.assertEqual(env["AWS_BEARER_TOKEN_BEDROCK"], BEARER_TOKEN)
        self.assertNotIn("BEDROCK_API_KEY", env)

    def test_pi_receives_the_bedrock_bearer_token_additively(self):
        # Same additive semantics as every other key: the bearer token re-auths
        # the seeded amazon-bedrock provider, no switchover, no model change.
        env = self.run_wrapper("pi", {"BEDROCK_API_KEY": BEARER_TOKEN})
        self.assertEqual(env["AWS_BEARER_TOKEN_BEDROCK"], BEARER_TOKEN)

    def test_claude_receives_the_bedrock_bearer_token_and_keeps_bedrock(self):
        env = self.run_wrapper("claude", {"BEDROCK_API_KEY": BEARER_TOKEN})
        self.assertEqual(env["AWS_BEARER_TOKEN_BEDROCK"], BEARER_TOKEN)
        # The bearer token re-auths Bedrock itself: the harness signal and the
        # Bedrock model defaults stay untouched (they resolve in the key's
        # account).
        self.assertEqual(env["CLAUDE_CODE_USE_BEDROCK"], "1")
        self.assertEqual(
            env["ANTHROPIC_DEFAULT_SONNET_MODEL"], "global.anthropic.claude-sonnet-5"
        )
        self.assertNotIn("ANTHROPIC_API_KEY", env)

    def test_claude_prefers_the_anthropic_key_over_the_bearer_token(self):
        # Precedence (TASK-19): with both keys staged claude rides the Anthropic
        # API and the unused bearer token is NOT handed to the process.
        env = self.run_wrapper("claude", {
            "ANTHROPIC_API_KEY": ANTHROPIC_KEY,
            "BEDROCK_API_KEY": BEARER_TOKEN,
        })
        self.assertEqual(env["ANTHROPIC_API_KEY"], ANTHROPIC_KEY)
        self.assertEqual(env["CLAUDE_CODE_USE_BEDROCK"], "0")
        self.assertNotIn("AWS_BEARER_TOKEN_BEDROCK", env)
        for name in BEDROCK_MODEL_VARS:
            self.assertNotIn(name, env, f"{name} should be neutralized")

    def test_bedrock_bearer_token_absent_stays_absent(self):
        for harness in ("opencode", "claude", "pi"):
            with self.subTest(harness=harness):
                env = self.run_wrapper(harness, {"OPENROUTER_API_KEY": "or-key"})
                self.assertNotIn("AWS_BEARER_TOKEN_BEDROCK", env)

    def test_no_key_value_of_the_bearer_token_reaches_wrapper_output(self):
        for harness in ("opencode", "claude", "pi"):
            with self.subTest(harness=harness):
                self.run_wrapper(harness, {"BEDROCK_API_KEY": BEARER_TOKEN})
                self.assertNotIn(BEARER_TOKEN, self.harness_stderr)

    def test_bare_pi_uses_the_interactive_role(self):
        pi_dir = Path(self.tmp.name) / "pi-agent"
        role = pi_dir / "roles" / "remote-interactive.md"
        role.parent.mkdir(parents=True)
        role.write_text("interactive")
        self.run_wrapper("pi", extra_env={"PI_CODING_AGENT_DIR": str(pi_dir)})
        self.assertEqual(
            json.loads(self.argv_dump.read_text()),
            ["--append-system-prompt", str(role)],
        )

    def test_pi_strips_inherited_telegram_config(self):
        env = self.run_wrapper("pi", extra_env={
            "SCH_TELEGRAM_BOT_TOKEN": "super-secret-token",
            "SCH_TELEGRAM_CHAT_ID": "-100123",
            "SCH_TELEGRAM_COMMANDS_TABLE": "commands",
        })
        self.assertNotIn("SCH_TELEGRAM_NOTIFICATIONS_CONFIGURED", env)
        self.assertNotIn("SCH_TELEGRAM_BOT_TOKEN", env)
        self.assertNotIn("SCH_TELEGRAM_CHAT_ID", env)
        self.assertNotIn("SCH_TELEGRAM_COMMANDS_TABLE", env)

    # --- secrecy (task 2.4) ---------------------------------------------------

    def test_no_key_value_reaches_wrapper_output(self):
        for harness in ("opencode", "claude", "pi"):
            with self.subTest(harness=harness):
                self.run_wrapper(harness, {
                    "ANTHROPIC_API_KEY": ANTHROPIC_KEY,
                    "OPENCODE_API_KEY": "oc-key-secret",
                })
                self.assertNotIn(ANTHROPIC_KEY, self.harness_stderr)
                self.assertNotIn("oc-key-secret", self.harness_stderr)

    # --- staging file as the source (add-user-provider-keys, tasks 3.1/3.3) ---

    def test_raw_shell_without_a_staging_file_has_no_provider_key(self):
        # `agentcore exec --it` without `sch`: nothing staged the keys, so the
        # session stays Bedrock-only (spec: user-provider-keys, "Shell grezza
        # senza file tmpfs"). Also the regression guard for the retired
        # /proc/1/environ recovery: no deployment-wide key is reachable any more.
        self.assertFalse(self.staging_file.exists())
        for harness in ("opencode", "claude"):
            with self.subTest(harness=harness):
                env = self.run_wrapper(harness)
                for name in ("ANTHROPIC_API_KEY", *GATEWAY_KEYS):
                    self.assertNotIn(name, env)
                for name in (
                    "SCH_ANTHROPIC_API_KEY", "SCH_OPENCODE_API_KEY",
                    "SCH_OPENROUTER_API_KEY", "SCH_KILO_API_KEY",
                ):
                    self.assertNotIn(name, env)
        # And claude keeps every Bedrock signal.
        self.assertEqual(self.run_wrapper("claude")["CLAUDE_CODE_USE_BEDROCK"], "1")

    def test_staging_file_and_inherited_env_are_equivalent(self):
        keys = {"ANTHROPIC_API_KEY": ANTHROPIC_KEY, "OPENROUTER_API_KEY": "or-key"}
        staged = self.run_wrapper("opencode", keys)
        self.staging_file.unlink()
        inherited = self.run_wrapper("opencode", keys, inherited=True)
        for name in ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"):
            self.assertEqual(staged[name], inherited[name])
            self.assertEqual(staged["SCH_" + name], inherited["SCH_" + name])

    def test_staging_file_is_never_sourced(self):
        # The file is read line by line and only the allowlisted names are
        # exported: no content of it can be executed, and nothing else leaks
        # into the harness environment.
        canary = Path(self.tmp.name) / "sourced-canary"
        self.staging_file.write_text(
            "SCH_KILO_API_KEY=kilo-value\n"
            f"$(touch {canary})\n"
            "PATH=/nowhere\n"
            "SCH_EVIL=1\n"
            "; rm -rf /\n"
            "SCH_ANTHROPIC_API_KEY=value with spaces and $HOME\n"
        )
        self.staging_file.chmod(0o600)
        env = self.run_wrapper("opencode")
        self.assertEqual(env["KILO_API_KEY"], "kilo-value")
        self.assertEqual(env["ANTHROPIC_API_KEY"], "value with spaces and $HOME")
        self.assertNotIn("SCH_EVIL", env)
        self.assertNotEqual(env["PATH"], "/nowhere")
        self.assertFalse(canary.exists())

    def test_missing_staging_directory_is_not_fatal(self):
        self.staging_file = Path(self.tmp.name) / "no-such-dir" / "provider-keys.env"
        env = self.run_wrapper("opencode")
        self.assertNotIn("ANTHROPIC_API_KEY", env)


class ClaudeApiKeyApprovalSeedTests(unittest.TestCase):
    """init-workspace.sh pre-approves the key and keeps settings.json coherent."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.workspace = root / "workspace"
        self.config = root / "claude"
        self.pi_config = root / "pi"

    def run_init(self, key: str | None = ANTHROPIC_KEY):
        env = os.environ.copy()
        env.pop("SCH_ANTHROPIC_API_KEY", None)
        env.update({
            "SCH_HARNESS": "claude",
            "SCH_WORKSPACE_ROOT": str(self.workspace),
            "XDG_DATA_HOME": str(self.workspace / "state" / "data"),
            "XDG_CONFIG_HOME": str(self.workspace / "state" / "config"),
            "CLAUDE_CONFIG_DIR": str(self.config),
            "SCH_CLAUDE_TEMPLATE_DIR": str(CLAUDE_TEMPLATES),
            "PI_CODING_AGENT_DIR": str(self.pi_config),
            "SCH_PI_TEMPLATE_DIR": str(PI_TEMPLATES),
        })
        if key is not None:
            env["SCH_ANTHROPIC_API_KEY"] = key
        return subprocess.run(
            ["bash", str(INIT_SCRIPT)], env=env, text=True, capture_output=True,
            check=True,
        )

    @property
    def global_config(self) -> Path:
        # Same resolution as the pinned Claude Code binary: with
        # CLAUDE_CONFIG_DIR set the global config lives inside it.
        return self.config / ".claude.json"

    def read_global_config(self) -> dict:
        return json.loads(self.global_config.read_text())

    def read_settings(self) -> dict:
        return json.loads((self.config / "settings.json").read_text())

    # --- no key: nothing is written ------------------------------------------

    def test_without_key_the_global_config_is_not_created(self):
        self.run_init(key=None)
        self.assertFalse(self.global_config.exists())
        self.assertEqual(self.read_settings()["env"]["CLAUDE_CODE_USE_BEDROCK"], "1")

    def test_without_key_an_existing_global_config_is_untouched(self):
        self.config.mkdir(parents=True)
        original = json.dumps({"numStartups": 7, "theme": "dark"})
        self.global_config.write_text(original)

        self.run_init(key=None)

        self.assertEqual(self.global_config.read_text(), original)

    # --- key present: approval is pre-seeded ---------------------------------

    def test_fresh_seed_approves_the_key_suffix_only(self):
        self.run_init()
        responses = self.read_global_config()["customApiKeyResponses"]
        self.assertEqual(responses["approved"], [ANTHROPIC_SUFFIX])
        self.assertEqual(responses["rejected"], [])
        # The key itself never lands on disk — only its last 20 characters.
        body = self.global_config.read_text()
        self.assertNotIn(ANTHROPIC_KEY, body)
        self.assertEqual(len(ANTHROPIC_SUFFIX), 20)
        self.assertEqual(
            stat.S_IMODE(self.global_config.stat().st_mode), 0o600
        )

    def test_existing_user_content_is_preserved(self):
        self.config.mkdir(parents=True)
        self.global_config.write_text(json.dumps({
            "numStartups": 12,
            "theme": "light",
            "projects": {"/mnt/workspace/repo": {"hasTrustDialogAccepted": True}},
            "customApiKeyResponses": {"approved": ["previouslyapprovedkey1"]},
        }))

        self.run_init()

        data = self.read_global_config()
        self.assertEqual(data["numStartups"], 12)
        self.assertEqual(data["theme"], "light")
        self.assertTrue(data["projects"]["/mnt/workspace/repo"]["hasTrustDialogAccepted"])
        self.assertEqual(
            data["customApiKeyResponses"]["approved"],
            ["previouslyapprovedkey1", ANTHROPIC_SUFFIX],
        )

    def test_a_previous_rejection_of_the_same_key_is_cleared(self):
        self.config.mkdir(parents=True)
        self.global_config.write_text(json.dumps({
            "customApiKeyResponses": {"approved": [], "rejected": [ANTHROPIC_SUFFIX]},
        }))

        self.run_init()

        responses = self.read_global_config()["customApiKeyResponses"]
        self.assertEqual(responses["approved"], [ANTHROPIC_SUFFIX])
        self.assertEqual(responses["rejected"], [])

    def test_reruns_are_idempotent(self):
        self.run_init()
        first = self.global_config.read_text()
        self.run_init()
        self.assertEqual(self.global_config.read_text(), first)
        result = self.run_init()
        self.assertIn("already reconciled", result.stdout)
        self.assertEqual(
            self.read_global_config()["customApiKeyResponses"]["approved"],
            [ANTHROPIC_SUFFIX],
        )

    def test_unparseable_global_config_is_byte_preserved_with_warning(self):
        self.config.mkdir(parents=True)
        original = b"{ not json at all\n"
        self.global_config.write_bytes(original)

        result = self.run_init()

        self.assertEqual(self.global_config.read_bytes(), original)
        self.assertIn("not parseable", result.stdout)

    def test_existing_config_json_wins_over_claude_json(self):
        # Claude Code prefers `$CLAUDE_CONFIG_DIR/.config.json` when it exists.
        self.config.mkdir(parents=True)
        legacy = self.config / ".config.json"
        legacy.write_text(json.dumps({"numStartups": 3}))

        self.run_init()

        self.assertFalse(self.global_config.exists())
        data = json.loads(legacy.read_text())
        self.assertEqual(data["numStartups"], 3)
        self.assertEqual(data["customApiKeyResponses"]["approved"], [ANTHROPIC_SUFFIX])

    def test_no_key_value_reaches_init_output(self):
        result = self.run_init()
        self.assertNotIn(ANTHROPIC_KEY, result.stdout)
        self.assertNotIn(ANTHROPIC_KEY, result.stderr)
        self.assertNotIn(ANTHROPIC_SUFFIX, result.stdout)
        self.assertNotIn(ANTHROPIC_SUFFIX, result.stderr)

    # --- settings.json env reconciliation (task 3.3) -------------------------

    def test_fresh_seed_disables_bedrock_when_the_key_is_present(self):
        self.run_init()
        self.assertEqual(self.read_settings()["env"]["CLAUDE_CODE_USE_BEDROCK"], "0")

    def test_existing_settings_are_flipped_when_the_key_appears(self):
        # A workspace seeded before the key was configured: settings.json still
        # forces Bedrock ON, and Claude Code applies settings `env` ON TOP of
        # the process environment — so it would defeat the dispatcher override.
        self.run_init(key=None)
        self.assertEqual(self.read_settings()["env"]["CLAUDE_CODE_USE_BEDROCK"], "1")

        self.run_init()

        self.assertEqual(self.read_settings()["env"]["CLAUDE_CODE_USE_BEDROCK"], "0")

    def test_removing_the_key_restores_bedrock(self):
        self.run_init()
        self.assertEqual(self.read_settings()["env"]["CLAUDE_CODE_USE_BEDROCK"], "0")

        self.run_init(key=None)

        self.assertEqual(self.read_settings()["env"]["CLAUDE_CODE_USE_BEDROCK"], "1")

    def test_operator_env_values_are_left_alone(self):
        self.config.mkdir(parents=True)
        (self.config / "settings.json").write_text(json.dumps({
            "agent": "operator-agent",
            "env": {"CLAUDE_CODE_USE_BEDROCK": "true", "FOO": "bar"},
        }))

        self.run_init()

        settings = self.read_settings()
        # Not one of the two values this script writes => operator's choice.
        self.assertEqual(settings["env"]["CLAUDE_CODE_USE_BEDROCK"], "true")
        self.assertEqual(settings["env"]["FOO"], "bar")
        self.assertEqual(settings["agent"], "operator-agent")


if __name__ == "__main__":
    unittest.main()
