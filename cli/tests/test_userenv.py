"""Unit tests for the per-user provider keys on the CLI side
(add-user-provider-keys, tasks 1.1/1.2/1.3/1.4).

Three concerns, one per class:

* :class:`UserEnvParseTests` — the tolerant dotenv parse of ``~/.sch/env``, the
  name allowlist and the permission gate (spec: user-provider-keys,
  "Sorgente utente delle chiavi provider" + "Permessi del file env utente").
* :class:`PayloadProviderKeyTests` — the payload shape: the field appears only
  when keys exist, and a payload without keys is byte-identical to the
  pre-capability one.
* :class:`InvokeCoverageTests` / :class:`SecrecyTests` — every invoke path
  carries the keys (they are injected in the two invocation wrappers, which is
  what makes the coverage requirement structural), and no key value can reach
  any CLI output, not even through a failed invocation.
"""

import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_CLI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _CLI_DIR not in sys.path:
    sys.path.insert(0, _CLI_DIR)

from sch import runtime
from sch import userenv
from sch.config import Config

# Key-shaped values: only their NAMES may ever appear in any output.
ANTHROPIC_KEY = "sk-ant-api03-0123456789abcdefghijKLMNOPQRSTUV"
OPENROUTER_KEY = "sk-or-v1-secret-openrouter-value"
BEARER_TOKEN = "bedrock-api-key-verify-0123456789abcdef"

REPO_ROOT = Path(__file__).resolve().parents[2]


class _EnvFileFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.env_file = self.home / ".sch" / "env"
        self.env_file.parent.mkdir(parents=True)
        userenv.reset_cache()
        self.addCleanup(userenv.reset_cache)

    def write_env(self, text, mode=0o600):
        self.env_file.write_text(text)
        os.chmod(self.env_file, mode)

    def load(self):
        self.warnings = []
        return userenv.load_provider_keys(
            path=self.env_file, warn=self.warnings.append
        )


class UserEnvParseTests(_EnvFileFixture):
    def test_configured_keys_are_returned(self):
        self.write_env(
            "# my keys\n"
            f"OPENROUTER_API_KEY={OPENROUTER_KEY}\n"
            f"ANTHROPIC_API_KEY={ANTHROPIC_KEY}\n"
        )
        self.assertEqual(
            self.load(),
            {"ANTHROPIC_API_KEY": ANTHROPIC_KEY, "OPENROUTER_API_KEY": OPENROUTER_KEY},
        )
        self.assertEqual(self.warnings, [])

    def test_missing_file_yields_no_keys_and_no_warning(self):
        self.assertEqual(self.load(), {})
        self.assertEqual(self.warnings, [])

    def test_empty_file_yields_no_keys(self):
        self.write_env("")
        self.assertEqual(self.load(), {})
        self.assertEqual(self.warnings, [])

    def test_allowlist_excludes_everything_else(self):
        self.write_env(
            "AWS_SECRET_ACCESS_KEY=must-not-travel\n"
            "TELEGRAM_BOT_TOKEN=must-not-travel\n"
            "SCH_REGION=eu-west-1\n"
            f"KILO_API_KEY=kilo-value\n"
        )
        self.assertEqual(self.load(), {"KILO_API_KEY": "kilo-value"})

    def test_bedrock_api_key_is_allowlisted(self):
        # TASK-19 (cross-account Bedrock): the Amazon Bedrock API key (bearer
        # token) issued by another account rides the same pipeline.
        self.write_env(
            "AWS_ACCESS_KEY_ID=must-not-travel\n"
            "AWS_SECRET_ACCESS_KEY=must-not-travel\n"
            f"BEDROCK_API_KEY={BEARER_TOKEN}\n"
        )
        self.assertEqual(self.load(), {"BEDROCK_API_KEY": BEARER_TOKEN})

    def test_github_token_is_allowlisted(self):
        # TASK-26 (opt-in GitHub access): the forge credential rides the
        # same staging/transport/secrecy pipeline as the provider keys.
        self.write_env(
            "GH_TOKEN=must-not-travel\n"
            "GITHUB_TOKEN=github_pat_testvalue\n"
        )
        self.assertEqual(self.load(), {"GITHUB_TOKEN": "github_pat_testvalue"})

    def test_malformed_lines_are_skipped_and_valid_ones_survive(self):
        self.write_env(
            "this line has no equals sign\n"
            "=novalue\n"
            "9INVALID=x\n"
            "   \n"
            "# comment = not a key\n"
            f"export OPENCODE_API_KEY='quoted-value'\n"
            f'ANTHROPIC_API_KEY="{ANTHROPIC_KEY}"\n'
        )
        self.assertEqual(
            self.load(),
            {"ANTHROPIC_API_KEY": ANTHROPIC_KEY, "OPENCODE_API_KEY": "quoted-value"},
        )

    def test_empty_value_behaves_like_not_configured(self):
        self.write_env("KILO_API_KEY=\nOPENCODE_API_KEY=   \n")
        self.assertEqual(self.load(), {})

    def test_last_assignment_wins(self):
        self.write_env("KILO_API_KEY=old\nKILO_API_KEY=new\n")
        self.assertEqual(self.load(), {"KILO_API_KEY": "new"})

    @unittest.skipUnless(os.name == "posix", "permission bits are POSIX-only")
    def test_world_readable_file_is_refused_with_the_remedy(self):
        self.write_env(f"ANTHROPIC_API_KEY={ANTHROPIC_KEY}\n", mode=0o644)
        self.assertEqual(self.load(), {})
        self.assertEqual(len(self.warnings), 1)
        message = self.warnings[0]
        self.assertIn("chmod 600", message)
        self.assertIn(str(self.env_file), message)
        # The warning names the problem, never the secret.
        self.assertNotIn(ANTHROPIC_KEY, message)

    @unittest.skipUnless(os.name == "posix", "permission bits are POSIX-only")
    def test_group_readable_file_is_refused_too(self):
        self.write_env(f"ANTHROPIC_API_KEY={ANTHROPIC_KEY}\n", mode=0o640)
        self.assertEqual(self.load(), {})
        self.assertEqual(len(self.warnings), 1)

    @unittest.skipUnless(os.name == "posix", "permission bits are POSIX-only")
    def test_owner_only_file_is_accepted(self):
        self.write_env(f"ANTHROPIC_API_KEY={ANTHROPIC_KEY}\n", mode=0o600)
        self.assertEqual(self.load(), {"ANTHROPIC_API_KEY": ANTHROPIC_KEY})
        self.assertEqual(self.warnings, [])

    def test_a_directory_in_place_of_the_file_is_not_fatal(self):
        target = self.home / ".sch" / "envdir"
        target.mkdir()
        self.warnings = []
        self.assertEqual(
            userenv.load_provider_keys(path=target, warn=self.warnings.append), {}
        )
        self.assertEqual(len(self.warnings), 1)

    def test_values_with_newlines_are_dropped(self):
        # Cannot survive the one-line-per-key staging file in the microVM.
        self.assertEqual(
            userenv.select_provider_keys({"KILO_API_KEY": "a\nb"}), {}
        )

    def test_path_resolution_follows_home(self):
        with patch.dict(os.environ, {"HOME": str(self.home)}, clear=False):
            self.assertEqual(userenv.user_env_path(), self.env_file)

    def test_config_exposes_the_keys_and_caches_them(self):
        self.write_env(f"ANTHROPIC_API_KEY={ANTHROPIC_KEY}\n")
        env = {"HOME": str(self.home)}
        env.pop("XDG_CONFIG_HOME", None)
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("XDG_CONFIG_HOME", None)
            cfg = Config()
            self.assertEqual(cfg.provider_keys, {"ANTHROPIC_API_KEY": ANTHROPIC_KEY})
            # Mutating the returned dict cannot poison the cached set.
            cfg.provider_keys["ANTHROPIC_API_KEY"] = "tampered"
            self.assertEqual(cfg.provider_keys, {"ANTHROPIC_API_KEY": ANTHROPIC_KEY})


class PayloadProviderKeyTests(unittest.TestCase):
    KEYS = {
        "ANTHROPIC_API_KEY": ANTHROPIC_KEY,
        "KILO_API_KEY": "kilo-value",
        "GITHUB_TOKEN": "github_pat_testvalue",
    }

    def test_every_builder_carries_the_keys_when_given(self):
        builders = (
            lambda **kw: runtime.payload_noop("ws", "opencode", "fresh", "s3", 1, **kw),
            lambda **kw: runtime.payload_mark_interactive("ws", "opencode", True, "s3", 1, **kw),
            lambda **kw: runtime.payload_prepare_run("ws", "opencode", "s3", 1, "m", **kw),
            lambda **kw: runtime.payload_checkpoint("ws", "opencode", "s3", 1, **kw),
            lambda **kw: runtime.payload_task("ws", "opencode", "hi", False, 30, "s3", 1, "m", **kw),
            lambda **kw: runtime.payload_info("ws", "opencode", "s3", 1, **kw),
            lambda **kw: runtime.payload_serve_ensure("ws", "opencode", "s3", 1, **kw),
            lambda **kw: runtime.payload_git_seed("ws", "opencode", "b", "s3", 1, **kw),
            lambda **kw: runtime.payload_git_snapshot("ws", "opencode", "s3", 1, **kw),
            lambda **kw: runtime.payload_session_import("ws", "opencode", "s3", 1, **kw),
        )
        for build in builders:
            with self.subTest(payload=json.loads(build())["action"]):
                data = json.loads(build(provider_keys=self.KEYS))
                self.assertEqual(data["provider_keys"], self.KEYS)
                # And without them the field is ABSENT, not empty: the shim
                # reads absence as "this user configured no keys".
                self.assertNotIn("provider_keys", json.loads(build()))
                self.assertNotIn(
                    "provider_keys", json.loads(build(provider_keys={}))
                )

    def test_payload_without_keys_is_byte_identical_to_the_legacy_shape(self):
        self.assertEqual(
            runtime.payload_noop("ws", "opencode", "fresh", "s3", 2),
            runtime.payload_noop("ws", "opencode", "fresh", "s3", 2, provider_keys=None),
        )

    def test_builders_enforce_the_allowlist(self):
        data = json.loads(runtime.payload_noop(
            "ws", "opencode", "fresh",
            provider_keys={
                "AWS_SECRET_ACCESS_KEY": "nope",
                "OPENROUTER_API_KEY": OPENROUTER_KEY,
                "KILO_API_KEY": "",
            },
        ))
        self.assertEqual(data["provider_keys"], {"OPENROUTER_API_KEY": OPENROUTER_KEY})


class _FakeCfg:
    region = "eu-west-1"

    def __init__(self, keys=None):
        self.provider_keys = dict(keys or {})


class InvokeCoverageTests(unittest.TestCase):
    """Coverage is structural: it lives in the two invocation wrappers."""

    KEYS = {"OPENROUTER_API_KEY": OPENROUTER_KEY}

    def _captured_payload(self, invoker, returncode=0):
        seen = {}

        def fake_run(argv, **kwargs):
            seen["payload"] = argv[argv.index("--payload") + 1]
            return subprocess.CompletedProcess(argv, returncode, "", "")

        with patch.object(runtime, "runtime_arn", return_value="arn:aws:x"), \
             patch.object(runtime.subprocess, "run", side_effect=fake_run):
            invoker()
        return json.loads(seen["payload"])

    def test_verified_invoke_injects_the_keys(self):
        cfg = _FakeCfg(self.KEYS)
        data = self._captured_payload(
            lambda: runtime.invoke_verified(
                cfg, "sid", runtime.payload_noop("ws", "opencode", "fresh"), "op"
            )
        )
        self.assertEqual(data["provider_keys"], self.KEYS)

    def test_best_effort_invoke_injects_the_keys(self):
        cfg = _FakeCfg(self.KEYS)
        data = self._captured_payload(
            lambda: runtime.invoke_best_effort(
                cfg, "sid", runtime.payload_mark_interactive("ws", "opencode", True)
            )
        )
        self.assertEqual(data["provider_keys"], self.KEYS)

    def test_no_keys_configured_leaves_the_payload_untouched(self):
        cfg = _FakeCfg({})
        payload = runtime.payload_noop("ws", "opencode", "fresh")
        with patch.object(runtime, "runtime_arn", return_value="arn:aws:x"):
            self.assertEqual(runtime._inject_provider_keys(cfg, payload), payload)

    def test_injection_never_overwrites_an_explicit_field(self):
        cfg = _FakeCfg({"KILO_API_KEY": "from-cfg"})
        payload = runtime.payload_noop(
            "ws", "opencode", "fresh", provider_keys={"KILO_API_KEY": "explicit"}
        )
        data = json.loads(runtime._inject_provider_keys(cfg, payload))
        self.assertEqual(data["provider_keys"], {"KILO_API_KEY": "explicit"})

    def test_injection_degrades_instead_of_raising(self):
        for cfg in (object(), _FakeCfg({"KILO_API_KEY": "k"})):
            payload = "not json at all"
            self.assertEqual(runtime._inject_provider_keys(cfg, payload), payload)

    def test_no_payload_builder_call_site_bypasses_the_wrappers(self):
        """Structural check behind "nessun path resta scoperto" (task 1.3).

        Every ``payload_*`` value built inside ``cli/sch`` must be handed to
        ``invoke_verified``/``invoke_best_effort`` — the two functions that
        inject the keys. A future command that invoked the runtime some other
        way (raw subprocess, a new helper) would fail here instead of silently
        clearing the staged key set.
        """
        import ast

        wrappers = ("invoke_verified", "invoke_best_effort", "_invoke")

        def call_name(node):
            func = node.func
            if isinstance(func, ast.Attribute):
                return func.attr
            return getattr(func, "id", "") or ""

        offenders = []
        for path in sorted((REPO_ROOT / "cli" / "sch").rglob("*.py")):
            if path.name == "runtime.py":
                continue
            tree = ast.parse(path.read_text(), filename=str(path))
            calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
            wrapper_calls = [n for n in calls if call_name(n) in wrappers]
            # Payload values handed to a wrapper directly...
            direct = {id(arg) for call in wrapper_calls for arg in call.args}
            # ...or through a local variable that is.
            forwarded = {
                arg.id for call in wrapper_calls for arg in call.args
                if isinstance(arg, ast.Name)
            }
            via_name = set()
            for node in ast.walk(tree):
                if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
                    continue
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
                if any(t in forwarded for t in targets):
                    via_name.add(id(node.value))
            for node in calls:
                if not call_name(node).startswith("payload_"):
                    continue
                if id(node) in direct or id(node) in via_name:
                    continue
                offenders.append("{}:{} {}".format(path.name, node.lineno, call_name(node)))
        self.assertEqual(offenders, [], "payload built outside an invoke wrapper")


class SecrecyTests(unittest.TestCase):
    """No key value in any CLI output — not even on a failed invocation."""

    KEYS = {"ANTHROPIC_API_KEY": ANTHROPIC_KEY, "OPENROUTER_API_KEY": OPENROUTER_KEY}

    def _run_failing_invoke(self, returncode=255, exc=None):
        cfg = _FakeCfg(self.KEYS)
        payload = runtime.payload_task("ws", "opencode", "hi", False, 30)

        def fake_run(argv, **kwargs):
            if exc is not None:
                raise exc
            # A real failure writes the aws CLI error on the process' stderr,
            # which the wrapper redirects to DEVNULL; nothing echoes the argv.
            return subprocess.CompletedProcess(argv, returncode, "", "")

        out, err = io.StringIO(), io.StringIO()
        with patch.object(runtime, "runtime_arn", return_value="arn:aws:x"), \
             patch.object(runtime.subprocess, "run", side_effect=fake_run), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = runtime.invoke_verified(cfg, "sid", payload, "task")
        return result, out.getvalue() + err.getvalue()

    def test_failed_invocation_reveals_no_key(self):
        result, output = self._run_failing_invoke()
        self.assertFalse(result.ok)
        # `raw_text` is what commands print when they report a failure.
        self.assertEqual(result.raw_text, "")
        for value in self.KEYS.values():
            self.assertNotIn(value, result.raw_text)
            self.assertNotIn(value, output)

    def test_missing_aws_cli_reveals_no_key(self):
        result, output = self._run_failing_invoke(exc=FileNotFoundError("aws"))
        self.assertFalse(result.ok)
        self.assertEqual(result.raw_text, "")
        for value in self.KEYS.values():
            self.assertNotIn(value, output)

    def test_storage_verification_error_reveals_no_key(self):
        result, _ = self._run_failing_invoke()
        message = runtime.storage_verification_error(result, "s3")
        self.assertTrue(message)
        for value in self.KEYS.values():
            self.assertNotIn(value, message)

    def test_permission_warning_reveals_no_key(self):
        if os.name != "posix":
            self.skipTest("permission bits are POSIX-only")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "env"
            path.write_text(f"ANTHROPIC_API_KEY={ANTHROPIC_KEY}\n")
            os.chmod(path, 0o644)
            warnings = []
            userenv.load_provider_keys(path=path, warn=warnings.append)
        self.assertTrue(warnings)
        for message in warnings:
            self.assertNotIn(ANTHROPIC_KEY, message)


class InfoProviderKeysTests(unittest.TestCase):
    """`sch info` reports the configured key NAMES (never values) and the env
    path, so a misplaced or empty `~/.sch/env` file is visible instead of
    silently leaving the session Bedrock-only (TASK-17)."""

    KEYS = {"ANTHROPIC_API_KEY": ANTHROPIC_KEY, "OPENROUTER_API_KEY": OPENROUTER_KEY}

    def _run_info(self, env_text=None, mode=0o600):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".sch" / "env"
            if env_text is not None:
                env_file.parent.mkdir(parents=True)
                env_file.write_text(env_text)
                os.chmod(env_file, mode)
            saved = {name: os.environ.get(name)
                     for name in ("HOME", "USERPROFILE", "SCH_RUNTIME_ARN")}
            os.environ["HOME"] = tmp
            os.environ.pop("USERPROFILE", None)
            os.environ["SCH_RUNTIME_ARN"] = "arn:aws:bedrock:test"
            userenv.reset_cache()
            self.addCleanup(userenv.reset_cache)
            try:
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    from sch.commands import info
                    info.cmd_info(Config(), None)
            finally:
                for name, value in saved.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value
        return out.getvalue()

    def test_info_lists_configured_key_names(self):
        output = self._run_info(
            "\n".join("{}={}".format(name, value)
                      for name, value in self.KEYS.items()) + "\n"
        )
        self.assertIn("provider keys: ANTHROPIC_API_KEY, OPENROUTER_API_KEY", output)
        self.assertIn(str(Path(".sch") / "env"), output)
        for value in self.KEYS.values():
            self.assertNotIn(value, output)

    def test_info_lists_the_bedrock_api_key_like_the_others(self):
        # TASK-19: the cross-account Bedrock key is reported by name like the
        # external provider keys.
        output = self._run_info(f"BEDROCK_API_KEY={BEARER_TOKEN}\n")
        self.assertIn("provider keys: BEDROCK_API_KEY", output)
        self.assertNotIn(BEARER_TOKEN, output)

    def test_info_reports_none_and_path_hint_when_file_missing(self):
        output = self._run_info(env_text=None)
        self.assertIn("provider keys: none", output)
        self.assertIn(str(Path(".sch") / "env"), output)
        for value in self.KEYS.values():
            self.assertNotIn(value, output)


if __name__ == "__main__":
    unittest.main()
