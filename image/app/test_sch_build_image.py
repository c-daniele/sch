"""Offline tests for the build-source key of sch-build-image (TASK-20.2).

Spec: docs/specs/security/per-principal-isolation.md R33 and
docs/specs/platform/session-image-rebuild.md. The script runs for real with
fake `aws` and `zip` executables first on PATH, so no AWS call leaves the
machine; the fake `aws` records its argv.
"""

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "sch-build-image.sh"
PREFIX = "o.0123456789abcdef"
WS = "ws-" + "b" * 40

FAKE_AWS = """#!/bin/bash
printf '%s\\n' "$*" >> "${FAKE_AWS_LOG}"
case "$1 $2" in
  "codebuild start-build") echo "proj:build-1" ;;
  "codebuild batch-get-builds")
    case "$*" in
      *buildStatus*) printf 'SUCCEEDED\\tCOMPLETED\\n' ;;
      *) echo "None" ;;
    esac ;;
esac
exit 0
"""

FAKE_ZIP = """#!/bin/bash
# zip -qr <archive> ... : create the archive only.
: > "$2"
"""


def write_exe(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class SourceKeyTests(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        write_exe(bin_dir / "aws", FAKE_AWS)
        write_exe(bin_dir / "zip", FAKE_ZIP)
        ws_root = self.root / "workspace"
        (ws_root / "repo" / "image").mkdir(parents=True)
        (ws_root / "repo" / "image" / "Dockerfile").write_text("FROM scratch\n")
        (ws_root / "state").mkdir()
        (ws_root / "state" / ".sch-initialized").write_text('{"workspace": "%s"}' % WS)
        self.log = self.root / "aws.log"
        self.env = {
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(self.root),
            "TMPDIR": str(self.root),
            "SCH_IMAGE_REBUILD_PROJECT": "proj",
            "SCH_CHECKPOINT_BUCKET": "bucket",
            "SCH_WORKSPACE_ROOT": str(ws_root),
            "FAKE_AWS_LOG": str(self.log),
        }

    def run_script(self, extra_env):
        env = dict(self.env, **extra_env)
        return subprocess.run(
            ["bash", str(SCRIPT), "--timeout", "0"], env=env,
            capture_output=True, text=True, timeout=60,
        )

    def aws_calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_isolation_off_key_is_unchanged(self):
        proc = self.run_script({})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        calls = self.aws_calls()
        self.assertIn(f"s3 cp", calls[0])
        self.assertTrue(calls[0].endswith(f"s3://bucket/builds/{WS}/source.zip --only-show-errors"), calls[0])
        self.assertIn(f"--source-location-override bucket/builds/{WS}/source.zip", calls[1])

    def test_owner_prefix_scopes_the_key(self):
        proc = self.run_script({"SCH_OWNER_PREFIX": PREFIX})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        calls = self.aws_calls()
        self.assertIn(f"s3://bucket/builds/{PREFIX}/{WS}/source.zip", calls[0])
        self.assertIn(f"--source-location-override bucket/builds/{PREFIX}/{WS}/source.zip", calls[1])

    def test_empty_owner_prefix_means_isolation_off(self):
        proc = self.run_script({"SCH_OWNER_PREFIX": ""})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"s3://bucket/builds/{WS}/source.zip", self.aws_calls()[0])

    def test_invalid_owner_prefix_fails_before_any_aws_call(self):
        for bad in ("o.0123", "o.0123456789ABCDEF", "x/../o.0123456789abcdef", "o.0123456789abcdef/x"):
            proc = self.run_script({"SCH_OWNER_PREFIX": bad})
            self.assertEqual(proc.returncode, 1, bad)
            self.assertIn("invalid SCH_OWNER_PREFIX", proc.stderr)
            self.assertEqual(self.aws_calls(), [], bad)


if __name__ == "__main__":
    unittest.main()
