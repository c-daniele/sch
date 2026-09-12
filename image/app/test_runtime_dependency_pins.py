"""Regression guards for runtime dependency versions with required fixes."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


DOCKERFILE = Path(__file__).parents[1] / "Dockerfile"


class RuntimeDependencyPinTests(unittest.TestCase):
    def test_opencode_supports_subagent_permissions_in_headless_run(self):
        text = DOCKERFILE.read_text(encoding="utf-8")
        match = re.search(r"^ARG OPENCODE_VERSION=(\d+)\.(\d+)\.(\d+)$", text, re.MULTILINE)
        self.assertIsNotNone(match, "Dockerfile must pin OPENCODE_VERSION")
        self.assertGreaterEqual(
            tuple(map(int, match.groups())),
            (1, 18, 20),
            "OpenCode < 1.18.20 can hang when a headless subagent asks permission",
        )

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
