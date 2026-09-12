import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sch import repo


def make_repo_like(root):
    """Create the minimal marker layout repo.py validates against."""
    tunnel = Path(root) / "tunnel"
    tunnel.mkdir(parents=True, exist_ok=True)
    (tunnel / "sync.js").write_text("")
    return Path(root)


class ManagedRootTests(unittest.TestCase):
    def test_posix_uses_xdg_data_home(self):
        with tempfile.TemporaryDirectory() as td, \
             patch.object(repo.sys, "platform", "linux"), \
             patch.dict(os.environ, {"XDG_DATA_HOME": td}):
            self.assertEqual(
                repo.managed_root(), Path(td) / "sch" / "repo"
            )

    def test_posix_defaults_to_local_share(self):
        with patch.object(repo.sys, "platform", "linux"), \
             patch.dict(os.environ, {}, clear=False):
            os.environ.pop("XDG_DATA_HOME", None)
            self.assertEqual(
                repo.managed_root(), Path.home() / ".local" / "share" / "sch" / "repo"
            )

    def test_windows_uses_localappdata(self):
        with tempfile.TemporaryDirectory() as td, \
             patch.object(repo.sys, "platform", "win32"), \
             patch.dict(os.environ, {"LOCALAPPDATA": td}):
            self.assertEqual(
                repo.managed_root(), Path(td) / "sch" / "repo"
            )


class RepoRootResolutionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = make_repo_like(self._tmp.name)
        # Isolate from the developer's real environment in every test.
        patcher = patch.dict(os.environ, {"SCH_REPO_ROOT": ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_env_override_wins(self):
        with patch.dict(os.environ, {"SCH_REPO_ROOT": str(self.root)}), \
             patch.object(repo, "managed_root", return_value=Path("/nonexistent")):
            self.assertEqual(repo.repo_root(), self.root)

    def test_invalid_env_override_is_rejected_not_fallback(self):
        with tempfile.TemporaryDirectory() as empty, \
             patch.dict(os.environ, {"SCH_REPO_ROOT": empty}):
            self.assertIsNone(repo.repo_root())

    def test_managed_checkout_beats_legacy(self):
        with tempfile.TemporaryDirectory() as managed, \
             patch.object(repo, "managed_root", return_value=make_repo_like(managed)):
            self.assertEqual(repo.repo_root(), Path(managed))

    def test_legacy_checkout_fallback(self):
        with patch.object(repo, "managed_root", return_value=Path("/nonexistent")):
            # This test suite itself runs from a git checkout of the repo.
            self.assertEqual(repo.repo_root(), repo.legacy_root())

    def test_no_repo_found(self):
        with patch.object(repo, "managed_root", return_value=Path("/nonexistent")), \
             patch.object(repo, "legacy_root", return_value=Path("/nonexistent")):
            self.assertIsNone(repo.repo_root())

    def test_tunnel_dir_and_missing_message(self):
        self.assertEqual(repo.tunnel_dir(), repo.repo_root() / "tunnel")
        message = repo.missing_message("sync")
        self.assertIn("sync requires", message)
        self.assertIn("SCH_REPO_ROOT", message)
        self.assertIn("sch setup", message)


if __name__ == "__main__":
    unittest.main()


class DefaultRepoUrlTests(unittest.TestCase):
    """The clone URL comes from the installed package metadata (pyproject.toml
    Homepage), so a repository move is a single edit; the constant is only the
    fallback for a checkout that was never installed."""

    def _with_metadata(self, entries):
        class Meta:
            def get_all(self, key):
                return entries if key == "Project-URL" else None

        return patch("importlib.metadata.metadata", return_value=Meta())

    def test_homepage_becomes_git_url(self):
        with self._with_metadata(["Homepage, https://github.com/acme/sch"]):
            self.assertEqual(repo.default_repo_url(), "https://github.com/acme/sch.git")

    def test_homepage_with_git_suffix_is_kept(self):
        with self._with_metadata(["Homepage, https://github.com/acme/sch.git/"]):
            self.assertEqual(repo.default_repo_url(), "https://github.com/acme/sch.git")

    def test_missing_metadata_falls_back_to_constant(self):
        with patch("importlib.metadata.metadata", side_effect=Exception("not installed")):
            self.assertEqual(repo.default_repo_url(), repo._FALLBACK_REPO_URL)

    def test_other_labels_are_ignored(self):
        with self._with_metadata(["Source, https://example.com/x", "Homepage, https://github.com/acme/sch"]):
            self.assertEqual(repo.default_repo_url(), "https://github.com/acme/sch.git")
