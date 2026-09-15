"""Offline tests for the TASK-1 memory-cost footprint work.

Covers: regenerable-dir exclusion from the repo fingerprint and the repo
tarball, the Node heap / build-parallelism caps (defaults, precedence,
escape hatches), loud OOM remediation on headless tasks, the post-restore
project-env rebuild plan, and the same caps on the interactive wrapper path.
"""

import importlib.util
import os
import stat
import subprocess
import sys
import tarfile
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

spec = importlib.util.spec_from_file_location("sch_memory_main", app_dir / "main.py")
main = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = main
with patch("threading.Thread"):
    spec.loader.exec_module(main)

WRAPPER = app_dir.parent / "scripts" / "harness-wrapper.sh"

CAP_ENV_KEYS = (
    "SCH_NODE_HEAP_MB",
    "SCH_BUILD_JOBS",
    "SCH_REBUILD_ENV_ON_RESTORE",
    "NODE_OPTIONS",
    "MAKEFLAGS",
    "CMAKE_BUILD_PARALLEL_LEVEL",
    "CARGO_BUILD_JOBS",
)


def clean_env(**overrides):
    """A hermetic child env: minimal base + explicit overrides only."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/tmp/no-home"}
    env.update(overrides)
    return env


def make_repo(root: Path) -> Path:
    repo = root / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.py").write_text("print('src')\n")
    (repo / ".git").mkdir()
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (repo / "node_modules" / "pkg").mkdir(parents=True)
    (repo / "node_modules" / "pkg" / "f.js").write_text("x\n")
    (repo / ".venv" / "lib").mkdir(parents=True)
    (repo / ".venv" / "lib" / "h.py").write_text("z\n")
    (repo / "sub" / "node_modules" / "q").mkdir(parents=True)
    (repo / "sub" / "node_modules" / "q" / "g.js").write_text("y\n")
    return repo


class FingerprintExcludeTests(unittest.TestCase):
    def test_regenerable_changes_do_not_flip_fingerprint(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            before = main._tree_fingerprint(
                repo, exclude_dir_names=main.REPO_CHECKPOINT_EXCLUDE_NAMES
            )
            (repo / "node_modules" / "pkg" / "f.js").write_text("changed\n")
            (repo / ".venv" / "lib" / "new.py").write_text("new\n")
            (repo / "sub" / "node_modules" / "q2").mkdir()
            (repo / "sub" / "node_modules" / "q2" / "h.js").write_text("h\n")
            self.assertEqual(
                before,
                main._tree_fingerprint(
                    repo, exclude_dir_names=main.REPO_CHECKPOINT_EXCLUDE_NAMES
                ),
            )

    def test_source_changes_still_flip_fingerprint(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            before = main._tree_fingerprint(
                repo, exclude_dir_names=main.REPO_CHECKPOINT_EXCLUDE_NAMES
            )
            (repo / "src" / "a.py").write_text("print('edited')\n")
            self.assertNotEqual(
                before,
                main._tree_fingerprint(
                    repo, exclude_dir_names=main.REPO_CHECKPOINT_EXCLUDE_NAMES
                ),
            )

    def test_git_metadata_stays_fingerprinted(self):
        # .git must stay durable (spec checkpointing R1): HEAD changes flip.
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            before = main._tree_fingerprint(
                repo, exclude_dir_names=main.REPO_CHECKPOINT_EXCLUDE_NAMES
            )
            (repo / ".git" / "HEAD").write_text("ref: refs/heads/other\n")
            self.assertNotEqual(
                before,
                main._tree_fingerprint(
                    repo, exclude_dir_names=main.REPO_CHECKPOINT_EXCLUDE_NAMES
                ),
            )

    def test_fingerprint_repo_uses_excludes(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            with patch.object(main, "REPO_DIR", repo):
                before = main._fingerprint_repo()
                (repo / "node_modules" / "pkg" / "f.js").write_text("changed\n")
                self.assertEqual(before, main._fingerprint_repo())


class ArchiveExcludeTests(unittest.TestCase):
    def members(self, tar_path: Path) -> list:
        with tarfile.open(tar_path) as tar:
            return tar.getnames()

    def test_exclude_names_drop_regenerable_dirs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = make_repo(root)
            dest = root / "repo.tar.gz"
            self.assertTrue(
                main._create_archive(
                    repo, dest, exclude_names=main.REPO_CHECKPOINT_EXCLUDE_NAMES
                )
            )
            names = self.members(dest)
            self.assertIn("repo/src/a.py", names)
            self.assertIn("repo/.git/HEAD", names)
            for name in names:
                parts = name.split("/")
                self.assertNotIn("node_modules", parts, name)
                self.assertNotIn(".venv", parts, name)

    def test_default_archive_keeps_everything(self):
        # Backward compatibility: no exclude_names = old behavior.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = make_repo(root)
            dest = root / "repo.tar.gz"
            self.assertTrue(main._create_archive(repo, dest))
            names = self.members(dest)
            self.assertIn("repo/node_modules/pkg/f.js", names)
            self.assertIn("repo/.venv/lib/h.py", names)

    def test_excluded_tarball_is_smaller(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = make_repo(root)
            blob = "0123456789abcdef" * 65536  # 1 MB of junk per regenerable file
            (repo / "node_modules" / "pkg" / "big.js").write_text(blob)
            (repo / ".venv" / "lib" / "big.py").write_text(blob)
            full = root / "full.tar.gz"
            slim = root / "slim.tar.gz"
            self.assertTrue(main._create_archive(repo, full))
            self.assertTrue(
                main._create_archive(
                    repo, slim, exclude_names=main.REPO_CHECKPOINT_EXCLUDE_NAMES
                )
            )
            self.assertLess(slim.stat().st_size, full.stat().st_size // 2)


class MemoryCapsTests(unittest.TestCase):
    def test_defaults(self):
        env = main._apply_memory_caps(clean_env())
        self.assertIn("--max-old-space-size=1792", env["NODE_OPTIONS"])
        self.assertIn("-j2", env["MAKEFLAGS"])
        self.assertEqual(env["SCH_BUILD_JOBS"], "2")
        self.assertEqual(env["CMAKE_BUILD_PARALLEL_LEVEL"], "2")
        self.assertEqual(env["CARGO_BUILD_JOBS"], "2")

    def test_existing_node_options_preserved_and_extended(self):
        env = main._apply_memory_caps(clean_env(NODE_OPTIONS="--trace-warnings"))
        self.assertIn("--trace-warnings", env["NODE_OPTIONS"])
        self.assertIn("--max-old-space-size=1792", env["NODE_OPTIONS"])

    def test_operator_heap_wins(self):
        env = main._apply_memory_caps(
            clean_env(NODE_OPTIONS="--max-old-space-size=8192")
        )
        self.assertIn("--max-old-space-size=8192", env["NODE_OPTIONS"])
        self.assertNotIn("1792", env["NODE_OPTIONS"])

    def test_zero_disables_heap_cap(self):
        env = main._apply_memory_caps(clean_env(SCH_NODE_HEAP_MB="0"))
        self.assertNotIn("NODE_OPTIONS", env)

    def test_invalid_heap_falls_back_to_default(self):
        env = main._apply_memory_caps(clean_env(SCH_NODE_HEAP_MB="lots"))
        self.assertIn("--max-old-space-size=1792", env["NODE_OPTIONS"])

    def test_operator_makeflags_wins(self):
        env = main._apply_memory_caps(clean_env(MAKEFLAGS="-j8"))
        self.assertEqual(env["MAKEFLAGS"], "-j8")

    def test_custom_jobs_fan_out(self):
        env = main._apply_memory_caps(clean_env(SCH_BUILD_JOBS="4"))
        self.assertIn("-j4", env["MAKEFLAGS"])
        self.assertEqual(env["SCH_BUILD_JOBS"], "4")
        self.assertEqual(env["CMAKE_BUILD_PARALLEL_LEVEL"], "4")

    def test_headless_env_carries_caps(self):
        with patch.dict(os.environ, clean_env(), clear=True):
            env = main._headless_harness_env("opencode")
        self.assertIn("--max-old-space-size=1792", env["NODE_OPTIONS"])
        self.assertEqual(env["SCH_EXECUTION_MODE"], "headless")
        self.assertEqual(env["SCH_HARNESS"], "opencode")


class OomRemediationTests(unittest.TestCase):
    def test_v8_heap_message(self):
        hint = main._oom_remediation(
            "FATAL ERROR: Reached heap limit Allocation failed - "
            "JavaScript heap out of memory",
            134,
        )
        self.assertIsNotNone(hint)
        self.assertIn("SCH_NODE_HEAP_MB", hint)
        self.assertIn("SCH_BUILD_JOBS", hint)

    def test_kernel_oom_kill(self):
        hint = main._oom_remediation("Killed", 137)
        self.assertIsNotNone(hint)
        self.assertIn("SCH_NODE_HEAP_MB", hint)

    def test_ordinary_failure_has_no_hint(self):
        self.assertIsNone(main._oom_remediation("boom: something failed", 1))
        self.assertIsNone(main._oom_remediation("", 1))
        self.assertIsNone(main._oom_remediation(None, 0))


class EnvRebuildPlanTests(unittest.TestCase):
    def test_npm_ci_when_lockfile_present(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / "package.json").write_text("{}\n")
            (repo / "package-lock.json").write_text("{}\n")
            self.assertEqual(
                main._project_env_plan(repo),
                [("npm-ci", ["npm", "ci", "--no-audit", "--no-fund"])],
            )

    def test_npm_skipped_when_env_present(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / "package.json").write_text("{}\n")
            (repo / "node_modules").mkdir()
            self.assertEqual(main._project_env_plan(repo), [])

    def test_uv_sync_when_lockfile_present(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / "pyproject.toml").write_text("[project]\n")
            (repo / "uv.lock").write_text("x\n")
            self.assertEqual(main._project_env_plan(repo), [("uv-sync", ["uv", "sync"])])

    def test_pip_when_only_requirements(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / "requirements.txt").write_text("requests\n")
            plan = main._project_env_plan(repo)
            self.assertEqual(len(plan), 1)
            self.assertEqual(plan[0][0], "pip-install")

    def test_disabled_short_circuits(self):
        with tempfile.TemporaryDirectory() as td:
            with (
                patch.object(main, "REPO_DIR", Path(td)),
                patch.dict(os.environ, {"SCH_REBUILD_ENV_ON_RESTORE": "0"}),
            ):
                self.assertEqual(main._maybe_rebuild_project_env(), "skipped-disabled")

    def test_noop_when_nothing_missing(self):
        with tempfile.TemporaryDirectory() as td:
            with (
                patch.object(main, "REPO_DIR", Path(td)),
                patch.dict(os.environ, clean_env(), clear=True),
            ):
                self.assertEqual(main._maybe_rebuild_project_env(), "skipped-noop")

    def test_missing_tool_is_unavailable_not_failure(self):
        with tempfile.TemporaryDirectory() as td:
            with (
                patch.object(main, "REPO_DIR", Path(td)),
                patch.object(
                    main, "_project_env_plan",
                    return_value=[("x", ["definitely-not-a-real-binary-xyz", "a"])],
                ),
                patch.dict(os.environ, clean_env(), clear=True),
            ):
                self.assertEqual(
                    main._maybe_rebuild_project_env(), "skipped-unavailable"
                )

    def test_success_and_failure_statuses(self):
        with tempfile.TemporaryDirectory() as td:
            with (
                patch.object(main, "REPO_DIR", Path(td)),
                patch.object(
                    main, "_project_env_plan", return_value=[("ok", ["true"])],
                ),
                patch.dict(os.environ, clean_env(), clear=True),
            ):
                self.assertEqual(main._maybe_rebuild_project_env(), "rebuilt")
            with (
                patch.object(main, "REPO_DIR", Path(td)),
                patch.object(
                    main, "_project_env_plan", return_value=[("bad", ["false"])],
                ),
                patch.dict(os.environ, clean_env(), clear=True),
            ):
                self.assertEqual(main._maybe_rebuild_project_env(), "rebuild-failed")


class WrapperCapsTests(unittest.TestCase):
    def run_wrapper(self, extra_env):
        dump = Path(tempfile.mkdtemp()) / "dump.sh"
        dump.write_text(
            "#!/bin/bash\n"
            'echo "NODE_OPTIONS=${NODE_OPTIONS:-<empty>}"\n'
            'echo "MAKEFLAGS=${MAKEFLAGS:-<empty>}"\n'
            'echo "SCH_BUILD_JOBS=${SCH_BUILD_JOBS:-<empty>}"\n'
        )
        dump.chmod(dump.stat().st_mode | stat.S_IXUSR)
        env = clean_env(
            SCH_HARNESS="opencode",
            SCH_HARNESS_REAL=str(dump),
            SCH_HARNESS_WAIT="0",
            HOME=tempfile.mkdtemp(),
        )
        env.update(extra_env)
        proc = subprocess.run(
            ["bash", str(WRAPPER)],
            env=env, text=True, capture_output=True, timeout=30,
        )
        return proc

    def test_wrapper_applies_caps(self):
        proc = self.run_wrapper({})
        self.assertIn("--max-old-space-size=1792", proc.stdout)
        self.assertIn("-j2", proc.stdout)

    def test_wrapper_heap_escape_hatch(self):
        proc = self.run_wrapper({"SCH_NODE_HEAP_MB": "0"})
        self.assertIn("NODE_OPTIONS=<empty>", proc.stdout)

    def test_wrapper_operator_heap_wins(self):
        proc = self.run_wrapper(
            {"NODE_OPTIONS": "--max-old-space-size=8192"}
        )
        self.assertIn("--max-old-space-size=8192", proc.stdout)
        self.assertNotIn("1792", proc.stdout)


if __name__ == "__main__":
    unittest.main()
