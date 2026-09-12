"""Tests for the stdlib ANSI dashboard renderer, input and actions."""

import io
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_CLI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _CLI_DIR not in sys.path:
    sys.path.insert(0, _CLI_DIR)

from sch import dashboard as dashboard_data
from sch.commands import dashboard


def row(name="alpha", harness="opencode", **kwargs):
    values = {
        "name": name, "harness": harness, "storage": "s3",
        "task_state": "unknown", "heartbeat": None, "checkpoint": None,
        "status_json": '{"state":"unknown"}', "error": "",
    }
    values.update(kwargs)
    return dashboard_data.WorkspaceSnapshot(**values)


class RendererTests(unittest.TestCase):
    def test_frame_has_columns_selection_detail_footer_and_age(self):
        snapshot = dashboard_data.DashboardSnapshot(
            (row(task_state="running", heartbeat="2026-08-15T12:00Z", checkpoint="ok"),),
            90,
        )
        lines = dashboard.render_lines(
            snapshot, "alpha", {}, 100, 20, now=lambda: 100
        )
        rendered = "\n".join(lines)
        self.assertIn("snapshot 10s old", rendered)
        self.assertIn("WORKSPACE", rendered)
        self.assertIn("LIVE", rendered)
        self.assertIn(">alpha", rendered)
        self.assertIn("selected alpha", rendered)
        self.assertIn("Enter run", rendered)

    def test_live_column_reflects_manifest_freshness(self):
        snapshot = dashboard_data.DashboardSnapshot(
            (
                row("alive", manifest_age_s=42.0),
                row("gone", manifest_age_s=7200.0),
                row("silent", manifest_age_s=None),
            ),
            1,
        )
        lines = dashboard.render_lines(snapshot, "alive", {}, 100, 20, now=lambda: 1)
        alive_line = next(line for line in lines if "alive" in line)
        gone_line = next(line for line in lines if "gone" in line)
        silent_line = next(line for line in lines if "silent" in line)
        self.assertIn("\u25cf 42s", alive_line)
        self.assertIn("- 2h", gone_line)
        self.assertIn("?", silent_line)
        self.assertIn("live=\u25cf 42s", "\n".join(lines))

    def test_live_cell_thresholds_and_age_formats(self):
        self.assertEqual(dashboard.live_cell(None), "?")
        self.assertEqual(dashboard.live_cell(0), "\u25cf 0s")
        self.assertEqual(
            dashboard.live_cell(dashboard_data.LIVE_THRESHOLD_S), "\u25cf 3m"
        )
        self.assertEqual(
            dashboard.live_cell(dashboard_data.LIVE_THRESHOLD_S + 1), "- 3m"
        )
        self.assertEqual(dashboard.format_age(119), "119s")
        self.assertEqual(dashboard.format_age(120), "2m")
        self.assertEqual(dashboard.format_age(7199), "119m")
        self.assertEqual(dashboard.format_age(7200), "2h")
        self.assertEqual(dashboard.format_age(48 * 3600), "2d")

    def test_narrow_frame_is_truncated_to_width(self):
        snapshot = dashboard_data.DashboardSnapshot(
            (row(name="workspace-with-a-very-long-name"),), 1
        )
        lines = dashboard.render_lines(snapshot, None, {}, 28, 8, now=lambda: 1)
        self.assertTrue(lines)
        self.assertTrue(all(len(line) <= 28 for line in lines))

    def test_persisted_status_cannot_inject_terminal_controls(self):
        snapshot = dashboard_data.DashboardSnapshot(
            (row(status_json='{"state":"failed","error":"bad\\u001b[2J\\nnext"}'),), 1
        )
        rendered = "\n".join(
            dashboard.render_lines(
                snapshot, "alpha", {}, 80, 10, detail=True, now=lambda: 1
            )
        )
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("\nnext", rendered)

    def test_claude_web_is_visibly_unavailable(self):
        snapshot = dashboard_data.DashboardSnapshot((row(harness="claude"),), 1)
        rendered = "\n".join(
            dashboard.render_lines(snapshot, "alpha", {}, 100, 10, now=lambda: 1)
        )
        self.assertIn("n/a", rendered)
        self.assertIn("web(n/a)", rendered)

    def test_pi_web_is_visibly_unavailable(self):
        """add-pi-harness task 5.5/5.7: `sch web` is opencode-only, so the pi
        column and the `w` action show n/a (spec: remote-ui-tunnel)."""
        snapshot = dashboard_data.DashboardSnapshot((row(harness="pi"),), 1)
        rendered = "\n".join(
            dashboard.render_lines(snapshot, "alpha", {}, 100, 10, now=lambda: 1)
        )
        self.assertIn("n/a", rendered)
        self.assertIn("web(n/a)", rendered)

    def test_opencode_web_stays_available(self):
        snapshot = dashboard_data.DashboardSnapshot((row(harness="opencode"),), 1)
        rendered = "\n".join(
            dashboard.render_lines(snapshot, "alpha", {}, 100, 10, now=lambda: 1)
        )
        self.assertNotIn("web(n/a)", rendered)

    def test_viewport_follows_selected_workspace(self):
        rows = tuple(row("ws{:02d}".format(index)) for index in range(20))
        snapshot = dashboard_data.DashboardSnapshot(rows, 1)
        rendered = "\n".join(
            dashboard.render_lines(snapshot, "ws19", {}, 80, 9, now=lambda: 1)
        )
        self.assertIn(">ws19", rendered)
        self.assertNotIn(" ws00", rendered)

    def test_status_detail_contains_offline_fields_and_output(self):
        item = row(task_state="failed", status_json=(
            '{"state":"failed","exit_code":2,"error":"bad","output":"tail"}'
        ))
        self.assertEqual(
            dashboard.status_lines(item),
            ["status alpha", "state        : failed", "exit_code    : 2",
             "error        : bad", "output:", "tail"],
        )

    def test_frame_contains_clear_home_sequence(self):
        snapshot = dashboard_data.DashboardSnapshot((), 0)
        self.assertTrue(
            dashboard.render_frame(snapshot, None, {}, 80, 10).startswith(dashboard.CLEAR)
        )


class InputAndSelectionTests(unittest.TestCase):
    def test_decodes_posix_keys(self):
        cases = {
            b"\x1b[A": "up", b"\x1b[B": "down", b"\r": "run",
            b"S": "shell", b"w": "w", b"s": "s", b"r": "r", b"q": "q",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(dashboard.decode_input(raw), expected)

    def test_decodes_windows_extended_keys(self):
        self.assertEqual(dashboard.decode_input("\xe0H", "nt"), "up")
        self.assertEqual(dashboard.decode_input("\x00P", "nt"), "down")

    def test_unknown_key_is_ignored(self):
        self.assertIsNone(dashboard.decode_input("x"))

    def test_selection_stays_anchored_and_wraps(self):
        snapshot = dashboard_data.DashboardSnapshot((row("alpha"), row("beta")), 0)
        self.assertEqual(dashboard.move_selection(snapshot, "beta", 0), "beta")
        self.assertEqual(dashboard.move_selection(snapshot, "beta", 1), "alpha")
        self.assertEqual(dashboard.move_selection(snapshot, "missing", 0), "alpha")


class ActionTests(unittest.TestCase):
    def test_cli_argv_uses_canonical_entrypoint_not_process_argv_zero(self):
        with patch.object(dashboard.sys, "argv", ["/unrelated/runner"]):
            argv = dashboard.cli_argv("run", "alpha")
        self.assertEqual(argv[0], sys.executable)
        self.assertEqual(argv[1], os.path.abspath(os.path.join(
            os.path.dirname(dashboard.__file__), "..", "__main__.py"
        )))
        self.assertEqual(argv[2:], ["run", "alpha"])

    def test_detached_options_are_platform_specific(self):
        self.assertEqual(dashboard._detached_options("posix"), {"start_new_session": True})
        flags = dashboard._detached_options("nt")["creationflags"]
        self.assertTrue(flags & 0x00000008)
        self.assertTrue(flags & 0x00000200)

    def test_claude_guard_spawns_nothing_and_opens_nothing(self):
        popen = MagicMock()
        browser = MagicMock()
        message = dashboard.start_web_bridge(row(harness="claude"), {}, popen, browser)
        self.assertIn("unavailable", message)
        popen.assert_not_called()
        browser.assert_not_called()

    def test_pi_guard_spawns_nothing_and_opens_nothing(self):
        popen = MagicMock()
        browser = MagicMock()
        message = dashboard.start_web_bridge(row(harness="pi"), {}, popen, browser)
        self.assertIn("unavailable", message)
        self.assertIn("pi", message)
        popen.assert_not_called()
        browser.assert_not_called()

    def test_web_tracks_plain_localhost_url_and_detaches(self):
        child = MagicMock(pid=42, stdout=io.StringIO("http://127.0.0.1:4321\n"))
        child.poll.return_value = None
        popen = MagicMock(return_value=child)
        browser = MagicMock(return_value=True)
        bridges = {}
        with patch.object(dashboard, "_detached_options", return_value={"start_new_session": True}), \
             patch.object(dashboard, "cli_argv", return_value=["python", "sch", "web", "alpha"]):
            message = dashboard.start_web_bridge(row(), bridges, popen, browser)
        self.assertIn("ready", message)
        self.assertEqual(bridges["alpha"].url, "http://127.0.0.1:4321")
        self.assertEqual(popen.call_args.args[0][-1], "--no-browser")
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        browser.assert_called_once_with("http://127.0.0.1:4321")

    def test_existing_live_bridge_is_reused(self):
        process = MagicMock()
        process.poll.return_value = None
        bridges = {"alpha": dashboard.WebBridge(42, "http://127.0.0.1:4321", process)}
        popen = MagicMock()
        browser = MagicMock()
        dashboard.start_web_bridge(row(), bridges, popen, browser)
        popen.assert_not_called()
        browser.assert_called_once_with("http://127.0.0.1:4321")

    def test_dead_bridge_is_replaced(self):
        old_process = MagicMock()
        old_process.poll.return_value = 4
        old = dashboard.WebBridge(10, "http://127.0.0.1:4000", old_process)
        child = MagicMock(pid=42, stdout=io.StringIO("http://127.0.0.1:4321\n"))
        child.poll.return_value = None
        bridges = {"alpha": old}
        dashboard.start_web_bridge(row(), bridges, MagicMock(return_value=child), MagicMock())
        self.assertIs(bridges["alpha"].process, child)
        old_process.terminate.assert_not_called()

    def test_invalid_non_local_ready_line_is_not_tracked(self):
        child = MagicMock(pid=42, stdout=io.StringIO("http://0.0.0.0:4321\n"))
        child.poll.return_value = None
        popen = MagicMock(return_value=child)
        bridges = {}
        message = dashboard.start_web_bridge(row(), bridges, popen, MagicMock())
        self.assertIn("valid localhost URL", message)
        self.assertEqual(bridges, {})
        child.terminate.assert_called_once_with()

    def test_web_readiness_timeout_terminates_unready_child(self):
        child = MagicMock(pid=42)
        child.poll.return_value = None
        with patch.object(dashboard, "_read_ready_line", side_effect=RuntimeError("timed out")):
            message = dashboard.start_web_bridge(
                row(), {}, MagicMock(return_value=child), MagicMock(), ready_timeout=0
            )
        self.assertIn("timed out", message)
        child.terminate.assert_called_once_with()


class CommandLoopTests(unittest.TestCase):
    def _run(self, actions, handoff=0, vt=True, bridge=None):
        events = []
        terminal = MagicMock()
        terminal.enable_vt.return_value = vt
        terminal.enter.side_effect = lambda: events.append("enter")
        terminal.leave.side_effect = lambda: events.append("leave")
        terminal.read_action.side_effect = actions
        snapshot = dashboard_data.DashboardSnapshot((row(),), 1)
        worker = MagicMock()
        worker.start.side_effect = lambda: worker.output.put(snapshot)

        def controller(_cfg, _interval, output):
            worker.output = output
            return worker

        stdin = MagicMock()
        stdout = MagicMock()
        stdin.isatty.return_value = True
        stdout.isatty.return_value = True
        messages = []
        def start_bridge(selected, bridges):
            if bridge is not None:
                bridges[selected.name] = bridge
            return "ready"

        with patch.object(dashboard, "Terminal", return_value=terminal), \
             patch.object(dashboard.dashboard_data, "RefreshController", side_effect=controller), \
             patch.object(dashboard.procs, "foreground_handoff", return_value=handoff) as child, \
             patch.object(dashboard, "start_web_bridge", side_effect=start_bridge), \
             patch.object(dashboard, "render_frame", side_effect=lambda *a, **kw: messages.append(kw.get("message", "")) or ""), \
             patch.object(dashboard.sys, "stdin", stdin), \
             patch.object(dashboard.sys, "stdout", stdout):
            result = dashboard.cmd_dashboard(object(), [])
        return result, events, worker, child, messages

    def test_handoff_leaves_and_reenters_terminal_and_reports_nonzero(self):
        result, events, worker, child, messages = self._run(["run", "q"], handoff=9)
        self.assertEqual(result, 0)
        self.assertEqual(events, ["enter", "leave", "enter", "leave"])
        child.assert_called_once_with(dashboard.cli_argv("run", "alpha"))
        worker.trigger.assert_called_once_with()
        self.assertTrue(any("run exited with status 9" in message for message in messages))

    def test_run_and_shell_presence_is_owned_by_handoff_child(self):
        for action in ("run", "shell"):
            with self.subTest(action=action):
                _, _, _, child, _ = self._run([action, "q"])
                child.assert_called_once_with(dashboard.cli_argv(action, "alpha"))

    def test_quit_does_not_clean_up_detached_bridges(self):
        process = MagicMock()
        self._run(["w", "q"], bridge=dashboard.WebBridge(
            42, "http://127.0.0.1:1", process
        ))
        process.terminate.assert_not_called()
        process.kill.assert_not_called()

    def test_vt_rejection_happens_before_terminal_entry_or_worker_start(self):
        terminal = MagicMock()
        terminal.enable_vt.return_value = False
        stdin = MagicMock()
        stdout = MagicMock()
        stdin.isatty.return_value = True
        stdout.isatty.return_value = True
        with patch.object(dashboard, "Terminal", return_value=terminal), \
             patch.object(dashboard.sys, "stdin", stdin), \
             patch.object(dashboard.sys, "stdout", stdout), \
             patch.object(dashboard.dashboard_data, "RefreshController") as controller, \
             patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit):
            dashboard.cmd_dashboard(object(), [])
        terminal.enter.assert_not_called()
        controller.assert_not_called()


class ArgumentTests(unittest.TestCase):
    def test_default_and_custom_interval(self):
        self.assertEqual(dashboard._parse_args([]), 20.0)
        self.assertEqual(dashboard._parse_args(["--interval", "2.5"]), 2.5)

    def test_invalid_interval_fails(self):
        for args in (["--interval", "0"], ["--interval", "no"], ["extra"]):
            with self.subTest(args=args), patch("sys.stderr", new=io.StringIO()):
                with self.assertRaises(SystemExit):
                    dashboard._parse_args(args)


if __name__ == "__main__":
    unittest.main()
