"""Focused tests for the CommandShell presence lease registry and action."""

from __future__ import annotations

import __future__
import asyncio
import importlib.util
import json
import sys
import tempfile
import threading
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


def load_main(snapshot_path):
    app_dir = Path(__file__).parent
    sys.path.insert(0, str(app_dir))
    bedrock = types.ModuleType("bedrock_agentcore")
    bedrock.BedrockAgentCoreApp = FakeApp
    boto3 = types.ModuleType("boto3")
    boto3.client = lambda *_args, **_kwargs: None
    module_name = "sch_command_shell_presence_main"
    spec = importlib.util.spec_from_file_location(module_name, app_dir / "main.py")
    module = importlib.util.module_from_spec(spec)
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    with patch.dict(
        sys.modules,
        {module_name: module, "bedrock_agentcore": bedrock, "boto3": boto3},
    ), patch.dict(
        "os.environ", {"SCH_COMMAND_SHELL_PRESENCE_FILE": str(snapshot_path)}
    ), patch("threading.Thread"):
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


class Clock:
    def __init__(self, now=1_000.0):
        self.now = now

    def __call__(self):
        return self.now


class CommandShellPresenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module_tmp = tempfile.TemporaryDirectory()
        cls.main = load_main(Path(cls.module_tmp.name) / "module-presence.json")

    @classmethod
    def tearDownClass(cls):
        cls.module_tmp.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.snapshot_path = Path(self.tmp.name) / "presence.json"
        self.clock = Clock()
        self.registry = self.main.CommandShellPresenceRegistry(
            self.snapshot_path, now_fn=self.clock
        )

    def update(self, shell="shell-1", attachment="client-1", state="attached", ttl=30):
        return self.registry.update(shell, attachment, state, ttl)

    def test_first_attach_and_idempotent_renewal(self):
        result = self.update()
        self.assertTrue(result["attached"])
        self.assertEqual(result["lease_count"], 1)
        first = self.registry.snapshot()["leases"][0]

        self.clock.now += 10
        renewed = self.update()
        lease = self.registry.snapshot()["leases"][0]
        self.assertEqual(renewed["lease_count"], 1)
        self.assertEqual(lease["attached_at"], first["attached_at"])
        self.assertGreater(lease["expires_at"], first["expires_at"])
        self.assertEqual(self.registry.snapshot()["history"][-1]["reason"], "attached")

    def test_renewal_churn_preserves_attached_history_baseline(self):
        registry = self.main.CommandShellPresenceRegistry(
            self.snapshot_path, history_max=2, now_fn=self.clock
        )
        registry.update("shell", "one", "attached", 30)
        for _ in range(10):
            self.clock.now += 1
            registry.update("shell", "one", "attached", 30)
        self.assertEqual(len(registry.snapshot()["history"]), 1)
        self.assertTrue(registry.attached_at_emission(1_005.0))

    def test_explicit_detach_and_reconnect_preserve_history(self):
        self.update()
        self.clock.now += 5
        self.update(state="detached")
        self.assertFalse(self.registry.current_attached())
        self.clock.now += 5
        self.update(attachment="client-2")

        self.assertTrue(self.registry.attached_at_emission(1_002.0))
        self.assertFalse(self.registry.attached_at_emission(1_007.0))
        self.assertTrue(self.registry.attached_at_emission(1_010.0))

    def test_stale_expiry_updates_snapshot(self):
        self.update(ttl=5)
        self.clock.now += 6
        self.assertTrue(self.registry.expire_stale())
        self.assertFalse(self.registry.current_attached())
        snapshot = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        self.assertEqual(snapshot["state"], "detached")
        self.assertIsNone(snapshot["expires_at"])
        self.assertEqual(snapshot["history"][-1]["reason"], "expired")

    def test_multi_client_detach_is_selective(self):
        self.update(attachment="client-1")
        self.update(attachment="client-2")
        result = self.update(attachment="client-1", state="detached")
        self.assertTrue(result["attached"])
        self.assertEqual(result["lease_count"], 1)
        self.assertEqual(
            self.registry.snapshot()["leases"][0]["attachmentId"], "client-2"
        )

    def test_attach_callback_runs_only_on_detached_to_attached_transitions(self):
        transitions = []
        self.registry.set_on_attached(lambda: transitions.append(self.clock.now))
        self.update(attachment="client-1")
        self.clock.now += 1
        self.update(attachment="client-1")
        self.update(attachment="client-2")
        self.update(attachment="client-1", state="detached")
        self.update(attachment="client-2", state="detached")
        self.clock.now += 1
        self.update(attachment="client-3")
        self.assertEqual(transitions, [1_000.0, 1_002.0])

    def test_invalid_inputs_are_rejected(self):
        invalid = (
            ("bad shell", "client", "attached", 30),
            ("shell", "", "attached", 30),
            ("shell", "client", "present", 30),
            ("shell", "client", "attached", 0),
            ("shell", "client", "attached", True),
            ("shell", "client", "attached", "30"),
        )
        for args in invalid:
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.registry.update(*args)

    def test_ttl_is_clamped_to_bounds(self):
        low = self.update(ttl=1)
        high = self.update(attachment="client-2", ttl=10_000)
        self.assertEqual(low["ttl_s"], self.main.COMMAND_SHELL_PRESENCE_TTL_MIN_S)
        self.assertEqual(high["ttl_s"], self.main.COMMAND_SHELL_PRESENCE_TTL_MAX_S)

    def test_registry_and_history_are_bounded(self):
        registry = self.main.CommandShellPresenceRegistry(
            self.snapshot_path,
            max_leases=2,
            history_max=3,
            now_fn=self.clock,
        )
        registry.update("shell", "one", "attached", 30)
        registry.update("shell", "two", "attached", 30)
        with self.assertRaisesRegex(ValueError, "registry is full"):
            registry.update("shell", "three", "attached", 30)
        for _ in range(5):
            self.clock.now += 1
            registry.update("shell", "one", "attached", 30)
        self.assertEqual(len(registry.snapshot()["leases"]), 2)
        self.assertLessEqual(len(registry.snapshot()["history"]), 3)

        self.clock.now += (
            registry.history_retention_s
            + self.main.COMMAND_SHELL_PRESENCE_TTL_MAX_S
            + 1
        )
        registry.expire_stale()
        history = registry.snapshot()["history"]
        self.assertLessEqual(len(history), 1)
        if history:
            self.assertEqual(history[0]["reason"], "retained-baseline")
            self.assertEqual(history[0]["state"], "detached")

    def test_snapshot_is_atomically_replaced_without_temp_files(self):
        with patch.object(
            self.main.os, "replace", wraps=self.main.os.replace
        ) as replace:
            self.update()
        replace.assert_called_once()
        self.assertEqual(Path(replace.call_args.args[1]), self.snapshot_path)
        self.assertEqual(list(self.snapshot_path.parent.glob("*.tmp")), [])

    def test_concurrent_updates_do_not_lose_leases(self):
        registry = self.main.CommandShellPresenceRegistry(
            self.snapshot_path, max_leases=32, now_fn=self.clock
        )
        threads = [
            threading.Thread(
                target=registry.update,
                args=("shell", f"client-{index}", "attached", 30),
            )
            for index in range(20)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(registry.snapshot()["leases"]), 20)

    def test_snapshot_reader_fails_open_to_detached(self):
        self.assertFalse(
            self.main.command_shell_snapshot_attached(
                Path(self.tmp.name) / "missing.json", now=self.clock.now
            )
        )
        self.snapshot_path.write_text("not-json", encoding="utf-8")
        self.assertFalse(
            self.main.command_shell_snapshot_attached(self.snapshot_path, now=self.clock.now)
        )
        self.snapshot_path.write_text(
            json.dumps({"version": 1, "state": "attached", "expires_at": 2_000}),
            encoding="utf-8",
        )
        self.assertFalse(
            self.main.command_shell_snapshot_attached(self.snapshot_path, now=self.clock.now)
        )
        self.update(ttl=5)
        self.assertTrue(
            self.main.command_shell_snapshot_attached(self.snapshot_path, now=self.clock.now)
        )
        self.assertFalse(
            self.main.command_shell_snapshot_attached(
                self.snapshot_path, now=self.clock.now + 6
            )
        )

    def test_dispatch_action_and_interactive_toggle_are_independent(self):
        original = self.main._COMMAND_SHELL_PRESENCE
        original_interactive = self.main._INTERACTIVE_ACTIVE
        self.main._COMMAND_SHELL_PRESENCE = self.registry
        self.addCleanup(
            lambda: setattr(self.main, "_COMMAND_SHELL_PRESENCE", original)
        )
        self.addCleanup(
            lambda: setattr(self.main, "_INTERACTIVE_ACTIVE", original_interactive)
        )

        self.main.invoke({"action": "mark-interactive", "active": True})
        self.assertFalse(self.main.command_shell_current_attached())
        response = self.main.invoke({
            "action": "command-shell-presence",
            "shellId": "shell-1",
            "attachmentId": "client-1",
            "state": "attached",
            "ttl_s": 30,
        })
        self.assertEqual(response["status"], "ok")
        self.assertTrue(response["attached"])
        self.assertTrue(self.main._INTERACTIVE_ACTIVE)

        rejected = self.main.invoke({
            "action": "command-shell-presence",
            "shellId": "shell-1",
            "attachmentId": "client-1",
            "state": "invalid",
            "ttl_s": 30,
        })
        self.assertEqual(rejected["status"], "rejected")
        self.assertTrue(self.main.command_shell_current_attached())


if __name__ == "__main__":
    unittest.main()
