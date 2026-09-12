"""Tests for CommandShell presence leases and command integration."""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_CLI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _CLI_DIR not in sys.path:
    sys.path.insert(0, _CLI_DIR)

from sch import presence, procs, runtime
from sch.commands import acp as acp_cmd
from sch.commands import attach as attach_cmd
from sch.commands import run as run_cmd
from sch.commands import shell as shell_cmd
from sch.commands import web as web_cmd


class PayloadTests(unittest.TestCase):
    def test_presence_payload_shapes(self):
        self.assertEqual(
            json.loads(runtime.payload_presence("shell-1", "client-1", "attached", 30)),
            {
                "action": "command-shell-presence",
                "shellId": "shell-1",
                "attachmentId": "client-1",
                "state": "attached",
                "ttl_s": 30,
            },
        )

    def test_presence_payload_carries_runtime_identity_and_epoch(self):
        self.assertEqual(
            json.loads(runtime.payload_presence(
                "shell-1", "client-1", "attached", 30,
                "ws", "opencode", "s3", 7,
            )),
            {
                "action": "command-shell-presence",
                "shellId": "shell-1",
                "attachmentId": "client-1",
                "state": "attached",
                "ttl_s": 30,
                "workspace": "ws",
                "harness": "opencode",
                "storage_backend": "s3",
                "session_epoch": 7,
            },
        )
        self.assertEqual(
            json.loads(runtime.payload_presence("shell-1", "client-1", "detached", 30)),
            {
                "action": "command-shell-presence",
                "shellId": "shell-1",
                "attachmentId": "client-1",
                "state": "detached",
                "ttl_s": 30,
            },
        )


class PresenceLeaseTests(unittest.TestCase):
    class TwoTickStop:
        def __init__(self):
            self.calls = []

        def wait(self, timeout):
            self.calls.append(timeout)
            return len(self.calls) > 1

        def set(self):
            pass

    def test_attach_renew_and_detach_use_same_identity(self):
        calls = []
        stop = self.TwoTickStop()
        lease = presence.PresenceLease(
            "cfg", "session", "shell", attachment_id="client",
            invoke=lambda cfg, sid, payload: calls.append((cfg, sid, json.loads(payload))),
            stop_event=stop,
        )

        lease.start()
        lease._thread.join(1)
        lease.close()

        self.assertFalse(lease._thread.is_alive())
        self.assertEqual([call[2]["state"] for call in calls], [
            "attached", "attached", "detached",
        ])
        self.assertTrue(all(call[1] == "session" for call in calls))
        self.assertTrue(all(call[2]["shellId"] == "shell" for call in calls))
        self.assertTrue(all(call[2]["attachmentId"] == "client" for call in calls))
        self.assertEqual(stop.calls, [presence.PRESENCE_RENEW_INTERVAL_S] * 2)

    def test_each_local_client_gets_a_distinct_attachment(self):
        first = presence.PresenceLease("cfg", "session", "shell")
        second = presence.PresenceLease("cfg", "session", "shell")
        self.assertNotEqual(first.attachment_id, second.attachment_id)

    def test_unsupported_runtime_never_escapes_worker(self):
        def unsupported(*_args):
            raise RuntimeError("unknown action")

        stop = self.TwoTickStop()
        lease = presence.PresenceLease(
            "cfg", "session", "shell", invoke=unsupported, stop_event=stop
        )
        lease.start()
        lease._thread.join(1)
        lease.close()

    def test_thread_start_failure_does_not_prevent_cleanup(self):
        calls = []
        lease = presence.PresenceLease(
            "cfg", "session", "shell", attachment_id="client",
            invoke=lambda _cfg, _sid, payload: calls.append(json.loads(payload)),
        )
        with patch.object(presence.threading.Thread, "start", side_effect=RuntimeError):
            lease.start()
        lease.close()
        self.assertEqual([call["state"] for call in calls], ["attached", "detached"])


class ProcessOwnershipTests(unittest.TestCase):
    def test_standalone_lease_wraps_child_even_on_interrupt(self):
        events = []
        lease = MagicMock()
        lease.start.side_effect = lambda: events.append("attach")
        lease.close.side_effect = lambda: events.append("detach")
        with patch.object(procs, "_save_terminal_state", return_value=None), \
             patch.object(procs, "reset_terminal_modes"), \
             patch.object(
                 procs.subprocess, "call",
                 side_effect=lambda *_args, **_kwargs: events.append("child") or (_ for _ in ()).throw(KeyboardInterrupt),
             ):
            with self.assertRaises(SystemExit) as raised:
                procs.exec_or_wait(["client"], presence=lease)
        self.assertEqual(raised.exception.code, 130)
        self.assertEqual(events, ["attach", "child", "detach"])

    def test_sync_lease_starts_after_ready_and_closes_after_child(self):
        events = []
        helper = MagicMock()
        helper.stdout.readline.side_effect = lambda: events.append("ready") or '{"type":"ready"}\n'
        helper.poll.return_value = None
        child = MagicMock()
        child.poll.side_effect = lambda: events.append("child") or 0
        lease = MagicMock()
        lease.start.side_effect = lambda: events.append("attach")
        lease.close.side_effect = lambda: events.append("detach")
        with patch.object(procs.subprocess, "Popen", side_effect=[helper, child]), \
             patch.object(procs, "_save_terminal_state", return_value=None), \
             patch.object(procs, "reset_terminal_modes"):
            self.assertEqual(
                procs.supervise_interactive(["sync"], ["client"], presence=lease), 0
            )
        self.assertLess(events.index("ready"), events.index("attach"))
        self.assertLess(events.index("attach"), events.index("child"))
        self.assertLess(events.index("child"), events.index("detach"))

    def test_sync_spawn_error_still_detaches_registered_lease(self):
        helper = MagicMock()
        helper.stdout.readline.return_value = '{"type":"ready"}\n'
        helper.poll.return_value = None
        lease = MagicMock()
        with patch.object(
            procs.subprocess, "Popen", side_effect=[helper, OSError("cannot spawn")]
        ):
            self.assertEqual(
                procs.supervise_interactive(["sync"], ["client"], presence=lease), 1
            )
        lease.start.assert_called_once_with()
        lease.close.assert_called_once_with()


class CommandShellIdTests(unittest.TestCase):
    def setUp(self):
        self.cfg = type(
            "Config", (), {"region": "test", "ws_dir": Path("/nonexistent/sch-test-ws")}
        )()
        self.resolved = type(
            "Resolved", (), {
                "sid": "sid", "harness": "opencode", "identity": "ws",
                "storage": "session", "epoch": 0, "was_created": False,
            }
        )()
        self.warm = runtime.InvocationResult(
            True, json.dumps({"status": "ok", "storage": "session"})
        )

    def _shell(self, args, generated="generated-shell"):
        lease = MagicMock()
        with patch.object(shell_cmd.procs, "require_command"), \
             patch.object(shell_cmd.harness_mod, "resolve_harness", return_value=self.resolved), \
             patch.object(shell_cmd.sync_mod, "resolve_binding", return_value=None), \
             patch.object(shell_cmd, "runtime_arn", return_value="arn"), \
             patch.object(shell_cmd.runtime, "invoke_verified", return_value=self.warm), \
             patch.object(shell_cmd.runtime, "invoke_best_effort"), \
             patch.object(shell_cmd.presence_mod, "new_shell_id", return_value=generated), \
             patch.object(shell_cmd.presence_mod, "PresenceLease", return_value=lease) as lease_type, \
             patch.object(shell_cmd.procs, "exec_or_wait", side_effect=SystemExit(0)) as execute, \
             patch.object(shell_cmd.workspace, "mark_status"):
            with self.assertRaises(SystemExit):
                shell_cmd.cmd_shell(self.cfg, args)
        return lease, lease_type, execute

    def test_shell_generates_and_passes_explicit_shell_id(self):
        lease, lease_type, execute = self._shell(["ws"])
        argv = execute.call_args.args[0]
        self.assertEqual(argv[argv.index("--shell-id") + 1], "generated-shell")
        self.assertIs(execute.call_args.kwargs["presence"], lease)
        lease_type.assert_called_once_with(
            self.cfg, "sid", "generated-shell", "ws", "opencode", "session", 0
        )

    def test_shell_reuses_supplied_shell_id(self):
        _, lease_type, execute = self._shell(["ws", "--shell-id", "existing-shell"])
        argv = execute.call_args.args[0]
        self.assertEqual(argv[argv.index("--shell-id") + 1], "existing-shell")
        lease_type.assert_called_once_with(
            self.cfg, "sid", "existing-shell", "ws", "opencode", "session", 0
        )

    def test_shell_sync_supervisor_owns_presence_lease(self):
        lease = MagicMock()
        with patch.object(shell_cmd.procs, "require_command"), \
             patch.object(shell_cmd.harness_mod, "resolve_harness", return_value=self.resolved), \
             patch.object(shell_cmd.sync_mod, "resolve_binding", return_value="/project"), \
             patch.object(shell_cmd.sync_mod, "baseline_path", return_value="/baseline"), \
             patch.object(shell_cmd.sync_mod, "helper_argv", return_value=["sync-helper"]), \
             patch.object(shell_cmd, "runtime_arn", return_value="arn"), \
             patch.object(shell_cmd.runtime, "invoke_verified", return_value=self.warm), \
             patch.object(shell_cmd.runtime, "invoke_best_effort"), \
             patch.object(shell_cmd.presence_mod, "PresenceLease", return_value=lease), \
             patch.object(shell_cmd.procs, "supervise_interactive", return_value=0) as supervise, \
             patch.object(shell_cmd.workspace, "mark_status"):
            self.assertEqual(
                shell_cmd.cmd_shell(self.cfg, ["ws", "--shell-id", "sync-shell"]), 0
            )
        self.assertEqual(
            supervise.call_args.args[1][
                supervise.call_args.args[1].index("--shell-id") + 1
            ],
            "sync-shell",
        )
        self.assertIs(supervise.call_args.kwargs["presence"], lease)

    def test_run_generates_shell_id_and_assigns_lease_to_command_child(self):
        lease = MagicMock()
        responses = [
            self.warm,
            runtime.InvocationResult(True, json.dumps({"status": "ok"})),
        ]
        with patch.object(run_cmd.procs, "require_command"), \
             patch.object(run_cmd.harness_mod, "resolve_harness", return_value=self.resolved), \
             patch.object(run_cmd.gitnative, "resolve_mode", return_value=None), \
             patch.object(run_cmd.sync_mod, "resolve_binding", return_value=None), \
             patch.object(run_cmd, "runtime_arn", return_value="arn"), \
             patch.object(run_cmd.runtime, "invoke_verified", side_effect=responses), \
             patch.object(run_cmd.runtime, "invoke_best_effort"), \
             patch.object(run_cmd.presence_mod, "new_shell_id", return_value="run-shell"), \
             patch.object(run_cmd.presence_mod, "PresenceLease", return_value=lease) as lease_type, \
             patch.object(run_cmd.procs, "exec_or_wait", side_effect=SystemExit(0)) as execute, \
             patch.object(run_cmd.workspace, "mark_status"):
            with self.assertRaises(SystemExit):
                run_cmd.cmd_run(self.cfg, ["ws", "--no-sync"])
        argv = execute.call_args.args[0]
        self.assertEqual(argv[argv.index("--shell-id") + 1], "run-shell")
        self.assertIs(execute.call_args.kwargs["presence"], lease)
        lease_type.assert_called_once_with(
            self.cfg, "sid", "run-shell", "ws", "opencode", "session", 0
        )

    def test_non_command_shell_commands_do_not_use_presence_leases(self):
        for module in (web_cmd, attach_cmd, acp_cmd):
            with self.subTest(module=module.__name__):
                self.assertNotIn("PresenceLease", vars(module))


if __name__ == "__main__":
    unittest.main()
