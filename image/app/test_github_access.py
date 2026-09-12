"""Offline tests for the opt-in GitHub access (TASK-26).

The default stays credential-less (spec git-native-workflow R21/R22); with a
staged ``GITHUB_TOKEN`` the shim wires the recorded ``origin`` and a
tmpfs-backed credential helper on git-native workspaces, and the dispatcher
exports ``GH_TOKEN``/``GITHUB_TOKEN`` on every harness. Real git repos in temp
dirs and the real ``harness-wrapper.sh``; no AWS, no network.
"""

from __future__ import annotations

import __future__
import asyncio
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).parents[1]
WRAPPER = ROOT / "scripts" / "harness-wrapper.sh"

GITHUB_TOKEN = "github_pat_testtoken0123456789abcdef"


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
    module_name = "sch_github_access_main"
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
            # Isolate from the developer machine: the microVM has no system
            # or global credential helper, and these tests assert exactly
            # that baseline (helper absent until SCH sets it).
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.environ.get("SCH_TEST_GIT_GLOBAL", "/dev/null"),
        },
    )


def make_source_repo(root: Path) -> str:
    src = root / "src"
    src.mkdir()
    _git(["init", "-q"], src)
    (src / "a.txt").write_text("a\n")
    _git(["add", "-A"], src)
    _git(["commit", "-qm", "one"], src)
    return _git(["rev-parse", "HEAD"], src).stdout.strip()


class SanitizeOriginUrlTests(unittest.TestCase):
    def test_https_survives(self):
        self.assertEqual(
            main._sanitize_origin_url("https://github.com/o/r.git"),
            "https://github.com/o/r.git",
        )

    def test_userinfo_is_stripped(self):
        self.assertEqual(
            main._sanitize_origin_url("https://x-access-token:secret@github.com/o/r.git"),
            "https://github.com/o/r.git",
        )

    def test_ssh_forms_are_rewritten(self):
        self.assertEqual(
            main._sanitize_origin_url("git@github.com:o/r.git"),
            "https://github.com/o/r.git",
        )
        self.assertEqual(
            main._sanitize_origin_url("ssh://git@github.com/o/r"),
            "https://github.com/o/r",
        )

    def test_non_github_and_garbage_yield_empty(self):
        for raw in (
            "", None, 42,
            "https://gitlab.com/o/r.git",
            "http://github.com/o/r.git",
            "git@gitlab.com:o/r.git",
            "https://github.com/o",
            "https://github.com/../evil",
            "/local/path",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(main._sanitize_origin_url(raw), "")


class TokenUrlSafetyTests(unittest.TestCase):
    def test_plain_token_is_safe(self):
        self.assertTrue(main._token_url_safe(GITHUB_TOKEN))

    def test_url_breaking_tokens_are_rejected(self):
        for bad in ("", "a b", "a@b", "a:b", "a/b", "a?b", "a#b", "a\nb", "a\\b"):
            with self.subTest(bad=bad):
                self.assertFalse(main._token_url_safe(bad))


class GithubAccessBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        # Empty global gitconfig + no system config for every git process the
        # shim spawns too (main._git inherits os.environ): this is the
        # microVM baseline the reconciliation is specified against.
        (self.root / "gitconfig-global").write_text("")
        self._env_patch = patch.dict(os.environ, {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(self.root / "gitconfig-global"),
            "SCH_TEST_GIT_GLOBAL": str(self.root / "gitconfig-global"),
        })
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _git(["init", "-q"], self.repo)
        main.REPO_DIR = self.repo
        main.BUNDLE_STAGING_DIR = self.root / "state" / "bundles"
        main.BUNDLE_STAGING_DIR.mkdir(parents=True)
        main.GIT_NATIVE_STATE_FILE = self.root / "state" / "git-native.json"
        main.PROVIDER_KEYS_FILE = self.root / "run" / "provider-keys.env"
        main.GIT_CREDENTIALS_FILE = self.root / "run" / "git-credentials"
        main._WORKSPACE_READY.set()
        main._WORKSPACE_NAME["value"] = "ws-a"
        main._SESSION_EPOCH["value"] = 0
        main._STORAGE_BACKEND["value"] = "session"
        main.CHECKPOINT_BUCKET = ""
        self.head = make_source_repo(self.root)

    def stage_keys(self, keys):
        return main._stage_provider_keys({"provider_keys": dict(keys)})

    def helper(self):
        proc = _git(["config", "--get", "credential.helper"], self.repo)
        return proc.stdout.strip() if proc.returncode == 0 else None

    def origin(self):
        proc = _git(["remote", "get-url", "origin"], self.repo)
        return proc.stdout.strip() if proc.returncode == 0 else None

    def origin_managed(self):
        proc = _git(["config", "--get", "remote.origin.schManaged"], self.repo)
        return proc.stdout.strip() if proc.returncode == 0 else None

    def seed(self, branch="feat/x", origin_url=""):
        bundle = self.root / "seed.bundle"
        _git(["bundle", "create", str(bundle), "HEAD"], self.root / "src")
        target = main.BUNDLE_STAGING_DIR / main.SEED_BUNDLE_NAME
        target.write_bytes(bundle.read_bytes())
        payload = {"branch": branch}
        if origin_url:
            payload["originUrl"] = origin_url
        return main._handle_git_seed(payload)


class GitSeedOriginTests(GithubAccessBase):
    def test_seed_records_sanitized_origin(self):
        self.stage_keys({})
        resp = self.seed(origin_url="git@github.com:o/r.git")
        self.assertEqual(resp["status"], "ok")
        self.assertEqual(resp["originUrl"], "https://github.com/o/r.git")
        state = json.loads(main.GIT_NATIVE_STATE_FILE.read_text())
        self.assertEqual(state["originUrl"], "https://github.com/o/r.git")

    def test_seed_without_origin_url_records_no_origin(self):
        self.stage_keys({})
        resp = self.seed()
        self.assertEqual(resp["status"], "ok")
        self.assertNotIn("originUrl", resp)
        state = json.loads(main.GIT_NATIVE_STATE_FILE.read_text())
        self.assertNotIn("originUrl", state)

    def test_seed_drops_non_github_origin(self):
        self.stage_keys({})
        resp = self.seed(origin_url="https://gitlab.com/o/r.git")
        self.assertEqual(resp["status"], "ok")
        self.assertNotIn("originUrl", resp)

    def test_seed_with_token_applies_access_immediately(self):
        self.stage_keys({"GITHUB_TOKEN": GITHUB_TOKEN})
        resp = self.seed(origin_url="https://github.com/o/r.git")
        self.assertEqual(resp["status"], "ok")
        self.assertEqual(self.origin(), "https://github.com/o/r.git")
        self.assertEqual(self.origin_managed(), "true")
        self.assertEqual(self.helper(), main._git_credential_helper())

    def test_seed_without_token_adds_no_origin(self):
        self.stage_keys({})
        resp = self.seed(origin_url="https://github.com/o/r.git")
        self.assertEqual(resp["status"], "ok")
        self.assertIsNone(self.origin())
        self.assertIsNone(self.helper())


class ReconcileTests(GithubAccessBase):
    def write_state(self, origin_url="https://github.com/o/r.git"):
        main._write_git_native_state("feat/x", self.head, origin_url)

    def test_token_wires_helper_origin_and_store(self):
        self.write_state()
        self.stage_keys({"GITHUB_TOKEN": GITHUB_TOKEN})
        main._reconcile_github_access()
        self.assertEqual(self.helper(), main._git_credential_helper())
        self.assertEqual(self.origin(), "https://github.com/o/r.git")
        self.assertEqual(self.origin_managed(), "true")
        body = main.GIT_CREDENTIALS_FILE.read_text()
        self.assertEqual(body, f"https://x-access-token:{GITHUB_TOKEN}@github.com\n")
        mode = stat.S_IMODE(main.GIT_CREDENTIALS_FILE.stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_reconcile_is_idempotent(self):
        self.write_state()
        self.stage_keys({"GITHUB_TOKEN": GITHUB_TOKEN})
        main._reconcile_github_access()
        main._reconcile_github_access()
        self.assertEqual(self.origin(), "https://github.com/o/r.git")
        self.assertEqual(self.helper(), main._git_credential_helper())

    def test_withdrawal_removes_store_helper_and_managed_origin(self):
        self.write_state()
        self.stage_keys({"GITHUB_TOKEN": GITHUB_TOKEN})
        main._reconcile_github_access()
        self.assertTrue(main.GIT_CREDENTIALS_FILE.is_file())
        self.stage_keys({})
        main._reconcile_github_access()
        self.assertFalse(main.GIT_CREDENTIALS_FILE.exists())
        self.assertIsNone(self.helper())
        self.assertIsNone(self.origin())

    def test_rotation_replaces_the_store(self):
        self.write_state()
        self.stage_keys({"GITHUB_TOKEN": "first-token-value"})
        main._reconcile_github_access()
        self.stage_keys({"GITHUB_TOKEN": "second-token-value"})
        main._reconcile_github_access()
        body = main.GIT_CREDENTIALS_FILE.read_text()
        self.assertIn("second-token-value", body)
        self.assertNotIn("first-token-value", body)

    def test_operator_origin_is_never_touched(self):
        _git(["remote", "add", "origin", "https://github.com/op/theirs.git"], self.repo)
        self.write_state()
        self.stage_keys({"GITHUB_TOKEN": GITHUB_TOKEN})
        main._reconcile_github_access()
        self.assertEqual(self.origin(), "https://github.com/op/theirs.git")
        self.assertIsNone(self.origin_managed())
        # ... including on withdrawal: only SCH-managed origins are removed.
        self.stage_keys({})
        main._reconcile_github_access()
        self.assertEqual(self.origin(), "https://github.com/op/theirs.git")

    def test_operator_helper_is_never_touched(self):
        _git(["config", "credential.helper", "store --file /operator/creds"], self.repo)
        self.write_state()
        self.stage_keys({"GITHUB_TOKEN": GITHUB_TOKEN})
        main._reconcile_github_access()
        self.assertEqual(self.helper(), "store --file /operator/creds")
        self.stage_keys({})
        main._reconcile_github_access()
        self.assertEqual(self.helper(), "store --file /operator/creds")

    def test_without_git_native_state_nothing_happens(self):
        self.stage_keys({"GITHUB_TOKEN": GITHUB_TOKEN})
        main._reconcile_github_access()
        self.assertIsNone(self.helper())
        self.assertIsNone(self.origin())
        self.assertFalse(main.GIT_CREDENTIALS_FILE.exists())

    def test_url_unsafe_token_is_treated_as_absent(self):
        self.write_state()
        self.stage_keys({"GITHUB_TOKEN": GITHUB_TOKEN})
        main._reconcile_github_access()
        self.assertEqual(self.helper(), main._git_credential_helper())
        self.stage_keys({"GITHUB_TOKEN": "has a space"})
        main._reconcile_github_access()
        self.assertIsNone(self.helper())
        self.assertIsNone(self.origin())

    def test_unrelated_keys_are_dropped_from_staging(self):
        names = self.stage_keys({"GITHUB_TOKEN": GITHUB_TOKEN, "EVIL": "x"})
        self.assertEqual(names, ["GITHUB_TOKEN"])

    def test_no_secret_in_git_config_or_logs(self):
        self.write_state()
        self.stage_keys({"GITHUB_TOKEN": GITHUB_TOKEN})
        with self.assertLogs(main.logger, "INFO") as logs:
            main._reconcile_github_access()
        config_text = (self.repo / ".git" / "config").read_text()
        self.assertNotIn(GITHUB_TOKEN, config_text)
        self.assertNotIn(GITHUB_TOKEN, "\n".join(logs.output))
        # The helper pointer itself is credential-free, so it may persist.
        self.assertIn(str(main.GIT_CREDENTIALS_FILE), config_text)


class WrapperGithubMappingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.bindir = root / "bin"
        self.bindir.mkdir()
        for name in ("opencode", "claude", "pi"):
            dest = self.bindir / name
            dest.write_text(WRAPPER.read_text())
            dest.chmod(0o755)
        self.env_dump = root / "env.json"
        self.real = root / "fake-harness"
        self.real.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os\n"
            f"json.dump(dict(os.environ), open({str(self.env_dump)!r}, 'w'))\n"
        )
        self.real.chmod(0o755)
        self.workspace = root / "workspace"
        (self.workspace / "state").mkdir(parents=True)
        self.staging_file = root / "provider-keys.env"

    def stage(self, keys):
        self.staging_file.write_text(
            "".join(f"SCH_{name}={value}\n" for name, value in keys.items())
        )
        self.staging_file.chmod(0o600)

    def run_wrapper(self, harness, staged=None, extra_env=None):
        if staged:
            self.stage(staged)
        elif self.staging_file.exists():
            self.staging_file.unlink()
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/home/sch"),
            "SCH_HARNESS": harness,
            "SCH_HARNESS_REAL": str(self.real),
            "SCH_WORKSPACE_ROOT": str(self.workspace),
            "SCH_PROVIDER_KEYS_FILE": str(self.staging_file),
            "SCH_HARNESS_WAIT": "0",
            "CLAUDE_CODE_USE_BEDROCK": "1",
        }
        env.update(extra_env or {})
        if self.env_dump.exists():
            self.env_dump.unlink()
        proc = subprocess.run(
            [str(self.bindir / harness)], env=env,
            text=True, capture_output=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn(GITHUB_TOKEN, proc.stdout + proc.stderr)
        return json.loads(self.env_dump.read_text())

    def test_every_harness_exports_both_token_names(self):
        for harness in ("opencode", "claude", "pi"):
            with self.subTest(harness=harness):
                env = self.run_wrapper(harness, {"GITHUB_TOKEN": GITHUB_TOKEN})
                self.assertEqual(env["GH_TOKEN"], GITHUB_TOKEN)
                self.assertEqual(env["GITHUB_TOKEN"], GITHUB_TOKEN)

    def test_absent_token_stays_absent_everywhere(self):
        for harness in ("opencode", "claude", "pi"):
            with self.subTest(harness=harness):
                env = self.run_wrapper(harness, {"ANTHROPIC_API_KEY": "x"})
                self.assertNotIn("GH_TOKEN", env)
                self.assertNotIn("GITHUB_TOKEN", env)

    def test_operator_token_override_wins(self):
        env = self.run_wrapper(
            "opencode", {"GITHUB_TOKEN": GITHUB_TOKEN},
            extra_env={"GH_TOKEN": "operator-value"},
        )
        self.assertEqual(env["GH_TOKEN"], "operator-value")

    def test_claude_keeps_bedrock_with_only_the_github_token(self):
        # The token is harness-independent: no provider switchover on claude.
        env = self.run_wrapper("claude", {"GITHUB_TOKEN": GITHUB_TOKEN})
        self.assertEqual(env["CLAUDE_CODE_USE_BEDROCK"], "1")
        self.assertNotIn("ANTHROPIC_API_KEY", env)


if __name__ == "__main__":
    unittest.main()
