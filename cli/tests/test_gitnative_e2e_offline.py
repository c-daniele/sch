"""Offline end-to-end integration of the git-native workflow chain
(add-git-native-workflow, local counterpart of bin/verify-git-native.sh):

    operator repo -> git bundle -> tunnel/bundle.js upload -> staging dir
    -> shim _handle_git_seed -> agent commits -> shim _handle_git_snapshot
    -> tunnel/bundle.js download -> `sch fetch` import -> merge

Two "workspaces" are seeded from the same HEAD (the parallel scenario of
task 6.3) with the REAL shim module, the REAL Node bundle helper piped to
the REAL fs_sync_worker.py, and the REAL fetch command — only the AgentCore
invocation layer is stubbed (invoke_verified routed to the in-process shim
handlers, bundle_helper_argv rewired to a local pipe transport via a tiny
Node driver). Requires node + the tunnel deps; skipped otherwise.
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_CLI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _CLI_DIR not in sys.path:
    sys.path.insert(0, _CLI_DIR)

_REPO_ROOT = Path(_CLI_DIR).parent
_TUNNEL_DIR = _REPO_ROOT / "tunnel"
_APP_DIR = _REPO_ROOT / "image" / "app"

from sch import gitnative, runtime, workspace
from sch.commands import fetch as fetch_cmd
from sch.config import Config

_NODE = shutil.which("node")

# Tiny Node driver: runs the real runBundleTransfer against the real
# fs_sync_worker.py over a local pipe instead of the AgentCore WebSocket.
_DRIVER = """
import { spawn } from 'node:child_process';
import { Duplex } from 'node:stream';
import { runBundleTransfer } from '%(tunnel)s/bundle.js';

const [,, stagingRoot, direction, localPath, name] = process.argv;
const child = spawn('python3', ['%(app)s/fs_sync_worker.py', '--root', stagingRoot], { stdio: ['pipe', 'pipe', 'pipe'] });
child.stderr.on('data', () => {});
const stream = Duplex.from({ readable: child.stdout, writable: child.stdin });
stream.on('error', () => {});
stream.closeStream = async () => { stream.destroy(); child.kill('SIGTERM'); };
const options = {
  region: 'test', runtimeArn: 'test', sessionId: 'test', workspace: 'ws',
  storage: 'session', sessionEpoch: 0, stagingRoot, name,
};
if (direction === 'upload') options.upload = localPath; else options.download = localPath;
try {
  const result = await runBundleTransfer(options, { log: () => {}, transport: { open: async () => stream } });
  process.stdout.write(JSON.stringify({ type: 'done', ...result }) + '\\n');
} catch (error) {
  process.stdout.write(JSON.stringify({ type: 'error', message: error.message }) + '\\n');
  process.exitCode = 1;
} finally {
  child.kill('SIGTERM');
}
""" % {"tunnel": _TUNNEL_DIR, "app": _APP_DIR}


def _git(repo, args, check=True):
    proc = subprocess.run(
        ["git", "-C", str(repo)] + list(args), capture_output=True, text=True,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "GIT_AUTHOR_NAME": "op", "GIT_AUTHOR_EMAIL": "op@local",
            "GIT_COMMITTER_NAME": "op", "GIT_COMMITTER_EMAIL": "op@local",
            "HOME": str(repo),
        },
    )
    if check and proc.returncode != 0:
        raise AssertionError("git {} failed: {}".format(args, proc.stderr))
    return proc


def _load_shim():
    sys.path.insert(0, str(_APP_DIR))
    import test_git_native  # reuses its FakeApp/load_main plumbing

    return test_git_native.load_main()


class RemoteWorkspace:
    """One simulated remote workspace: its own repo dir, staging dir, and a
    dedicated shim module instance (module-level state is per-workspace)."""

    def __init__(self, root, name):
        self.name = name
        self.root = Path(root) / name
        self.repo = self.root / "repo"
        self.staging = self.root / "state" / "bundles"
        self.repo.mkdir(parents=True)
        self.staging.mkdir(parents=True)
        _git(self.repo, ["init", "-q"])  # init-workspace.sh equivalent
        self.shim = _load_shim()
        self.shim.REPO_DIR = self.repo
        self.shim.BUNDLE_STAGING_DIR = self.staging
        self.shim.GIT_NATIVE_STATE_FILE = self.root / "state" / "git-native.json"
        self.shim._WORKSPACE_READY.set()
        self.shim._WORKSPACE_NAME["value"] = name

    def invoke(self, payload):
        action = payload.get("action")
        if action == "git-seed":
            return self.shim._handle_git_seed(payload)
        if action == "git-snapshot":
            return self.shim._handle_git_snapshot(payload)
        raise AssertionError("unexpected action " + str(action))

    def agent_commit(self, filename, content, message):
        (self.repo / filename).write_text(content)
        _git(self.repo, ["add", "-A"])
        _git(self.repo, ["commit", "-qm", message])

    def agent_dirty(self, filename, content):
        (self.repo / filename).write_text(content)


@unittest.skipUnless(_NODE, "node not available")
@unittest.skipUnless((_TUNNEL_DIR / "node_modules").is_dir(), "tunnel deps not installed")
class GitNativeOfflineE2ETests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.driver = self.root / "driver.mjs"
        self.driver.write_text(_DRIVER)

        # operator repo (the local HEAD both workspaces seed from)
        self.operator = self.root / "operator"
        self.operator.mkdir()
        _git(self.operator, ["init", "-q"])
        (self.operator / "shared.txt").write_text("shared\n")
        _git(self.operator, ["add", "-A"])
        _git(self.operator, ["commit", "-qm", "base"])
        self.head = _git(self.operator, ["rev-parse", "HEAD"]).stdout.strip()

        old = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = str(self.root / "xdg")
        self.cfg = Config()
        if old is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = old

        self.remotes = {}

    def _resolved(self):
        return type("Resolved", (), {
            "sid": "sid", "harness": "claude", "identity": "ws",
            "storage": "session", "epoch": 1, "was_created": False,
        })()

    def _transfer(self, remote, direction, local_path, name):
        """Real bundle.js transfer against the workspace's real staging dir."""
        proc = subprocess.run(
            [_NODE, str(self.driver), str(remote.staging), direction, str(local_path), name],
            capture_output=True, text=True, timeout=120,
        )
        control = json.loads(proc.stdout.strip().splitlines()[-1])
        if control.get("type") != "done":
            raise AssertionError("transfer failed: " + str(control))
        return control

    def _patched_helper(self, remote):
        """Route the CLI's bundle helper through the local pipe transport."""
        def fake_argv(cfg, arn, sid, ws, storage, epoch, name, upload=None, download=None):
            return ["LOCAL", name, str(upload or ""), str(download or "")]

        def fake_run(argv, doing):
            _, name, upload, download = argv
            if upload:
                return self._transfer(remote, "upload", upload, name)
            return self._transfer(remote, "download", download, name)

        return fake_argv, fake_run

    def _seed_workspace(self, ws_name, branch):
        remote = RemoteWorkspace(self.root, ws_name)
        self.remotes[ws_name] = remote
        workspace.save_workspace_state(self.cfg, ws_name, "sid", "claude", storage="session", epoch=1)
        mode = gitnative.GitNativeMode(branch, seeded=False)
        fake_argv, fake_run = self._patched_helper(remote)

        def fake_invoke(cfg, sid, payload, op):
            return runtime.InvocationResult(True, json.dumps(remote.invoke(json.loads(payload))))

        err = io.StringIO()
        with patch.object(gitnative, "local_repo_root", return_value=self.operator), \
             patch.object(gitnative, "bundle_helper_argv", side_effect=fake_argv), \
             patch.object(gitnative, "run_bundle_helper", side_effect=fake_run), \
             patch.object(gitnative.runtime, "invoke_verified", side_effect=fake_invoke), \
             contextlib.redirect_stderr(err):
            gitnative.ensure_seeded(
                self.cfg, ws_name, mode, "arn", "sid", ws_name, "claude", "session", 1,
            )
        return remote, mode

    def _fetch(self, ws_name, extra_args=()):
        remote = self.remotes[ws_name]
        fake_argv, fake_run = self._patched_helper(remote)
        warm = runtime.InvocationResult(True, json.dumps({"status": "ok", "storage": "session"}))

        def fake_invoke(cfg, sid, payload, op):
            data = json.loads(payload)
            if data.get("action") == "noop":
                return warm
            return runtime.InvocationResult(True, json.dumps(remote.invoke(data)))

        err = io.StringIO()
        with patch.object(fetch_cmd.harness_mod, "resolve_harness", return_value=self._resolved()), \
             patch.object(fetch_cmd, "runtime_arn", return_value="arn"), \
             patch.object(fetch_cmd.runtime, "invoke_verified", side_effect=fake_invoke), \
             patch.object(fetch_cmd.gitnative, "bundle_helper_argv", side_effect=fake_argv), \
             patch.object(fetch_cmd.gitnative, "run_bundle_helper", side_effect=fake_run), \
             contextlib.redirect_stderr(err):
            rc = fetch_cmd.cmd_fetch(self.cfg, [ws_name] + list(extra_args))
        return rc, err.getvalue()

    def test_parallel_seed_task_fetch_merge(self):
        """Task 6.3 parallel scenario, offline: two workspaces from the same
        HEAD, independent work, two fetches, clean merge of both branches."""
        remote_a, _ = self._seed_workspace("ws-a", "verify/a")
        remote_b, _ = self._seed_workspace("ws-b", "verify/b")

        # both seeded at the operator HEAD
        self.assertEqual(_git(remote_a.repo, ["rev-parse", "HEAD"]).stdout.strip(), self.head)
        self.assertEqual(_git(remote_b.repo, ["rev-parse", "HEAD"]).stdout.strip(), self.head)

        # "tasks": independent agent work in each workspace
        remote_a.agent_commit("from-a.txt", "work A\n", "task A")
        remote_b.agent_commit("from-b.txt", "work B\n", "task B")

        rc_a, _ = self._fetch("ws-a")
        rc_b, _ = self._fetch("ws-b")
        self.assertEqual((rc_a, rc_b), (0, 0))

        # both branches exist locally with their own work, no contamination
        self.assertEqual(
            _git(self.operator, ["show", "verify/a:from-a.txt"]).stdout, "work A\n"
        )
        self.assertEqual(
            _git(self.operator, ["show", "verify/b:from-b.txt"]).stdout, "work B\n"
        )
        self.assertNotEqual(
            _git(self.operator, ["show", "verify/a:from-b.txt"], check=False).returncode, 0
        )

        # merge both without overwrites
        _git(self.operator, ["merge", "-q", "--no-edit", "verify/a"])
        _git(self.operator, ["merge", "-q", "--no-edit", "verify/b"])
        self.assertTrue((self.operator / "from-a.txt").is_file())
        self.assertTrue((self.operator / "from-b.txt").is_file())
        self.assertTrue((self.operator / "shared.txt").is_file())

    def test_recovery_dirty_worktree_service_snapshot(self):
        """Task 6.4 mechanics, offline: commits + dirty state both delivered;
        the service commit carries the sch-session convention."""
        remote, _ = self._seed_workspace("ws-r", "verify/r")
        remote.agent_commit("committed.txt", "committed work\n", "committed")
        remote.agent_dirty("uncommitted.txt", "dirty state\n")

        rc, _ = self._fetch("ws-r")
        self.assertEqual(rc, 0)
        self.assertEqual(
            _git(self.operator, ["show", "verify/r:committed.txt"]).stdout,
            "committed work\n",
        )
        self.assertEqual(
            _git(self.operator, ["show", "verify/r:uncommitted.txt"]).stdout,
            "dirty state\n",
        )
        log = _git(self.operator, ["log", "verify/r", "--format=%an|%s"]).stdout
        self.assertIn("sch-session ws-r|wip: session snapshot (", log)

    def test_double_fetch_idempotent(self):
        remote, _ = self._seed_workspace("ws-i", "verify/i")
        remote.agent_commit("x.txt", "x\n", "work")
        rc1, _ = self._fetch("ws-i")
        sha1 = _git(self.operator, ["rev-parse", "verify/i"]).stdout.strip()
        rc2, err2 = self._fetch("ws-i")
        sha2 = _git(self.operator, ["rev-parse", "verify/i"]).stdout.strip()
        self.assertEqual((rc1, rc2), (0, 0))
        self.assertEqual(sha1, sha2)
        self.assertIn("already up to date", err2)


if __name__ == "__main__":
    unittest.main()
