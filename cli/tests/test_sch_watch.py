"""bin/sch-watch: English output, and a retrieval hint that follows the
workspace mode (`sch fetch` for git-native workspaces only) (TASK-25)."""

import datetime
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "bin" / "sch-watch"

# Stand-in for `bin/sch`: serves the queued `sch status --json` answers in
# order (the last one repeats) and records its arguments.
FAKE_SCH = """#!/usr/bin/env python3
import json, os, sys
root = os.environ["SCH_FAKE_DIR"]
with open(os.path.join(root, "answers.json")) as fh:
    answers = json.load(fh)
calls = os.path.join(root, "calls")
with open(calls, "a") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\\n")
with open(calls) as fh:
    n = len(fh.read().splitlines())
payload, rc = answers[min(n, len(answers)) - 1]
if payload is not None:
    print(json.dumps(payload))
sys.exit(rc)
"""


def _fresh_stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SchWatchTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        shutil.copy2(SOURCE, bin_dir / "sch-watch")
        fake = bin_dir / "sch"
        fake.write_text(FAKE_SCH)
        fake.chmod(0o755)
        self.watch = bin_dir / "sch-watch"

    def run_watch(self, answers):
        (self.root / "answers.json").write_text(json.dumps(answers))
        env = dict(os.environ, SCH_FAKE_DIR=str(self.root))
        result = subprocess.run(
            [str(self.watch), "ws", "0"], env=env, capture_output=True, text=True,
            timeout=30, check=False,
        )
        calls = (self.root / "calls").read_text().splitlines()
        self.assertTrue(calls)
        self.assertEqual(set(calls), {"status ws --json"})
        return result

    def test_mirror_workspace_is_reopened_with_sch_run(self):
        result = self.run_watch([
            [{"state": "running", "task_id": "2be6f96fa5c5", "heartbeat_utc": _fresh_stamp()}, 0],
            [{"state": "succeeded", "task_id": "2be6f96fa5c5"}, 0],
        ])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("state=running      task=2be6f96f", result.stdout)
        self.assertIn("  alive", result.stdout)
        self.assertIn("<- terminal state, watch finished", result.stdout)
        self.assertIn("sch-watch: task finished; next: bin/sch run ws", result.stdout)
        self.assertNotIn("sch fetch", result.stdout)

    def test_git_native_workspace_is_collected_with_sch_fetch(self):
        result = self.run_watch([
            [{"state": "failed", "session_mode": "git-native", "branch": "feat/x"}, 0],
        ])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("sch-watch: task finished; next: bin/sch fetch ws", result.stdout)
        self.assertNotIn("bin/sch run", result.stdout)

    def test_stale_task_names_the_recovery_steps_and_exits_1(self):
        result = self.run_watch([
            [{"state": "running", "heartbeat_utc": "2020-01-01T00:00:00Z"}, 3],
        ])
        self.assertEqual(result.returncode, 1)
        self.assertIn("*** STALE: the task is NOT running (microVM dead) ***", result.stdout)
        self.assertIn('"sch status" classifies the task as STALE', result.stdout)
        self.assertIn("1) bin/sch run ws", result.stdout)
        self.assertIn("2) bin/sch reset-session ws", result.stdout)

    def test_unreadable_status_keeps_polling(self):
        result = self.run_watch([
            [None, 1],
            [{"state": "interrupted"}, 0],
        ])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("status unreadable (S3 or credentials?)", result.stdout)
        self.assertIn("<- terminal state, watch finished", result.stdout)

    def test_no_italian_message_is_left(self):
        text = SOURCE.read_text(encoding="utf-8")
        for phrase in ("non leggibile", "credenziali", "sta girando", "VM morta",
                       '"  vivo"', "stato terminale", "watch finito"):
            self.assertNotIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
