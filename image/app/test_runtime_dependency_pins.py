"""Regression guards for runtime dependency versions with required fixes."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


DOCKERFILE = Path(__file__).parents[1] / "Dockerfile"


class RuntimeDependencyPinTests(unittest.TestCase):
    def test_opencode_pinned_on_the_2x_line_from_the_v2_package(self):
        # TASK-7: OpenCode 2 ships as `@opencode/cli` (the 1.x `opencode-ai`
        # package stops at 1.18.x). The 1.x headless floor (1.18.20, subagent
        # permission hang) is implied by any 2.x pin.
        text = DOCKERFILE.read_text(encoding="utf-8")
        match = re.search(r"^ARG OPENCODE_VERSION=(\d+)\.(\d+)\.(\d+)$", text, re.MULTILINE)
        self.assertIsNotNone(match, "Dockerfile must pin OPENCODE_VERSION")
        self.assertGreaterEqual(tuple(map(int, match.groups())), (2, 0, 0))
        self.assertIn('"@opencode/cli@${OPENCODE_VERSION}"', text)
        self.assertNotIn("opencode-ai@", text)
        # The version assert must strip the `opencode v` prefix 2.x prints.
        self.assertIn("sed -E 's/^opencode[[:space:]]+v?//'", text)

    def test_harness_pins_match_task_7(self):
        text = DOCKERFILE.read_text(encoding="utf-8")
        pins = dict(re.findall(r"^ARG (OPENCODE_VERSION|PI_VERSION|CLAUDE_CODE_VERSION)=(\S+)$", text, re.MULTILINE))
        self.assertEqual(pins, {
            "OPENCODE_VERSION": "2.0.18",
            "PI_VERSION": "0.87.1",
            "CLAUDE_CODE_VERSION": "2.1.282",
        })

    def test_gh_cli_pinned_with_versioned_install(self):
        # TASK-26: gh must stay pinned (no floating tag) and installed from
        # the exact-version release tarball with a build-time version assert.
        text = DOCKERFILE.read_text(encoding="utf-8")
        match = re.search(r"^ARG GH_VERSION=(\d+)\.(\d+)\.(\d+)$", text, re.MULTILINE)
        self.assertIsNotNone(match, "Dockerfile must pin GH_VERSION")
        self.assertIn(
            "github.com/cli/cli/releases/download/v${GH_VERSION}"
            "/gh_${GH_VERSION}_linux_${GH_ARCH}.tar.gz",
            text,
            "gh must install from the versioned release tarball",
        )
        self.assertIn("test \"$(gh --version", text)


if __name__ == "__main__":
    unittest.main()
