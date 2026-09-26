"""Tests for sch handoff selection, parsing and orchestration."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sch import runtime
from sch.commands import handoff
from sch.harness import ResolvedHarness


def _fake_run_opencode(processes):
    """Mimic _run_opencode's contract: with stdout_path the payload lands in
    the file (regression: export must not be captured via pipe, see 64KB
    truncation), without it the payload stays on proc.stdout."""
    queue = list(processes)

    def run(argv, cwd=None, stdout_path=None):
        proc = queue.pop(0)
        if stdout_path is not None:
            with open(stdout_path, "w", encoding="utf-8") as stream:
                stream.write(proc.stdout)
        return proc

    return run


class HandoffTests(unittest.TestCase):
    def test_parse_and_reject_repo_options(self):
        self.assertEqual(handoff._parse_args(["ws", "--session", "ses_x", "--sanitize", "--storage", "s3"]),
                         ("ws", "ses_x", True, "s3", ""))
        self.assertEqual(handoff._parse_args(["ws", "--harness", "opencode", "--session", "ses_x"]),
                         ("ws", "ses_x", False, "", "opencode"))
        for flag in ("--branch", "--sync", "--no-sync"):
            with self.assertRaises(SystemExit):
                handoff._parse_args(["ws", flag])
        with self.assertRaises(SystemExit):
            handoff._parse_args(["ws", "--harness"])
        with self.assertRaises(SystemExit):
            handoff._parse_args(["ws", "--unknown-flag"])
        # Invalid harness values are rejected at parse time.
        with self.assertRaises(SystemExit):
            handoff._parse_args(["ws", "--harness", "vscode"])

    def test_non_opencode_harness_flag_is_refused_without_mutation(self):
        """`--harness claude|pi` is rejected before any local or remote
        mutation: no export runs, no harness is resolved, no runtime is
        invoked (spec: session-handoff I1/I5)."""
        for flag in ("claude", "pi"):
            with patch.object(handoff.deps, "which_opencode") as which, \
                 patch.object(handoff, "_run_opencode") as run, \
                 patch.object(handoff.harness_mod, "resolve_harness") as resolve, \
                 patch.object(handoff.runtime, "invoke_verified") as invoke, \
                 contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit, msg=flag):
                    handoff.cmd_handoff(object(), ["ws", "--session", "ses_x", "--harness", flag])
                which.assert_not_called()
                run.assert_not_called()
                resolve.assert_not_called()
                invoke.assert_not_called()

    def test_latest_session_filters_current_directory(self):
        records = [
            {"id": "wrong", "title": "wrong", "updated": 999, "directory": "/other"},
            {"id": "old", "title": "old", "updated": 1, "directory": "/repo"},
            {"id": "new", "title": "newest", "updated": 2, "directory": "/repo"},
        ]
        proc = type("P", (), {"returncode": 0, "stdout": json.dumps(records), "stderr": ""})()
        err = io.StringIO()
        with patch.object(handoff, "_run_opencode", return_value=proc), contextlib.redirect_stderr(err):
            self.assertEqual(handoff._latest_session("opencode", "/repo"), "new")
        self.assertIn("newest", err.getvalue())

    def test_payload_shape(self):
        data = json.loads(runtime.payload_session_import("ws", "opencode", "s3", 4))
        self.assertEqual(data, {"action": "session-import", "workspace": "ws", "harness": "opencode", "storage_backend": "s3", "session_epoch": 4})

    def test_run_opencode_stdout_path_redirects_to_file(self):
        """Regression: export output goes straight to a file, never a pipe —
        opencode (bun) exits without flushing pipe writes, truncating exports
        beyond ~64KB and making handoff die on 'invalid JSON'."""
        import sys
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "export.json"
            size = 200_000  # comfortably past the 64KB pipe buffer
            proc = handoff._run_opencode(
                [sys.executable, "-c", "import sys; sys.stdout.write('x' * {})".format(size)],
                stdout_path=str(out),
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIsNone(proc.stdout)
            self.assertEqual(len(out.read_text()), size)

    def test_non_opencode_binding_is_refused_naming_the_constraint(self):
        """add-pi-harness task 5.4/5.7: `sch handoff` transfers an OpenCode
        session, so a workspace bound to claude or pi is refused with the
        opencode-only constraint named (spec: session-handoff)."""
        for bound in ("claude", "pi"):
            with tempfile.TemporaryDirectory() as tmp:
                ws_dir = Path(tmp) / "workspaces"
                ws_dir.mkdir(parents=True)
                (ws_dir / "bws").write_text(json.dumps({
                    "runtimeSessionId": "sch-bws-1111", "harness": bound,
                    "storage": "s3", "epoch": 1,
                }))
                cfg = SimpleNamespace(ws_dir=ws_dir, default_harness="opencode",
                                      default_storage="s3", workspace_registry_url="")
                stderr = io.StringIO()
                with patch.object(handoff.harness_mod, "resolve_harness") as resolve, \
                     patch.object(handoff.runtime, "invoke_verified") as invoke, \
                     contextlib.redirect_stderr(stderr):
                    with self.assertRaises(SystemExit, msg=bound):
                        handoff._reject_non_opencode_binding(cfg, "bws")
                    resolve.assert_not_called()
                    invoke.assert_not_called()
                message = stderr.getvalue()
                self.assertIn(bound, message)
                self.assertIn("opencode", message)

    def test_opencode_binding_and_fresh_workspace_are_not_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws_dir = Path(tmp) / "workspaces"
            ws_dir.mkdir(parents=True)
            cfg = SimpleNamespace(ws_dir=ws_dir, default_harness="opencode",
                                  default_storage="s3", workspace_registry_url="")
            # Never used: no index file at all.
            handoff._reject_non_opencode_binding(cfg, "fresh")
            (ws_dir / "ocws").write_text(json.dumps({
                "runtimeSessionId": "sch-ocws-2222", "harness": "opencode",
                "storage": "s3", "epoch": 1,
            }))
            handoff._reject_non_opencode_binding(cfg, "ocws")

    def test_unreadable_index_falls_through_to_resolve_harness(self):
        """The pre-check is a message nicety, never a new failure mode: a cfg
        it cannot read must not raise (resolve_harness still enforces)."""
        handoff._reject_non_opencode_binding(object(), "ws")

    def test_missing_opencode_precedes_harness_resolution(self):
        with patch.object(handoff.deps, "which_opencode", return_value=None), \
             patch.object(handoff.harness_mod, "resolve_harness") as resolve:
            with self.assertRaises(SystemExit):
                handoff.cmd_handoff(object(), ["ws"])
            resolve.assert_not_called()

    def test_happy_path_and_warnings(self):
        class Cfg:
            region = "eu-west-1"
        export = json.dumps({"info": {"id": "ses_x"}, "messages": []})
        processes = [
            type("P", (), {"returncode": 0, "stdout": "opencode v2.0.18\n", "stderr": ""})(),
            type("P", (), {"returncode": 0, "stdout": export, "stderr": ""})(),
        ]
        seen_argv = []
        fake_run = _fake_run_opencode(processes)

        def recording_run(argv, cwd=None, stdout_path=None):
            seen_argv.append(list(argv))
            return fake_run(argv, cwd=cwd, stdout_path=stdout_path)
        warm = runtime.InvocationResult(True, json.dumps({"status": "ok", "storage": "session"}))
        imported = runtime.InvocationResult(True, json.dumps({"status": "ok", "sessionID": "ses_x", "reimported": True, "repoEmpty": True}))
        out, err = io.StringIO(), io.StringIO()
        with patch.object(handoff.deps, "which_opencode", return_value="/bin/opencode"), \
             patch.object(handoff, "_run_opencode", side_effect=recording_run), \
             patch.object(handoff.harness_mod, "resolve_harness", return_value=ResolvedHarness("sid", "opencode", False, storage="session", epoch=1)), \
             patch.object(handoff.runtime, "invoke_verified", side_effect=[warm, imported]), \
             patch.object(handoff.bundlexfer, "bundle_helper_argv", return_value=["helper"]), \
             patch.object(handoff.bundlexfer, "run_bundle_helper"), \
             patch.object(handoff, "runtime_arn", return_value="arn"), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(handoff.cmd_handoff(Cfg(), ["ws", "--session", "ses_x"]), 0)
        self.assertEqual(out.getvalue().strip(), "ses_x")
        self.assertIn("overwritten", err.getvalue())
        self.assertIn("--sync", err.getvalue())
        # OpenCode 2: `session export --standalone <id>` (top-level `export` in 1.x).
        self.assertEqual(seen_argv[1], ["/bin/opencode", "session", "export", "--standalone", "ses_x"])

    def test_local_version_strips_the_2x_prefix(self):
        processes = [type("P", (), {"returncode": 0, "stdout": "opencode v2.0.18\n", "stderr": ""})()]
        with patch.object(handoff, "_run_opencode", side_effect=_fake_run_opencode(processes)):
            self.assertEqual(handoff._version("/bin/opencode"), "2.0.18")
        processes = [type("P", (), {"returncode": 1, "stdout": "", "stderr": "boom"})()]
        with patch.object(handoff, "_run_opencode", side_effect=_fake_run_opencode(processes)):
            self.assertEqual(handoff._version("/bin/opencode"), "unknown")

    def test_latest_session_uses_standalone_json_listing(self):
        listing = json.dumps([
            {"id": "ses_old", "directory": os.getcwd(), "updated": 1, "title": "old"},
            {"id": "ses_new", "directory": os.getcwd(), "updated": 5, "title": "new"},
            {"id": "ses_other", "directory": "/elsewhere", "updated": 9},
        ])
        processes = [type("P", (), {"returncode": 0, "stdout": listing, "stderr": ""})()]
        seen = []
        fake = _fake_run_opencode(processes)

        def recording(argv, cwd=None, stdout_path=None):
            seen.append(list(argv))
            return fake(argv, cwd=cwd, stdout_path=stdout_path)
        with patch.object(handoff, "_run_opencode", side_effect=recording), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(handoff._latest_session("/bin/opencode"), "ses_new")
        self.assertEqual(seen[0], ["/bin/opencode", "session", "list", "--standalone", "--format", "json"])

    def test_unknown_action_has_rebuild_remedy(self):
        class Cfg:
            region = "eu-west-1"
        export = json.dumps({"info": {"id": "ses_x"}})
        processes = [type("P", (), {"returncode": 0, "stdout": "1", "stderr": ""})(), type("P", (), {"returncode": 0, "stdout": export, "stderr": ""})()]
        warm = runtime.InvocationResult(True, json.dumps({"status": "ok", "storage": "session"}))
        old = runtime.InvocationResult(True, json.dumps({"status": "error", "message": "unknown action 'session-import'"}))
        with patch.object(handoff.deps, "which_opencode", return_value="opencode"), patch.object(handoff, "_run_opencode", side_effect=_fake_run_opencode(processes)), patch.object(handoff.harness_mod, "resolve_harness", return_value=ResolvedHarness("sid", "opencode", False, storage="session", epoch=1)), patch.object(handoff.runtime, "invoke_verified", side_effect=[warm, old]), patch.object(handoff.bundlexfer, "bundle_helper_argv", return_value=[]), patch.object(handoff.bundlexfer, "run_bundle_helper"), patch.object(handoff, "runtime_arn", return_value="arn"):
            with self.assertRaises(SystemExit):
                handoff.cmd_handoff(Cfg(), ["ws", "--session", "ses_x"])


if __name__ == "__main__":
    unittest.main()
