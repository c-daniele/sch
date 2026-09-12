"""Offline tests for per-harness headless argument construction."""

from __future__ import annotations

import __future__
import asyncio
import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


class FakeApp:
    def entrypoint(self, func):
        return func

    def websocket(self, func):
        return func

    def run(self, *_args, **_kwargs):
        return None


def load_main():
    app_dir = Path(__file__).parent
    sys.path.insert(0, str(app_dir))
    bedrock = types.ModuleType("bedrock_agentcore")
    bedrock.BedrockAgentCoreApp = FakeApp
    boto3 = types.ModuleType("boto3")
    boto3.client = lambda *_args, **_kwargs: None
    spec = importlib.util.spec_from_file_location("sch_headless_argv_main", app_dir / "main.py")
    module = importlib.util.module_from_spec(spec)
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    with patch.dict(sys.modules, {"bedrock_agentcore": bedrock, "boto3": boto3}), patch(
        "threading.Thread"
    ):
        source = (app_dir / "main.py").read_text(encoding="utf-8")
        code = compile(
            source,
            str(app_dir / "main.py"),
            "exec",
            flags=__future__.annotations.compiler_flag,
            dont_inherit=True,
        )
        exec(code, module.__dict__)
    return module


main = load_main()


class HeadlessArgvTests(unittest.TestCase):
    def setUp(self):
        self.original_agent = main._TASK_AGENT
        self.original_flags = list(main._TASK_AUTO_APPROVE_FLAGS)
        main._TASK_AGENT = "remote-auto"
        main._TASK_AUTO_APPROVE_FLAGS = ["--auto"]

    def tearDown(self):
        main._TASK_AGENT = self.original_agent
        main._TASK_AUTO_APPROVE_FLAGS = self.original_flags

    def test_fresh_claude_task_selects_remote_auto(self):
        self.assertEqual(
            main._build_headless_argv("claude", None, "build and test"),
            [
                "claude", "-p", "--agent", "remote-auto",
                "--dangerously-skip-permissions", "build and test",
            ],
        )

    def test_resumed_claude_task_keeps_safe_argument_order(self):
        self.assertEqual(
            main._build_headless_argv("claude", "session-id", "continue"),
            [
                "claude", "-p", "--resume", "session-id", "--agent",
                "remote-auto", "--dangerously-skip-permissions", "continue",
            ],
        )

    def test_custom_claude_agent_is_forwarded_without_interpretation(self):
        main._TASK_AGENT = "custom-auto"
        argv = main._build_headless_argv("claude", None, "prompt; not shell")
        self.assertEqual(argv[2:4], ["--agent", "custom-auto"])
        self.assertEqual(argv[-1], "prompt; not shell")

    def test_empty_claude_agent_omits_only_agent_flag(self):
        main._TASK_AGENT = ""
        argv = main._build_headless_argv("claude", None, "prompt")
        self.assertNotIn("--agent", argv)
        self.assertIn("--dangerously-skip-permissions", argv)

    def test_opencode_argv_is_unchanged(self):
        with patch.object(main.shutil, "which", return_value="/bin/opencode"):
            self.assertEqual(
                main._build_headless_argv("opencode", "oc-session", "prompt"),
                [
                    "/bin/opencode", "run", "--session", "oc-session",
                    "--agent", "remote-auto", "--auto", "prompt",
                ],
            )

    def test_headless_environment_sets_origin_and_strips_telegram_secrets(self):
        secrets = {
            "SCH_TELEGRAM_BOT_TOKEN": "token",
            "SCH_TELEGRAM_CHAT_ID": "chat",
            "SCH_TELEGRAM_COMMANDS_TABLE": "commands",
            "SCH_TELEGRAM_ROUTING_TABLE": "routing",
            "SCH_TELEGRAM_NOTIFICATIONS_CONFIGURED": "1",
        }
        with patch.dict(os.environ, secrets, clear=False):
            env = main._headless_harness_env("claude")
        self.assertEqual(env["SCH_HARNESS"], "claude")
        self.assertEqual(env["SCH_EXECUTION_MODE"], "headless")
        for name in secrets:
            self.assertNotIn(name, env)


class PiHeadlessArgvTests(unittest.TestCase):
    """add-pi-harness task 4.8: the pi argv branch (design D3/D4)."""

    def setUp(self):
        self.original_agent = main._TASK_AGENT
        main._TASK_AGENT = "remote-auto"
        # The role file is a real seeded artifact: patch is_file() rather than
        # creating one, so the tests stay filesystem-free.
        self.which = patch.object(main.shutil, "which", return_value="/bin/pi")
        self.which.start()
        self.addCleanup(self.which.stop)

    def tearDown(self):
        main._TASK_AGENT = self.original_agent

    def _argv(self, *args, role_present: bool = True, **kwargs):
        with patch.object(type(main.PI_ROLE_REMOTE_AUTO), "is_file", return_value=role_present):
            return main._build_headless_argv("pi", *args, **kwargs)

    def test_fresh_pi_task_uses_print_mode_and_the_remote_auto_role(self):
        self.assertEqual(
            self._argv(None, "build and test"),
            [
                "/bin/pi", "-p",
                "--append-system-prompt", str(main.PI_ROLE_REMOTE_AUTO),
                "build and test",
            ],
        )

    def test_pi_argv_carries_no_auto_approval_flag(self):
        """Pi has no permission prompt by design, so unattended execution is
        satisfied by construction — the absence of a flag is the contract, not a
        gap (spec: headless-task-execution)."""
        argv = self._argv(None, "prompt")
        for flag in ("--auto", "--dangerously-skip-permissions", "--yes", "--approve", "-a"):
            self.assertNotIn(flag, argv)

    def test_resumed_pi_task_passes_the_session_reference(self):
        session = "/home/sch/.pi/agent/sessions/--mnt-workspace-repo--/ts_uuid.jsonl"
        self.assertEqual(
            self._argv(session, "continue"),
            [
                "/bin/pi", "-p", "--session", session,
                "--append-system-prompt", str(main.PI_ROLE_REMOTE_AUTO),
                "continue",
            ],
        )

    def test_model_is_forwarded_as_a_provider_model_pair(self):
        argv = self._argv(None, "prompt", "eu.anthropic.claude-sonnet-4-6")
        self.assertEqual(
            argv,
            [
                "/bin/pi", "-p",
                "--provider", "amazon-bedrock",
                "--model", "eu.anthropic.claude-sonnet-4-6",
                "--append-system-prompt", str(main.PI_ROLE_REMOTE_AUTO),
                "prompt",
            ],
        )

    def test_model_id_is_opaque_and_never_shell_interpolated(self):
        argv = self._argv(None, "prompt; rm -rf /", "vendor/model:v1 ; echo x")
        self.assertIn("vendor/model:v1 ; echo x", argv)
        self.assertEqual(argv[-1], "prompt; rm -rf /")

    def test_without_a_model_the_provider_pair_is_absent(self):
        argv = self._argv(None, "prompt")
        self.assertNotIn("--provider", argv)
        self.assertNotIn("--model", argv)

    def test_missing_role_file_degrades_instead_of_failing(self):
        argv = self._argv(None, "prompt", role_present=False)
        self.assertNotIn("--append-system-prompt", argv)
        self.assertEqual(argv, ["/bin/pi", "-p", "prompt"])

    def test_empty_task_agent_omits_the_role_prompt(self):
        main._TASK_AGENT = ""
        argv = self._argv(None, "prompt")
        self.assertNotIn("--append-system-prompt", argv)


class PiSessionResolverTests(unittest.TestCase):
    """add-pi-harness task 4.8: the JSONL session resolver (design D5)."""

    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "mnt" / "workspace" / "repo"
        self.repo.mkdir(parents=True)
        self.config = self.root / "pi-agent"
        self._patches = [
            patch.object(main, "PI_CONFIG_DIR_LOCAL", self.config),
            patch.object(main, "REPO_DIR", self.repo),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def _sessions_dir(self) -> Path:
        directory = self.config / "sessions" / main._pi_session_dir_name(self.repo)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _write_session(self, name: str, *, session_id: str = "abc", mtime: float | None = None,
                       header: str | None = None) -> Path:
        import json as _json
        import os as _os

        path = self._sessions_dir() / name
        if header is None:
            header = _json.dumps({
                "type": "session", "version": 3, "id": session_id, "cwd": str(self.repo),
            })
        path.write_text(header + "\n", encoding="utf-8")
        if mtime is not None:
            _os.utime(path, (mtime, mtime))
        return path

    def test_cwd_encoding_matches_pi(self):
        """Verified against pi 0.84.2 dist/core/session-manager.js: leading
        separator dropped, `/`, `\\` and `:` mapped to `-`, wrapped in `--`."""
        self.assertEqual(
            main._pi_session_dir_name("/mnt/workspace/repo"),
            "--mnt-workspace-repo--",
        )
        self.assertEqual(main._pi_session_dir_name("/tmp/pi-probe"), "--tmp-pi-probe--")

    def test_latest_session_by_mtime_is_returned_as_a_path(self):
        self._write_session("2026-01-01_old.jsonl", session_id="old", mtime=1000)
        newest = self._write_session("2026-02-02_new.jsonl", session_id="new", mtime=2000)
        self.assertEqual(main._resolve_latest_pi_session(), str(newest))

    def test_no_sessions_directory_degrades_to_a_fresh_session(self):
        self.assertIsNone(main._resolve_latest_pi_session())

    def test_empty_sessions_directory_degrades_to_a_fresh_session(self):
        self._sessions_dir()
        self.assertIsNone(main._resolve_latest_pi_session())

    def test_malformed_header_is_skipped_in_favor_of_a_valid_older_file(self):
        valid = self._write_session("older.jsonl", session_id="ok", mtime=1000)
        self._write_session("newer.jsonl", mtime=2000, header="{ not json")
        self.assertEqual(main._resolve_latest_pi_session(), str(valid))

    def test_foreign_header_type_is_not_resumed(self):
        self._write_session("newer.jsonl", mtime=2000, header='{"type":"other"}')
        self.assertIsNone(main._resolve_latest_pi_session())

    def test_header_without_an_id_is_not_resumed(self):
        self._write_session("newer.jsonl", mtime=2000, header='{"type":"session","version":3}')
        self.assertIsNone(main._resolve_latest_pi_session())

    def test_header_with_wrong_version_is_not_resumed(self):
        self._write_session(
            "newer.jsonl",
            mtime=2000,
            header='{"type":"session","version":2,"id":"old","cwd":"%s"}' % self.repo,
        )
        self.assertIsNone(main._resolve_latest_pi_session())

    def test_colliding_directory_with_foreign_cwd_is_not_resumed(self):
        self._write_session(
            "newer.jsonl",
            mtime=2000,
            header='{"type":"session","version":3,"id":"foreign","cwd":"/other/repo"}',
        )
        self.assertIsNone(main._resolve_latest_pi_session())

    def test_sessions_of_another_worktree_are_ignored(self):
        other = self.config / "sessions" / main._pi_session_dir_name("/some/other/repo")
        other.mkdir(parents=True)
        (other / "s.jsonl").write_text('{"type":"session","version":3,"id":"x"}\n')
        self.assertIsNone(main._resolve_latest_pi_session())

    def test_resolver_dispatch_selects_the_pi_implementation(self):
        newest = self._write_session("s.jsonl", session_id="dispatch", mtime=2000)
        self.assertEqual(main._resolve_latest_harness_session("pi"), str(newest))


class PiReadyMarkerTests(unittest.TestCase):
    def test_each_harness_gates_on_its_own_marker(self):
        self.assertEqual(main._harness_ready_marker("pi"), main.PI_READY_MARKER)
        self.assertEqual(main._harness_ready_marker("claude"), main.CLAUDE_READY_MARKER)
        self.assertEqual(main._harness_ready_marker("opencode"), main.READY_MARKER)
        # Markers must be distinct: a workspace seeded for one harness must not
        # unblock another harness's binary.
        markers = {
            main._harness_ready_marker(h) for h in ("pi", "claude", "opencode")
        }
        self.assertEqual(len(markers), 3)

    def test_pi_is_a_supported_harness(self):
        self.assertIn("pi", main.SUPPORTED_HARNESSES)
        # The default is unchanged: pi is additive (design goal "zero regressions").
        self.assertEqual(main.DEFAULT_HARNESS, "opencode")


if __name__ == "__main__":
    unittest.main()
