"""Offline tests for the lazy, supervised OpenCode web backend."""

from __future__ import annotations

import __future__
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


class FakeApp:
    def entrypoint(self, func):
        return func

    def websocket(self, func):
        return func

    def add_async_task(self, *_args, **_kwargs):
        return object()

    def complete_async_task(self, *_args, **_kwargs):
        return None

    def run(self, *_args, **_kwargs):
        return None


def load_main():
    app_dir = Path(__file__).parent
    sys.path.insert(0, str(app_dir))
    bedrock = types.ModuleType("bedrock_agentcore")
    bedrock.BedrockAgentCoreApp = FakeApp
    boto3 = types.ModuleType("boto3")
    boto3.client = lambda *_args, **_kwargs: None

    module_name = "sch_serve_web_main"
    spec = importlib.util.spec_from_file_location(module_name, app_dir / "main.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(
        sys.modules,
        {module_name: module, "bedrock_agentcore": bedrock, "boto3": boto3},
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


main = load_main()


class StopSupervisor(Exception):
    pass


class ServeWebTests(unittest.TestCase):
    def setUp(self):
        main._HARNESS["value"] = "opencode"
        main._WORKSPACE_NAME["value"] = "test-workspace"
        main.CHECKPOINT_BUCKET = ""
        main._SESSION_EPOCH["value"] = 0
        main._SERVE_STATE.update(
            {
                "proc": None,
                "port": None,
                "started_utc": None,
                "restart_count": 0,
                "supervisor_started": False,
            }
        )

    def test_supervisor_starts_web_on_localhost_without_output_pipes(self):
        process = Mock(pid=1234, returncode=None)
        process.poll.return_value = None
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp) / "repo"
            ready_marker = Path(tmp) / "ready"
            with patch.object(
                main, "READY_MARKER", ready_marker
            ), patch.object(main, "REPO_DIR", repo_dir), patch.object(
                main.shutil, "which", return_value="/usr/local/bin/opencode"
            ), patch.object(
                main.subprocess, "Popen", return_value=process
            ) as popen, patch.object(main.time, "sleep", side_effect=StopSupervisor):
                ready_marker.touch()
                repo_dir.mkdir()
                with self.assertRaises(StopSupervisor):
                    main._serve_supervisor_loop()

        popen.assert_called_once_with(
            [
                "/usr/local/bin/opencode",
                "web",
                "--hostname",
                "127.0.0.1",
                "--port",
                str(main.OPENCODE_SERVE_PORT),
            ],
            stdin=main.subprocess.DEVNULL,
            stdout=main.subprocess.DEVNULL,
            stderr=main.subprocess.DEVNULL,
            text=True,
            cwd=str(repo_dir),
            start_new_session=True,
            env=unittest.mock.ANY,
        )
        self.assertEqual(popen.call_args.kwargs["env"]["SCH_HARNESS"], "opencode")
        self.assertIs(main._SERVE_STATE["proc"], process)
        self.assertEqual(main._SERVE_STATE["port"], main.OPENCODE_SERVE_PORT)

    def test_supervisor_restarts_crashed_process(self):
        crashed = Mock(returncode=7)
        crashed.poll.return_value = 7
        replacement = Mock(pid=5678, returncode=None)
        replacement.poll.return_value = None
        main._SERVE_STATE["proc"] = crashed
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            main, "READY_MARKER", Path(tmp) / "ready"
        ), patch.object(main, "REPO_DIR", Path(tmp) / "repo"), patch.object(
            main.subprocess, "Popen", return_value=replacement
        ), patch.object(main.time, "sleep", side_effect=StopSupervisor):
            main.READY_MARKER.touch()
            main.REPO_DIR.mkdir()
            with self.assertRaises(StopSupervisor):
                main._serve_supervisor_loop()

        self.assertEqual(main._SERVE_STATE["restart_count"], 1)
        self.assertIs(main._SERVE_STATE["proc"], replacement)

    def test_serve_ensure_is_lazy_and_advertises_web_capability(self):
        main._SERVE_STATE["proc"] = Mock()
        main._SERVE_STATE["proc"].poll.return_value = None
        with patch.object(main, "_ensure_serve_supervisor_started") as ensure, patch.object(
            main, "_opencode_version", return_value="1.2.3"
        ):
            response = main.invoke({"action": "serve-ensure"})

        ensure.assert_called_once_with()
        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["capabilities"], {"web": True})
        self.assertEqual(response["port"], main.OPENCODE_SERVE_PORT)

    def test_starting_response_also_advertises_web_capability(self):
        with patch.object(main, "_ensure_serve_supervisor_started"), patch.object(
            main.time, "monotonic", side_effect=[0, 9]
        ), patch.object(main, "_opencode_version", return_value="1.2.3"):
            response = main.invoke({"action": "serve-ensure"})

        self.assertEqual(response["status"], "starting")
        self.assertEqual(response["capabilities"], {"web": True})

    def test_serve_ensure_rejects_claude_without_starting_supervisor(self):
        main._HARNESS["value"] = "claude"
        with patch.object(main, "_ensure_serve_supervisor_started") as ensure:
            response = main.invoke({"action": "serve-ensure"})

        ensure.assert_not_called()
        self.assertEqual(response["status"], "rejected")
        self.assertNotIn("capabilities", response)
        self.assertIn("requires harness=opencode", response["error"])


if __name__ == "__main__":
    unittest.main()
