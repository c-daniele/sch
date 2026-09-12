import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class CleanupScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        source = Path(__file__).resolve().parents[2] / "bin" / "sch-cleanup"
        shutil.copy2(source, self.bin_dir / "sch-cleanup")
        fake_sch = self.bin_dir / "sch"
        fake_sch.write_text("#!/bin/bash\nprintf '%s\\n' \"$2\" >> \"$SCH_TEST_STOPS\"\n")
        fake_sch.chmod(0o755)
        self.config = self.root / "config" / "sch"
        (self.config / "workspaces").mkdir(parents=True)
        (self.config / "sync" / "bindings").mkdir(parents=True)
        (self.config / "mirrors").mkdir(parents=True)
        (self.config / "workspaces" / "alpha").write_text("{}\n")
        (self.config / "workspaces" / "beta").write_text("{}\n")
        (self.config / "workspaces" / ".status.alpha").write_text("created\n")
        (self.config / "sync" / "bindings" / "one.json").write_text("{}\n")
        (self.config / "runtime-arn").write_text("arn\n")
        (self.config / "checkpoint-bucket").write_text("bucket\n")
        self.tmp = self.root / "tmp"
        (self.tmp / "sch-old" / "nested").mkdir(parents=True)
        self.stops = self.root / "stops"
        self.env = {
            **os.environ,
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "TMPDIR": str(self.tmp),
            "SCH_TEST_STOPS": str(self.stops),
        }

    def run_cleanup(self, *args):
        return subprocess.run(
            [str(self.bin_dir / "sch-cleanup"), *args],
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_dry_run_preserves_state(self):
        result = self.run_cleanup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Dry run only", result.stdout)
        self.assertTrue((self.config / "workspaces" / "alpha").exists())
        self.assertTrue((self.tmp / "sch-old").exists())
        self.assertFalse(self.stops.exists())

    def test_yes_stops_workspaces_and_removes_only_runtime_state(self):
        result = self.run_cleanup("--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.stops.read_text().splitlines(), ["alpha", "beta"])
        self.assertFalse((self.config / "workspaces").exists())
        self.assertFalse((self.config / "sync").exists())
        self.assertFalse((self.config / "mirrors").exists())
        self.assertFalse((self.tmp / "sch-old").exists())
        self.assertTrue((self.config / "runtime-arn").exists())
        self.assertTrue((self.config / "checkpoint-bucket").exists())


if __name__ == "__main__":
    unittest.main()
