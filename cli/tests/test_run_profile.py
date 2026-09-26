"""PTY tests for the runtime image's run-once profile script."""

import json
import os
import pty
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


PROFILE = Path(__file__).resolve().parents[2] / "image" / "scripts" / "sch-run-profile.sh"


class RunProfileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.marker = self.root / "run-once.json"
        self.workspace = self.root / "workspace.json"
        self.repo = self.root / "repo"
        self.bin_dir = self.root / "bin"
        self.argv_file = self.root / "argv"
        self.marker_state_file = self.root / "marker-state"
        self.continued_file = self.root / "continued"
        self.repo.mkdir()
        self.bin_dir.mkdir()
        self.workspace.write_text(json.dumps({"root": str(self.root)}))

        source = PROFILE.read_text()
        source = source.replace(
            '_SCH_RUN_MARKER="/home/sch/.sch-run-once.json"',
            '_SCH_RUN_MARKER="{}"'.format(self.marker),
        ).replace('/home/sch/.sch-workspace.json', str(self.workspace))
        self.profile = self.root / "sch-run-profile.sh"
        self.profile.write_text(source)

        self.env_file = self.root / "env"
        for harness_name in ("opencode", "claude", "pi"):
            harness = self.bin_dir / harness_name
            harness.write_text(
                "#!/bin/bash\n"
                ': > "${SCH_TEST_ARGV}"\n'
                'for arg in "$@"; do printf "%s\\n" "$arg" >> "${SCH_TEST_ARGV}"; done\n'
                'printf "%s" "${OPENCODE_CONFIG_CONTENT-<unset>}" > "${SCH_TEST_ENV}"\n'
                'if [ -e "${SCH_TEST_MARKER}" ]; then\n'
                '    printf present > "${SCH_TEST_MARKER_STATE}"\n'
                'else\n'
                '    printf absent > "${SCH_TEST_MARKER_STATE}"\n'
                'fi\n'
            )
            harness.chmod(0o755)

    def run_profile(self, marker, extra_env=None):
        self.marker.write_text(json.dumps(marker))
        master, slave = pty.openpty()
        # Hermetic child environment: the profile script reads SCH_* variables
        # (SCH_RUN_AUTOSTARTED short-circuits the autostart entirely), and a
        # developer machine or a CI runner may carry them -- e.g. when the suite
        # itself runs inside an SCH workspace. Only the test's own variables and
        # a clean PATH reach the script.
        env = {k: v for k, v in os.environ.items() if not k.startswith("SCH_")}
        env["PATH"] = str(self.bin_dir) + os.pathsep + env.get("PATH", "/usr/bin:/bin")
        env["SCH_TEST_ARGV"] = str(self.argv_file)
        env["SCH_TEST_ENV"] = str(self.env_file)
        env["SCH_TEST_MARKER"] = str(self.marker)
        env["SCH_TEST_MARKER_STATE"] = str(self.marker_state_file)
        env.update(extra_env or {})
        command = '. "$1"; printf continued > "$2"'
        try:
            process = subprocess.Popen(
                ["bash", "--noprofile", "--norc", "-c", command, "bash", str(self.profile), str(self.continued_file)],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env=env,
                close_fds=True,
            )
            os.close(slave)
            return_code = process.wait(timeout=10)
        finally:
            os.close(master)
            if process.poll() is None:
                process.kill()
                process.wait()
        self.assertEqual(return_code, 0)
        self.assertFalse(self.marker.exists())

    def test_claude_model_is_passed_as_exact_two_element_argv(self):
        model = "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
        self.run_profile({"harness": "claude", "model": model, "epoch": time.time()})

        self.assertEqual(self.argv_file.read_text().splitlines(), ["--model", model])
        self.assertEqual(self.marker_state_file.read_text(), "absent")
        self.assertFalse(self.continued_file.exists())

    def test_opencode_model_rides_config_content_env_not_argv(self):
        # OpenCode 2: the root TUI has no --model flag; the model is merged
        # through OPENCODE_CONFIG_CONTENT for this process only, and the TUI
        # runs --standalone (private embedded server).
        self.run_profile({"harness": "opencode", "model": "provider/model-id", "epoch": time.time()})

        self.assertEqual(self.argv_file.read_text().splitlines(), ["--standalone"])
        self.assertEqual(json.loads(self.env_file.read_text()), {"model": "provider/model-id"})
        self.assertEqual(self.marker_state_file.read_text(), "absent")
        self.assertFalse(self.continued_file.exists())

    def test_opencode_session_resume_keeps_standalone_and_session_flags(self):
        self.run_profile({
            "harness": "opencode", "model": "provider/model-id",
            "session_id": "ses_abc", "epoch": time.time(),
        })

        self.assertEqual(
            self.argv_file.read_text().splitlines(),
            ["--standalone", "--session", "ses_abc"],
        )
        self.assertEqual(json.loads(self.env_file.read_text()), {"model": "provider/model-id"})

    def test_pi_receives_provider_model_and_interactive_role(self):
        model = "eu.anthropic.claude-sonnet-4-6"
        pi_dir = self.root / "pi-agent"
        role = pi_dir / "roles" / "remote-interactive.md"
        role.parent.mkdir(parents=True)
        role.write_text("interactive role")

        self.run_profile(
            {"harness": "pi", "model": model, "epoch": time.time()},
            extra_env={"PI_CODING_AGENT_DIR": str(pi_dir)},
        )

        self.assertEqual(
            self.argv_file.read_text().splitlines(),
            [
                "--provider", "amazon-bedrock", "--model", model,
                "--append-system-prompt", str(role),
            ],
        )
        self.assertEqual(self.marker_state_file.read_text(), "absent")
        self.assertFalse(self.continued_file.exists())

    def test_absent_model_executes_bare_harness(self):
        self.run_profile({"harness": "opencode", "epoch": time.time()})

        self.assertEqual(self.argv_file.read_text().splitlines(), ["--standalone"])
        self.assertEqual(self.env_file.read_text(), "<unset>")
        self.assertEqual(self.marker_state_file.read_text(), "absent")
        self.assertFalse(self.continued_file.exists())

    def test_empty_model_executes_bare_harness(self):
        self.run_profile({"harness": "claude", "model": "", "epoch": time.time()})

        self.assertEqual(self.argv_file.read_text(), "")
        self.assertEqual(self.marker_state_file.read_text(), "absent")
        self.assertFalse(self.continued_file.exists())

    def test_expired_model_does_not_autostart(self):
        self.run_profile({"harness": "opencode", "model": "provider/model-id", "epoch": 0})

        self.assertFalse(self.argv_file.exists())
        self.assertTrue(self.continued_file.exists())


if __name__ == "__main__":
    unittest.main()
