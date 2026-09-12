"""Offline tests for the git-native shim actions (add-git-native-workflow,
spec runtime-image: `git-seed` seed/corrupt-bundle/double-seed, `git-snapshot`
dirty/clean/no-work). Real git repos in temp dirs, no AWS."""

from __future__ import annotations

import __future__
import asyncio
import importlib.util
import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


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
    module_name = "sch_git_native_main"
    spec = importlib.util.spec_from_file_location(module_name, app_dir / "main.py")
    module = importlib.util.module_from_spec(spec)
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
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


def _git(args, cwd):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False,
        env={
            "PATH": "/usr/bin:/bin",
            "GIT_AUTHOR_NAME": "op", "GIT_AUTHOR_EMAIL": "op@local",
            "GIT_COMMITTER_NAME": "op", "GIT_COMMITTER_EMAIL": "op@local",
            "HOME": str(cwd),
        },
    )


def make_source_repo(root: Path) -> str:
    """Local 'operator' repo with two commits; returns its HEAD sha."""
    src = root / "src"
    src.mkdir()
    _git(["init", "-q"], src)
    (src / "a.txt").write_text("a\n")
    _git(["add", "-A"], src)
    _git(["commit", "-qm", "one"], src)
    (src / "b.txt").write_text("b\n")
    _git(["add", "-A"], src)
    _git(["commit", "-qm", "two"], src)
    return _git(["rev-parse", "HEAD"], src).stdout.strip()


def make_seed_bundle(root: Path) -> Path:
    bundle = root / "seed.bundle"
    _git(["bundle", "create", str(bundle), "HEAD"], root / "src")
    return bundle


class GitNativeBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _git(["init", "-q"], self.repo)  # mirrors init-workspace.sh
        self.staging = self.root / "state" / "bundles"
        self.staging.mkdir(parents=True)
        main.REPO_DIR = self.repo
        main.BUNDLE_STAGING_DIR = self.staging
        main.GIT_NATIVE_STATE_FILE = self.root / "state" / "git-native.json"
        main._WORKSPACE_READY.set()
        main._WORKSPACE_NAME["value"] = "ws-a"
        main._SESSION_EPOCH["value"] = 0
        main._STORAGE_BACKEND["value"] = "session"
        main.CHECKPOINT_BUCKET = ""  # keep the writer fence out of offline tests
        self.head = make_source_repo(self.root)

    def stage_seed(self):
        bundle = make_seed_bundle(self.root)
        target = self.staging / main.SEED_BUNDLE_NAME
        target.write_bytes(bundle.read_bytes())
        return target


class GitSeedTests(GitNativeBase):
    def test_successful_seed(self):
        self.stage_seed()
        resp = main._handle_git_seed({"branch": "feat/x"})
        self.assertEqual(resp["status"], "ok")
        self.assertEqual(resp["baseSha"], self.head)
        current = _git(["rev-parse", "--abbrev-ref", "HEAD"], self.repo).stdout.strip()
        self.assertEqual(current, "feat/x")
        self.assertEqual(
            _git(["rev-parse", "HEAD"], self.repo).stdout.strip(), self.head
        )
        # worktree contains the seeded files
        self.assertTrue((self.repo / "a.txt").is_file())
        # staging bundle consumed
        self.assertFalse((self.staging / main.SEED_BUNDLE_NAME).exists())
        # state file persisted for git-snapshot / recovery
        state = json.loads(main.GIT_NATIVE_STATE_FILE.read_text())
        self.assertEqual(state["branch"], "feat/x")
        self.assertEqual(state["baseSha"], self.head)

    def test_missing_bundle(self):
        resp = main._handle_git_seed({"branch": "feat/x"})
        self.assertEqual(resp["status"], "error")
        self.assertIn("not found", resp["error"])
        # no partial mutations: repo still has no history, no state file
        self.assertNotEqual(_git(["rev-parse", "HEAD"], self.repo).returncode, 0)
        self.assertFalse(main.GIT_NATIVE_STATE_FILE.exists())

    def test_corrupt_bundle(self):
        target = self.staging / main.SEED_BUNDLE_NAME
        target.write_bytes(b"# not a bundle\n" + b"garbage" * 100)
        resp = main._handle_git_seed({"branch": "feat/x"})
        self.assertEqual(resp["status"], "error")
        self.assertNotEqual(_git(["rev-parse", "HEAD"], self.repo).returncode, 0)
        self.assertFalse(main.GIT_NATIVE_STATE_FILE.exists())

    def test_double_seed_refused(self):
        self.stage_seed()
        first = main._handle_git_seed({"branch": "feat/x"})
        self.assertEqual(first["status"], "ok")
        head_before = _git(["rev-parse", "HEAD"], self.repo).stdout.strip()
        self.stage_seed()
        second = main._handle_git_seed({"branch": "feat/y"})
        self.assertEqual(second["status"], "error")
        self.assertIn("feat/x", second["error"])  # names the provisioned branch
        # existing repo untouched
        self.assertEqual(
            _git(["rev-parse", "HEAD"], self.repo).stdout.strip(), head_before
        )

    def test_invalid_branch_name(self):
        self.stage_seed()
        resp = main._handle_git_seed({"branch": "feat//bad name"})
        self.assertEqual(resp["status"], "error")
        self.assertIn("invalid branch", resp["error"])

    def test_missing_branch(self):
        resp = main._handle_git_seed({})
        self.assertEqual(resp["status"], "error")

    def test_seed_refused_over_existing_history(self):
        # A repo with commits but no git-native state (e.g. mirror-synced or
        # pre-existing clone) must not be overwritten.
        (self.repo / "x.txt").write_text("x\n")
        _git(["add", "-A"], self.repo)
        _git(["commit", "-qm", "pre-existing"], self.repo)
        self.stage_seed()
        resp = main._handle_git_seed({"branch": "feat/x"})
        self.assertEqual(resp["status"], "error")
        self.assertIn("existing", resp["error"])


class GitSnapshotTests(GitNativeBase):
    def seed(self, branch="feat/x"):
        self.stage_seed()
        resp = main._handle_git_seed({"branch": branch})
        self.assertEqual(resp["status"], "ok")
        return resp

    def test_snapshot_unseeded(self):
        resp = main._handle_git_snapshot({})
        self.assertEqual(resp["status"], "error")
        self.assertIn("not seeded", resp["error"])

    def test_no_work(self):
        self.seed()
        resp = main._handle_git_snapshot({})
        self.assertEqual(resp["status"], "no-work")
        self.assertEqual(resp["headSha"], self.head)
        self.assertFalse(resp["snapshotCommitted"])
        self.assertFalse((self.staging / main.DELIVERY_BUNDLE_NAME).exists())

    def test_dirty_worktree_service_commit(self):
        self.seed()
        (self.repo / "wip.txt").write_text("uncommitted\n")
        resp = main._handle_git_snapshot({})
        self.assertEqual(resp["status"], "ok")
        self.assertTrue(resp["snapshotCommitted"])
        self.assertEqual(resp["branch"], "feat/x")
        self.assertEqual(resp["bundleRef"], main.DELIVERY_BUNDLE_NAME)
        # service commit convention (design D4)
        show = _git(["show", "-s", "--format=%an|%ae|%s", "HEAD"], self.repo).stdout.strip()
        author_name, author_email, subject = show.split("|", 2)
        self.assertEqual(author_name, "sch-session ws-a")
        self.assertEqual(author_email, "sch-session@local")
        self.assertTrue(subject.startswith("wip: session snapshot ("))
        # bundle importable into the source repo
        bundle = self.staging / main.DELIVERY_BUNDLE_NAME
        self.assertTrue(bundle.is_file())
        imp = _git(["fetch", str(bundle), "feat/x:feat/x"], self.root / "src")
        self.assertEqual(imp.returncode, 0, imp.stderr)
        self.assertEqual(
            _git(["rev-parse", "feat/x"], self.root / "src").stdout.strip(),
            resp["headSha"],
        )

    def test_agent_commits_clean_worktree(self):
        self.seed()
        (self.repo / "done.txt").write_text("agent work\n")
        _git(["add", "-A"], self.repo)
        _git(["commit", "-qm", "agent commit"], self.repo)
        resp = main._handle_git_snapshot({})
        self.assertEqual(resp["status"], "ok")
        self.assertFalse(resp["snapshotCommitted"])
        self.assertNotEqual(resp["headSha"], self.head)

    def test_idempotent_on_clean_worktree(self):
        self.seed()
        (self.repo / "wip.txt").write_text("uncommitted\n")
        first = main._handle_git_snapshot({})
        self.assertEqual(first["status"], "ok")
        second = main._handle_git_snapshot({})
        self.assertEqual(second["status"], "ok")
        self.assertFalse(second["snapshotCommitted"])
        self.assertEqual(first["headSha"], second["headSha"])


class InvokeDispatchTests(GitNativeBase):
    def test_invoke_routes_git_seed(self):
        self.stage_seed()
        resp = main.invoke({
            "action": "git-seed", "workspace": "ws-a", "branch": "feat/x",
        })
        self.assertEqual(resp["status"], "ok")
        self.assertEqual(resp["action"], "git-seed")

    def test_unknown_action_lists_git_verbs(self):
        resp = main.invoke({"action": "definitely-not-an-action"})
        self.assertIn("git-seed", resp["message"])
        self.assertIn("git-snapshot", resp["message"])


if __name__ == "__main__":
    unittest.main()
