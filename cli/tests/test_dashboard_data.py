"""Tests for dashboard process handoff and read-only data services."""

import contextlib
import io
import json
import os
import queue
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

_CLI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _CLI_DIR not in sys.path:
    sys.path.insert(0, _CLI_DIR)

from sch import dashboard, procs, runtime, workspace, workspace_registry
from sch.commands import list as list_cmd
from sch.commands import status as status_cmd


def _cfg(tmp_dir):
    root = Path(tmp_dir)
    return SimpleNamespace(
        ws_dir=root / "workspaces",
        config_dir=root,
        checkpoint_bucket_cache=root / "bucket",
        checkpoint_bucket_override="test-bucket",
        region="eu-west-1",
        workspace_registry_url="",
    )


def _record(name, runtime_workspace=None):
    return list_cmd.WorkspaceRecord(
        name=name,
        harness="opencode",
        storage="s3",
        sid="sid-{}".format(name),
        status="created",
        runtime_workspace=runtime_workspace or name,
    )


class ForegroundHandoffTests(unittest.TestCase):
    def test_restores_terminal_after_success(self):
        events = []
        with patch.object(procs, "_save_terminal_state", return_value="state") as save, \
             patch.object(procs, "_restore_terminal_state", side_effect=lambda value: events.append(value)), \
             patch.object(procs.subprocess, "call", return_value=0) as child:
            self.assertEqual(procs.foreground_handoff(["client", "arg"]), 0)
        save.assert_called_once_with()
        child.assert_called_once_with(["client", "arg"])
        self.assertEqual(events, ["state"])

    def test_restores_terminal_after_nonzero_exit(self):
        with patch.object(procs, "_save_terminal_state", return_value="state"), \
             patch.object(procs, "_restore_terminal_state") as restore, \
             patch.object(procs.subprocess, "call", return_value=19):
            self.assertEqual(procs.foreground_handoff(["client"]), 19)
        restore.assert_called_once_with("state")

    def test_restores_terminal_after_spawn_exception(self):
        error = OSError("cannot spawn")
        with patch.object(procs, "_save_terminal_state", return_value="state"), \
             patch.object(procs, "_restore_terminal_state") as restore, \
             patch.object(procs.subprocess, "call", side_effect=error):
            with self.assertRaisesRegex(OSError, "cannot spawn"):
                procs.foreground_handoff(["client"])
        restore.assert_called_once_with("state")

    def test_posix_standalone_waits_restores_and_resets_terminal(self):
        events = []
        with patch.object(procs.os, "name", "posix"), \
             patch.object(procs, "_save_terminal_state", return_value="state"), \
             patch.object(procs, "_restore_terminal_state", side_effect=lambda value: events.append(("restore", value))), \
             patch.object(procs, "reset_terminal_modes", side_effect=lambda: events.append(("reset",))), \
             patch.object(procs.subprocess, "call", return_value=5) as child:
            with self.assertRaises(SystemExit) as raised:
                procs.exec_or_wait(["agentcore", "exec"])
        self.assertEqual(raised.exception.code, 5)
        child.assert_called_once_with(["agentcore", "exec"])
        self.assertEqual(events, [("restore", "state"), ("reset",)])

    def test_missing_client_still_resets_terminal(self):
        with patch.object(procs.os, "name", "posix"), \
             patch.object(procs, "_save_terminal_state", return_value="state"), \
             patch.object(procs, "_restore_terminal_state") as restore, \
             patch.object(procs, "reset_terminal_modes") as reset, \
             patch.object(procs.subprocess, "call", side_effect=FileNotFoundError), \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                procs.exec_or_wait(["agentcore", "exec"])
        self.assertEqual(raised.exception.code, 1)
        restore.assert_called_once_with("state")
        reset.assert_called_once_with()

    def test_terminal_mode_reset_disables_mouse_and_paste_modes(self):
        # Keep in lockstep with tunnel/attach.js restoreTerminal().
        for mode in ("1000", "1002", "1003", "1004", "1006", "1015", "2004"):
            self.assertIn("\x1b[?{}l".format(mode), procs.TERMINAL_MODE_RESET)
        self.assertIn("\x1b[?25h", procs.TERMINAL_MODE_RESET)

    def test_windows_standalone_still_resolves_and_waits(self):
        with patch.object(procs.os, "name", "nt"), \
             patch.object(procs.shutil, "which", return_value="agentcore.cmd"), \
             patch.object(procs, "reset_terminal_modes"), \
             patch.object(procs.subprocess, "call", return_value=7) as child:
            with self.assertRaises(SystemExit) as raised:
                procs.exec_or_wait(["agentcore", "exec"])
        self.assertEqual(raised.exception.code, 7)
        child.assert_called_once_with(["agentcore.cmd", "exec"])

    def test_temp_json_files_are_unique_and_removed(self):
        paths = []
        barrier = threading.Barrier(2)

        def allocate():
            with procs.temp_json_file("status") as path:
                paths.append(path)
                barrier.wait()
                self.assertTrue(os.path.exists(path))

        threads = [threading.Thread(target=allocate) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(set(paths)), 2)
        self.assertTrue(all(not os.path.exists(path) for path in paths))


class WorkspaceRecordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = _cfg(self.tmp.name)

    def test_local_records_preserve_index_fields(self):
        workspace.save_workspace_state(
            self.cfg, "beta", "sid-b", "claude", storage="session", epoch=2
        )
        workspace.mark_status(self.cfg, "beta", "attached")
        records = list_cmd.read_workspace_records(self.cfg)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].name, "beta")
        self.assertEqual(records[0].harness, "claude")
        self.assertEqual(records[0].storage, "session")
        self.assertEqual(records[0].status.split()[0], "attached")
        self.assertEqual(records[0].runtime_workspace, "beta")
        self.assertEqual(records[0].epoch, 2)

    def test_registry_reader_is_read_only_and_uses_owner_identity(self):
        self.cfg.workspace_registry_url = "https://example.invalid"
        workspace.save_workspace_state(
            self.cfg, "remote", "old", "claude", "other-owner", "session"
        )
        remote = workspace_registry.RegistryWorkspace({
            "logicalWorkspace": "remote",
            "runtimeSessionId": "sid-r",
            "harness": "opencode",
            "workspaceIdentity": "owner-remote",
            "storage": "s3",
        })
        with patch.object(workspace_registry, "list_workspaces", return_value=[remote]), \
             patch.object(workspace, "save_workspace_state") as save:
            records = list_cmd.read_workspace_records(self.cfg)
        save.assert_not_called()
        self.assertEqual(records[0].status, "created")
        self.assertEqual(records[0].runtime_workspace, "owner-remote")

    def test_list_command_keeps_registry_cache_behavior(self):
        self.cfg.workspace_registry_url = "https://example.invalid"
        remote = workspace_registry.RegistryWorkspace({
            "logicalWorkspace": "remote",
            "runtimeSessionId": "sid-r",
            "harness": "opencode",
            "workspaceIdentity": "owner-remote",
            "storage": "s3",
        })
        with patch.object(workspace_registry, "list_workspaces", return_value=[remote]), \
             patch.object(workspace, "save_workspace_state") as save, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(list_cmd.cmd_list(self.cfg, []), 0)
        save.assert_called_once_with(
            self.cfg, "remote", "sid-r", "opencode", "owner-remote", "s3", 0
        )


class OfflineStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = _cfg(self.tmp.name)

    @staticmethod
    def _write_status(argv, **kwargs):
        Path(argv[-1]).write_text('{"state":"running","heartbeat_utc":"now"}')
        return SimpleNamespace(returncode=0)

    def test_reads_s3_status_with_expected_key(self):
        with patch.object(status_cmd.subprocess, "run", side_effect=self._write_status) as run:
            raw = status_cmd.read_offline_status(self.cfg, "owner-ws")
        self.assertEqual(json.loads(raw)["state"], "running")
        argv = run.call_args.args[0]
        self.assertIn("checkpoints/owner-ws/task-status.json", argv)
        self.assertFalse(os.path.exists(argv[-1]))

    def test_default_mode_preserves_none_on_s3_failure(self):
        with patch.object(
            status_cmd.subprocess, "run", return_value=SimpleNamespace(returncode=1)
        ):
            self.assertEqual(status_cmd.read_offline_status(self.cfg, "ws"), '{"state":"none"}')

    def test_strict_mode_surfaces_s3_failure(self):
        with patch.object(
            status_cmd.subprocess, "run", return_value=SimpleNamespace(returncode=1)
        ):
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                status_cmd.read_offline_status(self.cfg, "ws", require_success=True)

    def test_format_status_is_pure_and_render_wrapper_is_unchanged(self):
        data = {"state": "failed", "error": "x" * 501, "checkpoint_status": "failed"}
        text = status_cmd.format_status(data)
        self.assertIn("state        : failed\n", text)
        self.assertIn("error        : {}...\n".format("x" * 500), text)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status_cmd.render_status(data)
        self.assertEqual(output.getvalue(), text)


class DashboardAggregationTests(unittest.TestCase):
    def setUp(self):
        self.cfg = SimpleNamespace()

    def test_mixed_success_failure_is_immutable_and_name_sorted(self):
        records = (_record("zeta"), _record("alpha"), _record("middle"))

        def read_status(cfg, name, require_success=False):
            if name == "middle":
                raise RuntimeError("missing")
            return json.dumps({
                "state": "running" if name == "zeta" else "succeeded",
                "heartbeat_utc": "heartbeat-{}".format(name),
                "checkpoint_status": "confirmed",
            })

        with patch.object(dashboard, "read_offline_status", side_effect=read_status), \
             patch.object(dashboard, "read_manifest_age", return_value=42.0):
            snapshot = dashboard.aggregate_snapshot(
                self.cfg, records=records, max_workers=3, clock=lambda: 123.0
            )
        self.assertEqual([item.name for item in snapshot.workspaces], ["alpha", "middle", "zeta"])
        self.assertEqual(snapshot.workspaces[1].task_state, "unknown")
        self.assertIn("missing", snapshot.workspaces[1].error)
        self.assertEqual(snapshot.workspaces[0].checkpoint, "confirmed")
        self.assertEqual(snapshot.refreshed_at, 123.0)
        with self.assertRaises(Exception):
            snapshot.workspaces = ()

    def test_uses_registry_runtime_identity_for_status_key(self):
        record = _record("logical", "owner-logical")
        with patch.object(
            dashboard, "read_offline_status", return_value='{"state":"none"}'
        ) as read_status, patch.object(
            dashboard, "read_manifest_age", return_value=None
        ) as read_age:
            dashboard.aggregate_snapshot(self.cfg, records=(record,))
        read_status.assert_called_once_with(self.cfg, "owner-logical", require_success=True)
        self.assertEqual(read_age.call_args.args[:2], (self.cfg, "owner-logical"))

    def test_empty_snapshot_does_not_create_executor(self):
        with patch.object(dashboard, "ThreadPoolExecutor") as executor:
            snapshot = dashboard.aggregate_snapshot(self.cfg, records=(), clock=lambda: 8.0)
        executor.assert_not_called()
        self.assertEqual(snapshot, dashboard.DashboardSnapshot((), 8.0))

    def test_aggregation_never_invokes_runtime(self):
        record = _record("ws")
        with patch.object(dashboard, "read_offline_status", return_value='{"state":"none"}'), \
             patch.object(dashboard, "read_manifest_age", return_value=None), \
             patch.object(runtime, "invoke_best_effort") as best_effort, \
             patch.object(runtime, "invoke_verified") as verified:
            dashboard.aggregate_snapshot(self.cfg, records=(record,))
        best_effort.assert_not_called()
        verified.assert_not_called()

    def test_manifest_age_is_attached_even_without_task_status(self):
        record = _record("ws")
        with patch.object(
            dashboard, "read_offline_status", side_effect=RuntimeError("unavailable")
        ), patch.object(dashboard, "read_manifest_age", return_value=17.0):
            snapshot = dashboard.aggregate_snapshot(self.cfg, records=(record,))
        self.assertEqual(snapshot.workspaces[0].task_state, "unknown")
        self.assertEqual(snapshot.workspaces[0].manifest_age_s, 17.0)


class ManifestAgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = _cfg(self.tmp.name)

    def _head(self, stdout, returncode=0):
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    def test_reads_manifest_age_via_head_object_iso8601(self):
        with patch.object(
            dashboard.subprocess, "run",
            return_value=self._head("2026-08-16T06:46:24+00:00\n"),
        ) as run:
            age = dashboard.read_manifest_age(
                self.cfg, "owner-ws", clock=lambda: 1786862784.0 + 60.0
            )
        self.assertEqual(age, 60.0)
        argv = run.call_args.args[0]
        self.assertIn("head-object", argv)
        self.assertIn("checkpoints/owner-ws/manifest.json", argv)

    def test_parses_http_wire_timestamp_fallback(self):
        with patch.object(
            dashboard.subprocess, "run",
            return_value=self._head("Sat, 16 Aug 2026 06:46:24 GMT\n"),
        ):
            age = dashboard.read_manifest_age(
                self.cfg, "ws", clock=lambda: 1786862784.0 + 5.0
            )
        self.assertEqual(age, 5.0)

    def test_missing_manifest_or_failure_returns_none(self):
        with patch.object(
            dashboard.subprocess, "run", return_value=self._head("", returncode=254)
        ):
            self.assertIsNone(dashboard.read_manifest_age(self.cfg, "ws"))
        with patch.object(
            dashboard.subprocess, "run", return_value=self._head("not-a-date\n")
        ):
            self.assertIsNone(dashboard.read_manifest_age(self.cfg, "ws"))

    def test_clock_skew_never_returns_negative_age(self):
        with patch.object(
            dashboard.subprocess, "run",
            return_value=self._head("2026-08-16T06:46:24+00:00\n"),
        ):
            age = dashboard.read_manifest_age(
                self.cfg, "ws", clock=lambda: 1786862784.0 - 30.0
            )
        self.assertEqual(age, 0.0)

    def test_probe_never_invokes_runtime(self):
        with patch.object(
            dashboard.subprocess, "run", return_value=self._head("", returncode=254)
        ), patch.object(runtime, "invoke_best_effort") as best_effort, \
             patch.object(runtime, "invoke_verified") as verified:
            dashboard.read_manifest_age(self.cfg, "ws")
        best_effort.assert_not_called()
        verified.assert_not_called()


class RefreshControllerTests(unittest.TestCase):
    def test_initial_periodic_and_manual_refresh_publish_to_queue(self):
        output = queue.Queue()
        calls = []

        def aggregate(cfg):
            calls.append(len(calls))
            return "snapshot-{}".format(calls[-1])

        controller = dashboard.RefreshController(
            object(), interval=0.03, output=output, aggregate=aggregate
        )
        controller.start()
        self.addCleanup(controller.stop, 1)
        self.assertEqual(output.get(timeout=1), "snapshot-0")
        self.assertEqual(output.get(timeout=1), "snapshot-1")
        controller.trigger()
        self.assertEqual(output.get(timeout=1), "snapshot-2")
        controller.stop(1)
        self.assertFalse(controller._thread.is_alive())

    def test_manual_trigger_interrupts_long_interval(self):
        output = queue.Queue()
        controller = dashboard.RefreshController(
            object(), interval=60, output=output, aggregate=lambda cfg: object()
        )
        controller.start()
        self.addCleanup(controller.stop, 1)
        output.get(timeout=1)
        controller.trigger()
        self.assertIsNotNone(output.get(timeout=1))

    def test_rejects_nonpositive_interval(self):
        for interval in (0, -1):
            with self.assertRaises(ValueError):
                dashboard.RefreshController(object(), interval=interval)

    def test_worker_publishes_error_and_continues_after_aggregate_failure(self):
        output = queue.Queue()
        calls = []

        def aggregate(cfg):
            calls.append(None)
            if len(calls) == 1:
                raise RuntimeError("refresh broke")
            return dashboard.DashboardSnapshot((), 2.0)

        controller = dashboard.RefreshController(
            object(), interval=60, output=output, aggregate=aggregate
        )
        controller.start()
        self.addCleanup(controller.stop, 1)
        error = output.get(timeout=1)
        self.assertIsInstance(error, RuntimeError)
        self.assertIn("refresh broke", str(error))
        controller.trigger()
        self.assertEqual(output.get(timeout=1), dashboard.DashboardSnapshot((), 2.0))

    def test_controller_has_no_runtime_dependency(self):
        output = queue.Queue()
        with patch.object(runtime, "invoke_best_effort") as best_effort, \
             patch.object(runtime, "invoke_verified") as verified:
            controller = dashboard.RefreshController(
                object(), interval=60, output=output,
                aggregate=lambda cfg: dashboard.DashboardSnapshot((), 1.0),
            )
            controller.start()
            output.get(timeout=1)
            controller.stop(1)
        best_effort.assert_not_called()
        verified.assert_not_called()


if __name__ == "__main__":
    unittest.main()
