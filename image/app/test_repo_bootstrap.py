"""Container-free tests for the repository bootstrap of init-workspace.sh (TASK-10).

A first-boot clone with SCH_REPO_URL and SCH_REPO_TOKEN must leave no
credential in the workspace (git saves the clone URL as `origin` in
.git/config, and .git rides every S3 checkpoint), must never put the token on
a command line or a log line, and must still clone a private repository. A
workspace cloned by an earlier image has the embedded credential removed from
its saved `origin` URL at the next boot (spec: runtime-image R22).

The private repository is a bare repository served over git's dumb HTTP
protocol by a local server that requires Basic authentication, so no network
access is needed. GIT_TRACE records the argv of every git process and of every
subprocess git spawns (remote helpers, credential helpers).
"""

from __future__ import annotations

import base64
import http.server
import os
import subprocess
import tempfile
import threading
import unittest
from functools import partial
from pathlib import Path


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "init-workspace.sh"
OPENCODE_TEMPLATES = ROOT / "opencode-templates"

TOKEN = "ghp_TESTTOKEN0123456789abcdefSECRET"
USERNAME = "x-access-token"


class _AuthHandler(http.server.SimpleHTTPRequestHandler):
    """Serves a directory, requiring Basic auth with the expected credentials."""

    expected = "Basic " + base64.b64encode(f"{USERNAME}:{TOKEN}".encode()).decode()
    seen_auth: list = []

    def do_GET(self):  # noqa: N802 (http.server naming)
        auth = self.headers.get("Authorization")
        self.seen_auth.append(auth)
        if auth != self.expected:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="test"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        super().do_GET()

    def log_message(self, *args):
        pass


def _git(*args, cwd=None, env=None):
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, text=True, capture_output=True
    )


class RepoBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.repo = self.workspace / "repo"
        self.home = self.root / "home"
        self.home.mkdir()
        self.trace = self.root / "git-trace.log"
        self.git_env = self._base_env()
        self._make_served_repo()
        self._start_server()

    # -- fixtures ------------------------------------------------------------

    def _base_env(self) -> dict:
        # Hermetic: no SCH_* or GIT_* from the developer environment (a set
        # SCH_REPO_URL would clone the developer repo with the developer token).
        env = {
            k: v for k, v in os.environ.items()
            if not k.startswith(("SCH_", "GIT_"))
        }
        env.update({
            "HOME": str(self.home),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        })
        return env

    def _make_served_repo(self):
        self.served = self.root / "served"
        src = self.root / "src"
        _git("init", "-q", "-b", "main", str(src), env=self.git_env)
        (src / "README.md").write_text("hello\n")
        _git("add", "README.md", cwd=src, env=self.git_env)
        _git("commit", "-q", "-m", "init", cwd=src, env=self.git_env)
        _git("clone", "-q", "--bare", str(src), str(self.served / "repo.git"),
             env=self.git_env)
        _git("update-server-info", cwd=self.served / "repo.git", env=self.git_env)

    def _start_server(self):
        _AuthHandler.seen_auth = []
        handler = partial(_AuthHandler, directory=str(self.served))
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/repo.git"

    def run_init(self, **extra) -> subprocess.CompletedProcess:
        env = dict(self.git_env)
        env.update({
            "SCH_HARNESS": "opencode",
            "SCH_WORKSPACE_ROOT": str(self.workspace),
            "XDG_DATA_HOME": str(self.workspace / "state" / "data"),
            "XDG_CONFIG_HOME": str(self.workspace / "state" / "config"),
            "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
            "PI_CODING_AGENT_DIR": str(self.root / "pi-agent"),
            "SCH_OPENCODE_TEMPLATE_DIR": str(OPENCODE_TEMPLATES),
            "SCH_TELEGRAM_ENABLED_MARKER": str(self.root / "telegram-enabled"),
            "SCH_PROVIDER_KEYS_FILE": str(self.root / "provider-keys.env"),
            "AWS_REGION": "eu-central-1",
            "AWS_DEFAULT_REGION": "eu-central-1",
            "GIT_TRACE": str(self.trace),
        })
        env.update(extra)
        return subprocess.run(
            ["bash", str(SCRIPT)], env=env, text=True, capture_output=True,
            check=True, timeout=120,
        )

    # -- assertions ----------------------------------------------------------

    def assert_secret_absent(self, result, secret=TOKEN):
        self.assertNotIn(secret, result.stdout)
        self.assertNotIn(secret, result.stderr)
        if self.trace.exists():
            trace = self.trace.read_text(errors="replace")
            self.assertIn("trace: ", trace)  # the trace did record argv
            self.assertNotIn(secret, trace)
        needle = secret.encode()
        for base in (self.workspace, self.home):
            for path in base.rglob("*"):
                if path.is_file() and not path.is_symlink():
                    self.assertNotIn(needle, path.read_bytes(), str(path))

    def origin(self, key="url") -> list:
        result = subprocess.run(
            ["git", "-C", str(self.repo), "config", "--get-all", f"remote.origin.{key}"],
            env=self.git_env, text=True, capture_output=True,
        )
        return result.stdout.split()

    def assert_empty_repo(self):
        self.assertTrue((self.repo / ".git").is_dir())
        head = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "--verify", "HEAD"],
            env=self.git_env, capture_output=True,
        )
        self.assertNotEqual(head.returncode, 0, "empty repo must have no commits")
        self.assertEqual(self.origin(), [])

    # -- first-boot clone ------------------------------------------------------

    def test_token_clone_leaves_no_credential_anywhere(self):
        result = self.run_init(SCH_REPO_URL=self.url, SCH_REPO_TOKEN=TOKEN)
        self.assertIn("clone completed", result.stdout)
        self.assertEqual((self.repo / "README.md").read_text(), "hello\n")
        self.assertEqual(self.origin(), [self.url])
        self.assertIn(_AuthHandler.expected, _AuthHandler.seen_auth)
        # The credential helper ran, and its argv was traced without the token.
        self.assertIn("SCH_CLONE_PASSWORD", self.trace.read_text())
        self.assert_secret_absent(result)

    def test_configured_credential_store_does_not_keep_the_token(self):
        _git("config", "--global", "credential.helper", "store", env=self.git_env)
        result = self.run_init(SCH_REPO_URL=self.url, SCH_REPO_TOKEN=TOKEN)
        self.assertIn("clone completed", result.stdout)
        self.assertFalse((self.home / ".git-credentials").exists())
        self.assert_secret_absent(result)

    def test_credentials_embedded_in_the_url_are_moved_off_the_url(self):
        host = self.url.removeprefix("http://")
        url = f"http://{USERNAME}:{TOKEN}@{host}"
        result = self.run_init(SCH_REPO_URL=url)
        self.assertIn("clone completed", result.stdout)
        self.assertEqual(self.origin(), [self.url])
        self.assert_secret_absent(result)

    def test_missing_token_falls_back_to_empty_repo_without_prompting(self):
        result = self.run_init(SCH_REPO_URL=self.url)
        self.assertIn("clone failed", result.stdout)
        self.assert_empty_repo()

    def test_wrong_token_falls_back_to_empty_repo(self):
        wrong = "ghp_WRONGTOKEN9876543210"
        result = self.run_init(SCH_REPO_URL=self.url, SCH_REPO_TOKEN=wrong)
        self.assertIn("clone failed", result.stdout)
        self.assert_empty_repo()
        self.assert_secret_absent(result, secret=wrong)

    def test_public_clone_without_token(self):
        public = self.root / "public.git"
        _git("clone", "-q", "--bare", str(self.served / "repo.git"), str(public),
             env=self.git_env)
        result = self.run_init(SCH_REPO_URL=f"file://{public}")
        self.assertIn("clone completed", result.stdout)
        self.assertEqual((self.repo / "README.md").read_text(), "hello\n")

    # -- earlier-image migration -----------------------------------------------

    def _earlier_image_worktree(self, url: str, pushurl: str | None = None):
        self.repo.mkdir(parents=True)
        _git("init", "-q", str(self.repo), env=self.git_env)
        _git("-C", str(self.repo), "remote", "add", "origin", url, env=self.git_env)
        if pushurl:
            _git("-C", str(self.repo), "config", "remote.origin.pushurl", pushurl,
                 env=self.git_env)

    def test_existing_worktree_origin_is_scrubbed_at_boot(self):
        host = self.url.removeprefix("http://")
        leaked = f"http://{USERNAME}:{TOKEN}@{host}"
        self._earlier_image_worktree(leaked, pushurl=leaked)
        result = self.run_init()
        self.assertIn("removed embedded credentials from remote.origin.url", result.stdout)
        self.assertEqual(self.origin(), [self.url])
        self.assertEqual(self.origin("pushurl"), [self.url])
        self.assert_secret_absent(result)
        # Idempotent: a second boot finds nothing to remove.
        again = self.run_init()
        self.assertNotIn("removed embedded credentials", again.stdout)
        self.assertEqual(self.origin(), [self.url])

    def test_credential_free_origin_is_left_untouched(self):
        host = self.url.removeprefix("http://")
        named = f"http://someone@{host}"
        self._earlier_image_worktree(named)
        result = self.run_init()
        self.assertNotIn("removed embedded credentials", result.stdout)
        self.assertEqual(self.origin(), [named])


if __name__ == "__main__":
    unittest.main()
