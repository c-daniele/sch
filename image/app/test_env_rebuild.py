"""Offline tests for the post-restore project-env rebuild (TASK-1.3, TASK-21).

Covers: the lockfile-only plan for npm, uv and requirements.txt projects with
and without lockfiles, the scrubbed child env (nothing targets the shim
interpreter), the worktree guard (git status before == after, tracked files
never rewritten), the structured outcome surfaced by the info action, the
SCH_REBUILD_ENV_ON_RESTORE=0 escape hatch and the never-fail-closed contract.

No network: fake `uv`/`npm` executables on PATH simulate misbehaving tools,
and the real-tool probes use dependency-free projects in offline mode.
"""

import importlib.util
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

app_dir = Path(__file__).parent
sys.path.insert(0, str(app_dir))
bedrock = types.ModuleType("bedrock_agentcore")


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


bedrock.BedrockAgentCoreApp = FakeApp
sys.modules["bedrock_agentcore"] = bedrock
boto3 = types.ModuleType("boto3")
boto3.client = lambda *_args, **_kwargs: None
sys.modules["boto3"] = boto3

spec = importlib.util.spec_from_file_location("sch_env_rebuild_main", app_dir / "main.py")
main = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = main
with patch("threading.Thread"):
    spec.loader.exec_module(main)

HAVE_GIT = shutil.which("git") is not None
HAVE_UV = shutil.which("uv") is not None
HAVE_NPM = shutil.which("npm") is not None

PYPROJECT = (
    '[project]\nname = "probe"\nversion = "0.1.0"\n'
    'requires-python = ">=3.11"\ndependencies = []\n\n'
    "[tool.uv]\npackage = false\n"
)


def clean_env(**overrides):
    """A hermetic env: minimal base + explicit overrides only."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "UV_OFFLINE": "1",
        "npm_config_offline": "true",
        "npm_config_cache": str(Path(tempfile.gettempdir()) / "sch-test-npm-cache"),
    }
    if os.environ.get("UV_PYTHON"):
        env["UV_PYTHON"] = os.environ["UV_PYTHON"]
    env.update(overrides)
    return env


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"},
    ).stdout


def init_repo(repo: Path, files: dict) -> None:
    """A committed git repo holding `files`, ignoring the env dirs the way a
    real project does, plus one pre-existing dirty and untracked file so the
    guard is tested against a non-clean checkpoint state."""
    repo.mkdir(parents=True, exist_ok=True)
    files = {".gitignore": ".venv/\nnode_modules/\n", "notes.txt": "v1\n", **files}
    for rel, content in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(content)
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    (repo / "notes.txt").write_text("v2 uncommitted\n")
    (repo / "scratch.txt").write_text("untracked work\n")


def porcelain(repo: Path) -> str:
    return git(repo, "status", "--porcelain", "--untracked-files=all")


def tracked_bytes(repo: Path) -> dict:
    return {
        rel: (repo / rel).read_bytes()
        for rel in git(repo, "ls-files").split()
        if (repo / rel).is_file()
    }


def write_fake_tool(bin_dir: Path, name: str, body: str) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / name
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def steps_by_env(plan: list) -> dict:
    return {step["env"]: step for step in plan}


class PlanTests(unittest.TestCase):
    def plan(self, files: dict, dirs=()) -> dict:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            for rel, content in files.items():
                (repo / rel).write_text(content)
            for rel in dirs:
                (repo / rel).mkdir()
            return steps_by_env(main._project_env_plan(repo))

    def test_npm_with_lockfile_uses_npm_ci(self):
        step = self.plan({"package.json": "{}", "package-lock.json": "{}"})["node"]
        self.assertIsNone(step["skip"])
        self.assertEqual(step["commands"], [["npm", "ci", "--no-audit", "--no-fund"]])

    def test_npm_shrinkwrap_counts_as_lockfile(self):
        step = self.plan({"package.json": "{}", "npm-shrinkwrap.json": "{}"})["node"]
        self.assertEqual(step["commands"][0][:2], ["npm", "ci"])

    def test_npm_without_lockfile_is_skipped_never_npm_install(self):
        step = self.plan({"package.json": "{}"})["node"]
        self.assertEqual(step["commands"], [])
        self.assertTrue(step["skip"].startswith("no-lockfile"))

    def test_npm_skipped_when_env_present(self):
        self.assertEqual(self.plan({"package.json": "{}"}, dirs=["node_modules"]), {})

    def test_uv_with_lockfile_is_frozen(self):
        step = self.plan({"pyproject.toml": PYPROJECT, "uv.lock": "x"})["python"]
        self.assertEqual(step["commands"], [["uv", "sync", "--frozen"]])

    def test_pyproject_without_lockfile_is_skipped_never_uv_sync(self):
        step = self.plan({"pyproject.toml": PYPROJECT})["python"]
        self.assertEqual(step["commands"], [])
        self.assertTrue(step["skip"].startswith("no-lockfile"))

    def test_requirements_go_into_project_local_venv(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / "requirements.txt").write_text("requests\n")
            step = steps_by_env(main._project_env_plan(repo))["python"]
            flat = [arg for argv in step["commands"] for arg in argv]
            self.assertNotIn(sys.executable, flat)
            self.assertNotIn("pip3", flat)
            self.assertEqual(step["commands"][0], ["uv", "venv", "--quiet", ".venv"])
            install = step["commands"][1]
            self.assertEqual(install[:3], ["uv", "pip", "install"])
            self.assertIn(str(repo / ".venv" / "bin" / "python"), install)
            self.assertNotIn("--system", install)
            self.assertNotIn("--user", install)

    def test_requirements_win_over_unlocked_pyproject(self):
        step = self.plan({"pyproject.toml": PYPROJECT, "requirements.txt": "x\n"})["python"]
        self.assertEqual(step["label"], "uv-pip-requirements")

    def test_python_skipped_when_venv_present(self):
        self.assertEqual(
            self.plan({"pyproject.toml": PYPROJECT, "uv.lock": "x"}, dirs=[".venv"]), {},
        )

    def test_no_manifest_no_plan(self):
        self.assertEqual(self.plan({"README.md": "x"}), {})


class ChildEnvTests(unittest.TestCase):
    def test_interpreter_redirecting_vars_are_scrubbed(self):
        hostile = {name: "/somewhere" for name in main.ENV_REBUILD_SCRUBBED_VARS}
        with patch.dict(os.environ, clean_env(**hostile), clear=True):
            env = main._env_rebuild_child_env()
        for name in main.ENV_REBUILD_SCRUBBED_VARS:
            self.assertNotIn(name, env)
        self.assertIn("NODE_OPTIONS", env)  # memory caps still applied


class OutcomeTests(unittest.TestCase):
    def run_rebuild(self, repo: Path, env: dict) -> str:
        with (
            patch.object(main, "REPO_DIR", repo),
            patch.dict(os.environ, env, clear=True),
        ):
            return main._maybe_rebuild_project_env()

    def test_disabled_short_circuits_and_runs_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / "package.json").write_text("{}")
            (repo / "package-lock.json").write_text("{}")
            with patch.object(main.subprocess, "run") as run:
                status = self.run_rebuild(repo, clean_env(SCH_REBUILD_ENV_ON_RESTORE="0"))
            self.assertEqual(status, "skipped-disabled")
            run.assert_not_called()
            self.assertEqual(main.ENV_REBUILD_STATE["status"], "skipped-disabled")

    def test_noop_when_nothing_missing(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(self.run_rebuild(Path(td), clean_env()), "skipped-noop")

    def test_no_lockfiles_reports_reasons(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / "package.json").write_text("{}")
            (repo / "pyproject.toml").write_text(PYPROJECT)
            with patch.object(main.subprocess, "run") as run:
                status = self.run_rebuild(repo, clean_env())
            run.assert_not_called()
            self.assertEqual(status, "skipped-no-lockfile")
            state = main.ENV_REBUILD_STATE
            self.assertEqual({s["result"] for s in state["steps"]}, {"skipped"})
            self.assertIn("package.json without package-lock.json", state["summary"])
            self.assertIn("pyproject.toml without uv.lock", state["summary"])
            self.assertFalse((repo / "package-lock.json").exists())
            self.assertFalse((repo / "uv.lock").exists())

    def test_missing_tool_is_unavailable_not_failure(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / "package.json").write_text("{}")
            (repo / "package-lock.json").write_text("{}")
            status = self.run_rebuild(repo, clean_env(PATH=str(repo / "empty-bin")))
            self.assertEqual(status, "skipped-unavailable")
            self.assertIn("npm not on PATH", main.ENV_REBUILD_STATE["summary"])

    def test_partial_and_failed_statuses(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            bin_dir = repo.parent / (repo.name + "-bin")
            write_fake_tool(bin_dir, "npm", "mkdir -p node_modules\n")
            write_fake_tool(bin_dir, "uv", "echo boom >&2; exit 3\n")
            (repo / "package.json").write_text("{}")
            (repo / "package-lock.json").write_text("{}")
            (repo / "pyproject.toml").write_text(PYPROJECT)
            (repo / "uv.lock").write_text("x")
            try:
                status = self.run_rebuild(repo, clean_env(PATH=f"{bin_dir}:/usr/bin:/bin"))
                self.assertEqual(status, "rebuilt-partial")
                summary = main.ENV_REBUILD_STATE["summary"]
                self.assertIn("node npm-ci ok", summary)
                self.assertIn("python uv-sync-frozen failed (uv exit 3)", summary)
                shutil.rmtree(repo / "node_modules")
                write_fake_tool(bin_dir, "npm", "exit 1\n")
                self.assertEqual(
                    self.run_rebuild(repo, clean_env(PATH=f"{bin_dir}:/usr/bin:/bin")),
                    "rebuild-failed",
                )
            finally:
                shutil.rmtree(bin_dir, ignore_errors=True)

    def test_unexpected_error_never_raises(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / "package.json").write_text("{}")
            (repo / "package-lock.json").write_text("{}")
            with patch.object(main, "_snapshot_protected_files", side_effect=OSError("x")):
                self.assertEqual(self.run_rebuild(repo, clean_env()), "rebuild-failed")

    def test_info_exposes_the_outcome(self):
        main._record_env_rebuild(
            "skipped-no-lockfile",
            [{"env": "node", "label": "npm-ci", "result": "skipped",
              "reason": "no-lockfile: x"}],
            "unchanged",
        )
        with tempfile.TemporaryDirectory() as td:
            saved = {
                name: getattr(main, name) for name in (
                    "PROVIDER_KEYS_FILE", "CHECKPOINT_BUCKET",
                )
            }
            spool = main.telegram_notifier.SPOOL_DIR
            main.PROVIDER_KEYS_FILE = Path(td) / "provider-keys.env"
            main.CHECKPOINT_BUCKET = ""
            main.telegram_notifier.SPOOL_DIR = Path(td) / "spool"
            main._HARNESS["value"] = "opencode"
            main._WORKSPACE_NAME["value"] = "workspace"
            main._SESSION_EPOCH["value"] = 0
            main._STORAGE_BACKEND["value"] = "session"
            try:
                response = main.invoke({
                    "action": "info", "workspace": "workspace", "harness": "opencode",
                })
            finally:
                for name, value in saved.items():
                    setattr(main, name, value)
                main.telegram_notifier.SPOOL_DIR = spool
        block = response["checkpoint"]["env_rebuild"]
        self.assertEqual(block["status"], "skipped-no-lockfile")
        self.assertEqual(block["worktree"], "unchanged")
        self.assertEqual(block["steps"][0]["reason"], "no-lockfile: x")


@unittest.skipUnless(HAVE_GIT, "git not available")
class WorktreeGuardTests(unittest.TestCase):
    """The git status of the repo after the rebuild equals the status at
    checkpoint time, even when a tool misbehaves."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.repo = self.root / "repo"
        self.bin = self.root / "bin"

    def tearDown(self):
        self.td.cleanup()

    def rebuild(self) -> str:
        with (
            patch.object(main, "REPO_DIR", self.repo),
            patch.dict(os.environ, clean_env(PATH=f"{self.bin}:/usr/bin:/bin"), clear=True),
        ):
            return main._maybe_rebuild_project_env()

    def test_stray_and_rewritten_files_are_reverted(self):
        init_repo(self.repo, {
            "package.json": "{}\n", "package-lock.json": '{"pinned": 1}\n',
            "pyproject.toml": PYPROJECT, "uv.lock": "pinned\n",
            "src/app.py": "print(1)\n",
        })
        # A tool that ignores --frozen/ci semantics: rewrites both lockfiles,
        # edits a tracked source file, drops egg-info and a stray lockfile.
        write_fake_tool(self.bin, "npm", (
            "mkdir -p node_modules\n"
            "echo '{\"pinned\": 2}' > package-lock.json\n"
            "echo junk > yarn.lock\n"
        ))
        write_fake_tool(self.bin, "uv", (
            "mkdir -p .venv\n"
            "echo repinned > uv.lock\n"
            "echo 'print(2)' > src/app.py\n"
            "mkdir -p probe.egg-info && echo x > probe.egg-info/PKG-INFO\n"
        ))
        before_status, before_bytes = porcelain(self.repo), tracked_bytes(self.repo)
        status = self.rebuild()
        self.assertEqual(status, "rebuilt")
        self.assertEqual(porcelain(self.repo), before_status)
        self.assertEqual(tracked_bytes(self.repo), before_bytes)
        self.assertEqual((self.repo / "notes.txt").read_text(), "v2 uncommitted\n")
        self.assertTrue((self.repo / "scratch.txt").is_file())
        self.assertFalse((self.repo / "probe.egg-info").exists())
        self.assertEqual(main.ENV_REBUILD_STATE["worktree"], "restored")

    def test_well_behaved_rebuild_reports_unchanged(self):
        init_repo(self.repo, {"package.json": "{}\n", "package-lock.json": "{}\n"})
        write_fake_tool(self.bin, "npm", "mkdir -p node_modules\n")
        before = porcelain(self.repo)
        self.assertEqual(self.rebuild(), "rebuilt")
        self.assertEqual(porcelain(self.repo), before)
        self.assertEqual(main.ENV_REBUILD_STATE["worktree"], "unchanged")

    def test_env_dir_not_gitignored_is_kept(self):
        init_repo(self.repo, {
            ".gitignore": "", "package.json": "{}\n", "package-lock.json": "{}\n",
        })
        write_fake_tool(self.bin, "npm", "mkdir -p node_modules/dep && echo x > node_modules/dep/i.js\n")
        before = porcelain(self.repo)
        self.assertEqual(self.rebuild(), "rebuilt")
        self.assertTrue((self.repo / "node_modules" / "dep" / "i.js").is_file())
        self.assertEqual(main.ENV_REBUILD_STATE["worktree"], "unchanged")
        self.assertEqual(
            [line for line in porcelain(self.repo).splitlines()
             if "node_modules" not in line],
            before.splitlines(),
        )

    def test_unlocked_projects_leave_no_lockfile(self):
        init_repo(self.repo, {"package.json": "{}\n", "pyproject.toml": PYPROJECT})
        write_fake_tool(self.bin, "npm", "echo '{}' > package-lock.json\n")
        write_fake_tool(self.bin, "uv", "echo lock > uv.lock\n")
        before = porcelain(self.repo)
        self.assertEqual(self.rebuild(), "skipped-no-lockfile")
        self.assertEqual(porcelain(self.repo), before)
        self.assertFalse((self.repo / "package-lock.json").exists())
        self.assertFalse((self.repo / "uv.lock").exists())


@unittest.skipUnless(HAVE_GIT, "git not available")
class RealToolProbeTests(unittest.TestCase):
    """Real uv/npm against dependency-free probe projects, offline."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.repo = Path(self.td.name) / "repo"

    def tearDown(self):
        self.td.cleanup()

    def rebuild(self) -> str:
        with (
            patch.object(main, "REPO_DIR", self.repo),
            patch.dict(os.environ, clean_env(), clear=True),
        ):
            return main._maybe_rebuild_project_env()

    def assert_rebuilt_without_repo_changes(self, env_dir: str) -> None:
        before_status, before_bytes = porcelain(self.repo), tracked_bytes(self.repo)
        status = self.rebuild()
        self.assertEqual(status, "rebuilt", main.ENV_REBUILD_STATE)
        self.assertTrue((self.repo / env_dir).is_dir())
        self.assertEqual(porcelain(self.repo), before_status)
        self.assertEqual(tracked_bytes(self.repo), before_bytes)
        self.assertEqual(main.ENV_REBUILD_STATE["worktree"], "unchanged")

    @unittest.skipUnless(HAVE_UV, "uv not available")
    def test_uv_lockfile_project_with_stale_lock_is_not_rewritten(self):
        init_repo(self.repo, {"pyproject.toml": PYPROJECT})
        subprocess.run(["uv", "lock", "--offline", "--quiet"], cwd=self.repo,
                       check=True, env=clean_env())
        # Make the committed lock stale: plain `uv sync` would rewrite it.
        (self.repo / "pyproject.toml").write_text(
            PYPROJECT.replace('version = "0.1.0"', 'version = "0.2.0"'))
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "stale lock")
        self.assert_rebuilt_without_repo_changes(".venv")

    @unittest.skipUnless(HAVE_UV, "uv not available")
    def test_requirements_project_builds_a_local_venv(self):
        init_repo(self.repo, {"requirements.txt": "# no dependencies\n"})
        self.assert_rebuilt_without_repo_changes(".venv")
        venv_python = self.repo / ".venv" / "bin" / "python"
        self.assertTrue(venv_python.exists())
        prefix = subprocess.run(
            [str(venv_python), "-c", "import sys; print(sys.prefix)"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        self.assertEqual(Path(prefix).resolve(), (self.repo / ".venv").resolve())

    @unittest.skipUnless(HAVE_NPM, "npm not available")
    def test_npm_lockfile_project_is_installed_from_the_lock(self):
        init_repo(self.repo, {
            "package.json": '{"name": "probe", "version": "1.0.0", '
                            '"dependencies": {"dep": "file:./vendor/dep"}}\n',
            "vendor/dep/package.json": '{"name": "dep", "version": "1.0.0"}\n',
        })
        subprocess.run(
            ["npm", "install", "--no-audit", "--no-fund", "--silent"],
            cwd=self.repo, check=True, env=clean_env(),
        )
        shutil.rmtree(self.repo / "node_modules")
        git(self.repo, "add", "package-lock.json")
        git(self.repo, "commit", "-q", "-m", "lock")
        self.assert_rebuilt_without_repo_changes("node_modules")


if __name__ == "__main__":
    unittest.main()
