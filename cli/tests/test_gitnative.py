"""Unit tests for the git-native session mode (add-git-native-workflow):
`--branch` parsing/mutual exclusion on run/task, workspace state persistence,
re-seed skip, branch-name validation, payload builders, and the `sch fetch`
command flows (no-work, divergence, force, push, idempotence)."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_CLI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _CLI_DIR not in sys.path:
    sys.path.insert(0, _CLI_DIR)

from sch import gitnative, runtime, workspace
from sch.commands import fetch as fetch_cmd
from sch.commands import run as run_cmd
from sch.commands import task as task_cmd
from sch.config import Config


def _config(td):
    old = os.environ.get("XDG_CONFIG_HOME")
    os.environ["XDG_CONFIG_HOME"] = td
    try:
        return Config()
    finally:
        if old is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = old


def _git(repo, args):
    return subprocess.run(
        ["git", "-C", str(repo)] + list(args), capture_output=True, text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "GIT_AUTHOR_NAME": "op", "GIT_AUTHOR_EMAIL": "op@local",
            "GIT_COMMITTER_NAME": "op", "GIT_COMMITTER_EMAIL": "op@local",
            "HOME": str(repo),
        },
    )


def make_repo(root, name="src"):
    repo = Path(root) / name
    repo.mkdir()
    _git(repo, ["init", "-q"])
    (repo / "a.txt").write_text("a\n")
    _git(repo, ["add", "-A"])
    _git(repo, ["commit", "-qm", "one"])
    return repo


class BranchValidationTests(unittest.TestCase):
    def test_valid_branch_names(self):
        for name in ("feat/x", "change/task-model-flag", "fix-123", "a.b_c"):
            with self.subTest(name=name):
                self.assertEqual(gitnative.validate_branch_or_die(name), name)

    def test_invalid_branch_names_die(self):
        for name in ("", "feat//bad name", "-leading-dash", "a..b", "end/", "x y"):
            with self.subTest(name=name):
                with self.assertRaises(SystemExit):
                    gitnative.validate_branch_or_die(name)


class FlagExclusionTests(unittest.TestCase):
    def _options(self, **overrides):
        options = {"sync": "", "no_sync": False, "bootstrap": "abort",
                   "conflict": "abort", "storage": ""}
        options.update(overrides)
        return options

    def test_branch_alone_is_accepted(self):
        gitnative.check_flag_exclusion_or_die("feat/x", self._options())

    def test_branch_with_sync_dies(self):
        with self.assertRaises(SystemExit):
            gitnative.check_flag_exclusion_or_die("feat/x", self._options(sync="/p"))

    def test_branch_with_bootstrap_dies(self):
        with self.assertRaises(SystemExit):
            gitnative.check_flag_exclusion_or_die("feat/x", self._options(bootstrap="union"))

    def test_branch_with_conflict_dies(self):
        with self.assertRaises(SystemExit):
            gitnative.check_flag_exclusion_or_die("feat/x", self._options(conflict="local-wins"))

    def test_no_branch_ignores_sync_options(self):
        gitnative.check_flag_exclusion_or_die("", self._options(sync="/p"))


class ParseArgsTests(unittest.TestCase):
    def test_task_parses_branch(self):
        result = task_cmd._parse_args(["ws", "--branch", "feat/x", "build"])
        self.assertEqual(result[4], "feat/x")
        self.assertEqual(result[7], "build")

    def test_run_parses_branch(self):
        result = run_cmd._parse_args(["ws", "--branch", "feat/x"])
        self.assertEqual(result[3], "feat/x")

    def test_task_branch_with_sync_dies_before_any_invocation(self):
        with self.assertRaises(SystemExit):
            task_cmd._parse_args(["ws", "--branch", "feat/x", "--sync", "/p", "build"])

    def test_run_branch_with_sync_dies(self):
        with self.assertRaises(SystemExit):
            run_cmd._parse_args(["ws", "--branch", "feat/x", "--sync", "/p"])

    def test_run_branch_with_conflict_policy_dies(self):
        with self.assertRaises(SystemExit):
            run_cmd._parse_args(["ws", "--branch", "feat/x", "--conflict", "local-wins"])

    def test_task_invalid_branch_dies_without_runtime_contact(self):
        with self.assertRaises(SystemExit):
            task_cmd._parse_args(["ws", "--branch", "feat//bad name", "build"])

    def test_task_missing_branch_value_dies(self):
        with self.assertRaises(SystemExit):
            task_cmd._parse_args(["ws", "--branch"])


class WorkspaceStateTests(unittest.TestCase):
    def test_git_native_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _config(td)
            workspace.save_workspace_state(cfg, "ws", "sid-1", "claude", storage="s3", epoch=1)
            workspace.save_git_native_state(cfg, "ws", "feat/x", "abc123", "/repo/root")
            state = workspace.read_workspace_state(cfg, "ws")
            self.assertEqual(state.git_native["branch"], "feat/x")
            self.assertEqual(state.git_native["baseSha"], "abc123")
            self.assertEqual(state.git_native["localRepo"], "/repo/root")
            self.assertEqual(state.sid, "sid-1")

    def test_save_workspace_state_preserves_git_native(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _config(td)
            workspace.save_workspace_state(cfg, "ws", "sid-1", "claude", storage="s3", epoch=1)
            workspace.save_git_native_state(cfg, "ws", "feat/x", "abc123", "/repo/root")
            # e.g. `sch list` under the registry re-saves the state
            workspace.save_workspace_state(cfg, "ws", "sid-1", "claude", storage="s3", epoch=1)
            state = workspace.read_workspace_state(cfg, "ws")
            self.assertIsNotNone(state.git_native)
            self.assertEqual(state.git_native["branch"], "feat/x")

    def test_workspace_without_git_native_reads_none(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _config(td)
            workspace.save_workspace_state(cfg, "ws", "sid-1", "claude", storage="s3", epoch=1)
            self.assertIsNone(workspace.read_workspace_state(cfg, "ws").git_native)


class ResolveModeTests(unittest.TestCase):
    def _options(self, **overrides):
        options = {"sync": "", "no_sync": False, "bootstrap": "abort",
                   "conflict": "abort", "storage": ""}
        options.update(overrides)
        return options

    def test_no_flag_no_state_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _config(td)
            self.assertIsNone(gitnative.resolve_mode(cfg, "ws", "ws", "", self._options()))

    def test_flag_on_fresh_workspace_returns_unseeded_mode(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _config(td)
            mode = gitnative.resolve_mode(cfg, "ws", "ws", "feat/x", self._options())
            self.assertEqual(mode.branch, "feat/x")
            self.assertFalse(mode.seeded)

    def test_seeded_workspace_reuses_branch_without_flag(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _config(td)
            workspace.save_workspace_state(cfg, "ws", "sid", "claude", storage="s3", epoch=1)
            workspace.save_git_native_state(cfg, "ws", "feat/x", "abc", "/r")
            mode = gitnative.resolve_mode(cfg, "ws", "ws", "", self._options())
            self.assertTrue(mode.seeded)
            self.assertEqual(mode.branch, "feat/x")
            self.assertEqual(mode.base_sha, "abc")

    def test_seeded_workspace_rejects_sync(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _config(td)
            workspace.save_workspace_state(cfg, "ws", "sid", "claude", storage="s3", epoch=1)
            workspace.save_git_native_state(cfg, "ws", "feat/x", "abc", "/r")
            with self.assertRaises(SystemExit):
                gitnative.resolve_mode(cfg, "ws", "ws", "", self._options(sync="/p"))

    def test_seeded_workspace_rejects_different_branch(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _config(td)
            workspace.save_workspace_state(cfg, "ws", "sid", "claude", storage="s3", epoch=1)
            workspace.save_git_native_state(cfg, "ws", "feat/x", "abc", "/r")
            with self.assertRaises(SystemExit):
                gitnative.resolve_mode(cfg, "ws", "ws", "feat/other", self._options())

    def test_sync_bound_workspace_rejects_branch(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _config(td)
            from sch import sync as sync_mod
            project = Path(td) / "project"
            project.mkdir()
            sync_mod.save_binding(cfg, "ws", project)
            with self.assertRaises(SystemExit):
                gitnative.resolve_mode(cfg, "ws", "ws", "feat/x", self._options())


class EnsureSeededTests(unittest.TestCase):
    def test_seeded_mode_skips_all_transfer(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _config(td)
            mode = gitnative.GitNativeMode("feat/x", "abc", "/r", seeded=True)
            err = io.StringIO()
            with patch.object(gitnative, "run_bundle_helper") as helper, \
                 patch.object(gitnative.runtime, "invoke_verified") as invoke, \
                 contextlib.redirect_stderr(err):
                result = gitnative.ensure_seeded(
                    cfg, "ws", mode, "arn", "sid", "ws", "claude", "s3", 1,
                )
            helper.assert_not_called()
            invoke.assert_not_called()
            self.assertTrue(result.seeded)
            self.assertIn("no re-seed", err.getvalue())

    def test_unseeded_flow_uploads_then_seeds_then_persists(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _config(td)
            repo = make_repo(td)
            head = _git(repo, ["rev-parse", "HEAD"]).stdout.strip()
            workspace.save_workspace_state(cfg, "ws", "sid", "claude", storage="s3", epoch=1)
            mode = gitnative.GitNativeMode("feat/x", seeded=False)
            calls = []
            seed_result = runtime.InvocationResult(
                True, json.dumps({"status": "ok", "baseSha": head})
            )
            err = io.StringIO()
            with patch.object(gitnative, "local_repo_root", return_value=repo), \
                 patch.object(gitnative, "bundle_helper_argv", return_value=["helper"]), \
                 patch.object(gitnative, "run_bundle_helper",
                              side_effect=lambda *a, **k: calls.append("upload") or {}), \
                 patch.object(
                     gitnative.runtime, "invoke_verified",
                     side_effect=lambda *a, **k: calls.append("git-seed") or seed_result,
                 ), contextlib.redirect_stderr(err):
                gitnative.ensure_seeded(cfg, "ws", mode, "arn", "sid", "ws", "claude", "s3", 1)
            self.assertEqual(calls, ["upload", "git-seed"])
            self.assertTrue(mode.seeded)
            self.assertEqual(mode.base_sha, head)
            state = workspace.read_workspace_state(cfg, "ws")
            self.assertEqual(state.git_native["branch"], "feat/x")
            self.assertEqual(state.git_native["baseSha"], head)
            self.assertEqual(state.git_native["localRepo"], str(repo))

    def test_dirty_local_worktree_warns_and_proceeds(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _config(td)
            repo = make_repo(td)
            (repo / "dirty.txt").write_text("uncommitted\n")
            head = _git(repo, ["rev-parse", "HEAD"]).stdout.strip()
            workspace.save_workspace_state(cfg, "ws", "sid", "claude", storage="s3", epoch=1)
            mode = gitnative.GitNativeMode("feat/x", seeded=False)
            err = io.StringIO()
            seed_result = runtime.InvocationResult(
                True, json.dumps({"status": "ok", "baseSha": head})
            )
            with patch.object(gitnative, "local_repo_root", return_value=repo), \
                 patch.object(gitnative, "bundle_helper_argv", return_value=["helper"]), \
                 patch.object(gitnative, "run_bundle_helper", return_value={}), \
                 patch.object(gitnative.runtime, "invoke_verified", return_value=seed_result), \
                 contextlib.redirect_stderr(err):
                gitnative.ensure_seeded(cfg, "ws", mode, "arn", "sid", "ws", "claude", "s3", 1)
            self.assertIn("uncommitted changes", err.getvalue())
            self.assertTrue(mode.seeded)

    def test_unsupported_image_fails_before_harness(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _config(td)
            repo = make_repo(td)
            workspace.save_workspace_state(cfg, "ws", "sid", "claude", storage="s3", epoch=1)
            mode = gitnative.GitNativeMode("feat/x", seeded=False)
            unsupported = runtime.InvocationResult(True, json.dumps({
                "status": "error",
                "message": "unknown action 'git-seed' (supported: noop, info)",
            }))
            err = io.StringIO()
            with patch.object(gitnative, "local_repo_root", return_value=repo), \
                 patch.object(gitnative, "bundle_helper_argv", return_value=["helper"]), \
                 patch.object(gitnative, "run_bundle_helper", return_value={}), \
                 patch.object(gitnative.runtime, "invoke_verified", return_value=unsupported), \
                 contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit):
                    gitnative.ensure_seeded(cfg, "ws", mode, "arn", "sid", "ws", "claude", "s3", 1)
            self.assertIn("does not support git-native mode", err.getvalue())
            # nothing persisted on failure
            self.assertIsNone(workspace.read_workspace_state(cfg, "ws").git_native)


class TaskGitNativeFlowTests(unittest.TestCase):
    """cmd_task with --branch: seed before submit; --continue skips re-seed."""

    def _resolved(self):
        return type("Resolved", (), {
            "sid": "sid", "harness": "claude", "identity": "ws",
            "storage": "s3", "epoch": 1, "was_created": False,
        })()

    def test_task_seeds_before_submit(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _config(td)
            calls = []
            ok_warm = runtime.InvocationResult(True, json.dumps({"status": "ok", "storage": "s3"}))
            accepted = runtime.InvocationResult(True, json.dumps({"status": "accepted", "task_id": "t1"}))
            with patch.object(task_cmd.harness_mod, "resolve_harness", return_value=self._resolved()), \
                 patch.object(task_cmd, "runtime_arn", return_value="arn"), \
                 patch.object(
                     task_cmd.gitnative, "ensure_seeded",
                     side_effect=lambda *a, **k: calls.append("seed"),
                 ), \
                 patch.object(task_cmd.runtime, "invoke_verified", side_effect=[
                     ok_warm,
                     accepted,
                 ]) as invoke, \
                 patch.object(task_cmd.workspace, "mark_status"):
                out = io.StringIO()
                err = io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    rc = task_cmd.cmd_task(cfg, ["ws", "--branch", "feat/x", "build"])
            self.assertEqual(rc, 0)
            self.assertEqual(calls, ["seed"])
            # the second invocation is the task submit, after the seed
            self.assertEqual(invoke.call_count, 2)
            self.assertEqual(out.getvalue().strip(), "t1")

    def test_continue_on_seeded_workspace_skips_re_seed(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _config(td)
            workspace.save_workspace_state(cfg, "ws", "sid", "claude", storage="s3", epoch=1)
            workspace.save_git_native_state(cfg, "ws", "feat/x", "abc", "/r")
            ok_warm = runtime.InvocationResult(True, json.dumps({"status": "ok", "storage": "s3"}))
            accepted = runtime.InvocationResult(True, json.dumps({"status": "accepted", "task_id": "t2"}))
            with patch.object(task_cmd.harness_mod, "resolve_harness", return_value=self._resolved()), \
                 patch.object(task_cmd, "runtime_arn", return_value="arn"), \
                 patch.object(task_cmd.gitnative, "run_bundle_helper") as helper, \
                 patch.object(task_cmd.runtime, "invoke_verified", side_effect=[ok_warm, accepted]), \
                 patch.object(task_cmd.workspace, "mark_status"):
                out = io.StringIO()
                err = io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    rc = task_cmd.cmd_task(cfg, ["ws", "--continue", "fix tests"])
            self.assertEqual(rc, 0)
            helper.assert_not_called()  # no bundle transfer on --continue
            self.assertIn("no re-seed", err.getvalue())

    def test_sync_on_git_native_workspace_dies(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _config(td)
            workspace.save_workspace_state(cfg, "ws", "sid", "claude", storage="s3", epoch=1)
            workspace.save_git_native_state(cfg, "ws", "feat/x", "abc", "/r")
            err = io.StringIO()
            with patch.object(task_cmd.harness_mod, "resolve_harness", return_value=self._resolved()), \
                 patch.object(task_cmd.runtime, "invoke_verified") as invoke, \
                 contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit):
                    task_cmd.cmd_task(cfg, ["ws", "--sync", "/p", "build"])
            invoke.assert_not_called()  # dies before any remote mutation
            self.assertIn("git-native mode", err.getvalue())


class FetchCommandTests(unittest.TestCase):
    def _seeded_cfg(self, td, repo, branch="feat/x", base=None):
        cfg = _config(td)
        base = base or _git(repo, ["rev-parse", "HEAD"]).stdout.strip()
        workspace.save_workspace_state(cfg, "ws", "sid", "claude", storage="s3", epoch=1)
        workspace.save_git_native_state(cfg, "ws", branch, base, str(repo))
        return cfg, base

    def _resolved(self):
        return type("Resolved", (), {
            "sid": "sid", "harness": "claude", "identity": "ws",
            "storage": "s3", "epoch": 1, "was_created": False,
        })()

    def _remote_clone_with_commit(self, td, src_repo, branch="feat/x"):
        """Simulate the remote workspace: clone + one commit on the branch,
        producing a delivery bundle base..branch."""
        remote = Path(td) / "remote"
        subprocess.run(
            ["git", "clone", "-q", str(src_repo), str(remote)],
            capture_output=True, env={"PATH": "/usr/bin:/bin", "HOME": td},
        )
        _git(remote, ["checkout", "-qb", branch])
        (remote / "work.txt").write_text("session work\n")
        _git(remote, ["add", "-A"])
        _git(remote, ["commit", "-qm", "agent work"])
        base = _git(src_repo, ["rev-parse", "HEAD"]).stdout.strip()
        bundle = Path(td) / "delivery.bundle"
        _git(remote, ["bundle", "create", str(bundle), "{}..{}".format(base, branch)])
        head = _git(remote, ["rev-parse", branch]).stdout.strip()
        return bundle, head

    def test_unknown_workspace_dies(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _config(td)
            err = io.StringIO()
            with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
                fetch_cmd.cmd_fetch(cfg, ["nope"])
            self.assertIn("unknown workspace", err.getvalue())

    def test_non_git_native_workspace_dies(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _config(td)
            workspace.save_workspace_state(cfg, "ws", "sid", "claude", storage="s3", epoch=1)
            err = io.StringIO()
            with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
                fetch_cmd.cmd_fetch(cfg, ["ws"])
            self.assertIn("not in git-native mode", err.getvalue())

    def test_no_work_exits_cleanly(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(td)
            cfg, base = self._seeded_cfg(td, repo)
            ok_warm = runtime.InvocationResult(True, json.dumps({"status": "ok", "storage": "s3"}))
            no_work = runtime.InvocationResult(True, json.dumps({
                "status": "no-work", "branch": "feat/x", "headSha": base,
                "snapshotCommitted": False,
            }))
            err = io.StringIO()
            with patch.object(fetch_cmd.harness_mod, "resolve_harness", return_value=self._resolved()), \
                 patch.object(fetch_cmd, "runtime_arn", return_value="arn"), \
                 patch.object(fetch_cmd.runtime, "invoke_verified", side_effect=[ok_warm, no_work]), \
                 patch.object(fetch_cmd.gitnative, "run_bundle_helper") as helper, \
                 contextlib.redirect_stderr(err):
                rc = fetch_cmd.cmd_fetch(cfg, ["ws"])
            self.assertEqual(rc, 0)
            helper.assert_not_called()
            self.assertIn("no work to fetch", err.getvalue())

    def _run_fetch(self, cfg, repo, bundle, head, args, base):
        ok_warm = runtime.InvocationResult(True, json.dumps({"status": "ok", "storage": "s3"}))
        snapshot = runtime.InvocationResult(True, json.dumps({
            "status": "ok", "branch": "feat/x", "headSha": head,
            "baseSha": base, "snapshotCommitted": False,
            "bundleRef": "delivery.bundle",
        }))

        def fake_download(argv, doing):
            # copy the prepared bundle into the CLI's temp download path
            target = argv[argv.index("--download") + 1]
            Path(target).write_bytes(bundle.read_bytes())
            return {"type": "done"}

        err = io.StringIO()
        with patch.object(fetch_cmd.harness_mod, "resolve_harness", return_value=self._resolved()), \
             patch.object(fetch_cmd, "runtime_arn", return_value="arn"), \
             patch.object(fetch_cmd.runtime, "invoke_verified", side_effect=[ok_warm, snapshot]), \
             patch.object(fetch_cmd.gitnative, "bundle_helper_argv", return_value=["helper", "--download", "placeholder"]) as argv_builder, \
             patch.object(fetch_cmd.gitnative, "run_bundle_helper", side_effect=fake_download), \
             contextlib.redirect_stderr(err):
            def build_argv(*_args, **kwargs):
                return ["helper", "--download", str(kwargs["download"])]
            argv_builder.side_effect = build_argv
            rc = fetch_cmd.cmd_fetch(cfg, args)
        return rc, err.getvalue()

    def test_fetch_imports_branch_without_touching_checkout(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(td)
            cfg, base = self._seeded_cfg(td, repo)
            bundle, head = self._remote_clone_with_commit(td, repo)
            checked_out = _git(repo, ["rev-parse", "HEAD"]).stdout.strip()
            rc, err = self._run_fetch(cfg, repo, bundle, head, ["ws"], base)
            self.assertEqual(rc, 0)
            self.assertEqual(_git(repo, ["rev-parse", "feat/x"]).stdout.strip(), head)
            # current checkout untouched
            self.assertEqual(_git(repo, ["rev-parse", "HEAD"]).stdout.strip(), checked_out)
            self.assertIn("updated to", err)

    def test_fetch_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(td)
            cfg, base = self._seeded_cfg(td, repo)
            bundle, head = self._remote_clone_with_commit(td, repo)
            rc1, _ = self._run_fetch(cfg, repo, bundle, head, ["ws"], base)
            rc2, err2 = self._run_fetch(cfg, repo, bundle, head, ["ws"], base)
            self.assertEqual((rc1, rc2), (0, 0))
            self.assertIn("already up to date", err2)
            self.assertEqual(_git(repo, ["rev-parse", "feat/x"]).stdout.strip(), head)

    def test_divergent_local_branch_fails_and_suggests_force(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(td)
            cfg, base = self._seeded_cfg(td, repo)
            bundle, head = self._remote_clone_with_commit(td, repo)
            # diverge locally: another commit on feat/x from the same base
            _git(repo, ["branch", "feat/x", base])
            _git(repo, ["checkout", "-q", "feat/x"])
            (repo / "local.txt").write_text("local divergence\n")
            _git(repo, ["add", "-A"])
            _git(repo, ["commit", "-qm", "local commit"])
            _git(repo, ["checkout", "-q", "-"])
            local_sha = _git(repo, ["rev-parse", "feat/x"]).stdout.strip()

            with self.assertRaises(SystemExit):
                self._run_fetch(cfg, repo, bundle, head, ["ws"], base)
            # ref unchanged after the failed fetch
            self.assertEqual(_git(repo, ["rev-parse", "feat/x"]).stdout.strip(), local_sha)

    def test_force_overwrites_divergent_ref(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(td)
            cfg, base = self._seeded_cfg(td, repo)
            bundle, head = self._remote_clone_with_commit(td, repo)
            _git(repo, ["branch", "feat/x", base])
            _git(repo, ["checkout", "-q", "feat/x"])
            (repo / "local.txt").write_text("local divergence\n")
            _git(repo, ["add", "-A"])
            _git(repo, ["commit", "-qm", "local commit"])
            _git(repo, ["checkout", "-q", "-"])

            rc, _ = self._run_fetch(cfg, repo, bundle, head, ["ws", "--force"], base)
            self.assertEqual(rc, 0)
            self.assertEqual(_git(repo, ["rev-parse", "feat/x"]).stdout.strip(), head)

    def test_push_runs_only_after_successful_import(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(td)
            # bare origin for the push
            origin = Path(td) / "origin.git"
            subprocess.run(
                ["git", "init", "-q", "--bare", str(origin)],
                capture_output=True, env={"PATH": "/usr/bin:/bin", "HOME": td},
            )
            _git(repo, ["remote", "add", "origin", str(origin)])
            cfg, base = self._seeded_cfg(td, repo)
            bundle, head = self._remote_clone_with_commit(td, repo)
            rc, err = self._run_fetch(cfg, repo, bundle, head, ["ws", "--push"], base)
            self.assertEqual(rc, 0)
            self.assertIn("pushed 'feat/x' to origin", err)
            pushed = subprocess.run(
                ["git", "-C", str(origin), "rev-parse", "feat/x"],
                capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", "HOME": td},
            )
            self.assertEqual(pushed.stdout.strip(), head)

    def test_unsupported_image_reports_actionable_error(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(td)
            cfg, _ = self._seeded_cfg(td, repo)
            ok_warm = runtime.InvocationResult(True, json.dumps({"status": "ok", "storage": "s3"}))
            unsupported = runtime.InvocationResult(True, json.dumps({
                "status": "error",
                "message": "unknown action 'git-snapshot' (supported: noop)",
            }))
            err = io.StringIO()
            with patch.object(fetch_cmd.harness_mod, "resolve_harness", return_value=self._resolved()), \
                 patch.object(fetch_cmd, "runtime_arn", return_value="arn"), \
                 patch.object(fetch_cmd.runtime, "invoke_verified", side_effect=[ok_warm, unsupported]), \
                 contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit):
                    fetch_cmd.cmd_fetch(cfg, ["ws"])
            self.assertIn("does not support git-native mode", err.getvalue())

    def test_unknown_option_dies(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _config(td)
            with self.assertRaises(SystemExit):
                fetch_cmd.cmd_fetch(cfg, ["ws", "--evil"])


class PayloadBuilderTests(unittest.TestCase):
    def test_git_seed_payload(self):
        data = json.loads(runtime.payload_git_seed("ws", "claude", "feat/x", "s3", 2))
        self.assertEqual(data["action"], "git-seed")
        self.assertEqual(data["branch"], "feat/x")
        self.assertEqual(data["workspace"], "ws")
        self.assertEqual(data["storage_backend"], "s3")
        self.assertEqual(data["session_epoch"], 2)
        # No usable local origin: the field is ABSENT, not empty, so the
        # no-origin payload stays byte-for-byte what it was before TASK-26.
        self.assertNotIn("originUrl", data)

    def test_git_seed_payload_carries_origin_url(self):
        data = json.loads(runtime.payload_git_seed(
            "ws", "claude", "feat/x", "s3", 2,
            origin_url="https://github.com/o/r.git",
        ))
        self.assertEqual(data["originUrl"], "https://github.com/o/r.git")

    def test_git_snapshot_payload(self):
        data = json.loads(runtime.payload_git_snapshot("ws", "opencode", "session", 1))
        self.assertEqual(data["action"], "git-snapshot")
        self.assertNotIn("branch", data)

    def test_hostile_branch_name_stays_in_json_context(self):
        hostile = 'x";$(rm-rf)/y'
        data = json.loads(runtime.payload_git_seed("ws", "claude", hostile))
        self.assertEqual(data["branch"], hostile)


class StagingRootTests(unittest.TestCase):
    def test_backend_staging_roots(self):
        self.assertEqual(gitnative.staging_root("s3"), "/home/sch/workspace/state/bundles")
        self.assertEqual(gitnative.staging_root("session"), "/mnt/workspace/state/bundles")
        self.assertEqual(gitnative.staging_root(""), "/mnt/workspace/state/bundles")


class SanitizeOriginUrlTests(unittest.TestCase):
    # TASK-26: only credential-free github.com https URLs survive, so the
    # seed can never ship a token (or an SSH key reference) to the remote.
    def test_https_survives(self):
        self.assertEqual(
            gitnative.sanitize_origin_url("https://github.com/o/r.git"),
            "https://github.com/o/r.git",
        )

    def test_https_userinfo_is_stripped(self):
        self.assertEqual(
            gitnative.sanitize_origin_url("https://x-access-token:secret@github.com/o/r.git"),
            "https://github.com/o/r.git",
        )
        self.assertEqual(
            gitnative.sanitize_origin_url("https://user@github.com/o/r"),
            "https://github.com/o/r",
        )

    def test_ssh_forms_are_rewritten(self):
        self.assertEqual(
            gitnative.sanitize_origin_url("git@github.com:o/r.git"),
            "https://github.com/o/r.git",
        )
        self.assertEqual(
            gitnative.sanitize_origin_url("ssh://git@github.com/o/r"),
            "https://github.com/o/r",
        )

    def test_non_github_and_garbage_yield_empty(self):
        for raw in (
            "", "   ", None, 42,
            "https://gitlab.com/o/r.git",
            "http://github.com/o/r.git",
            "https://github.com/o",
            "https://github.com/../evil",
            "https://evilgithub.com/o/r.git",
            "/local/path",
            "https://github.com/o/r with space.git",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(gitnative.sanitize_origin_url(raw), "")


class LocalOriginUrlTests(unittest.TestCase):
    def test_repo_without_origin_yields_empty(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(td)
            self.assertEqual(gitnative.local_origin_url(repo), "")

    def test_github_origin_is_returned_sanitized(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(td)
            _git(repo, ["remote", "add", "origin", "git@github.com:o/r.git"])
            self.assertEqual(
                gitnative.local_origin_url(repo), "https://github.com/o/r.git"
            )

    def test_non_github_origin_yields_empty(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(td)
            _git(repo, ["remote", "add", "origin", "https://gitlab.com/o/r.git"])
            self.assertEqual(gitnative.local_origin_url(repo), "")

    def test_missing_git_is_not_fatal(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(td)
            with patch.dict(os.environ, {"PATH": "/nonexistent"}):
                self.assertEqual(gitnative.local_origin_url(repo), "")


if __name__ == "__main__":
    unittest.main()
