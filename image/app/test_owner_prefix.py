"""Offline tests for the owner-scoped shim of per-principal isolation (TASK-20.2).

Spec: docs/specs/security/per-principal-isolation.md R26-R34.

Contract under test:
  - the owner prefix comes only from SCH_OWNER_PREFIX; an invalid value makes
    every invocation (and the bootstrap) fail before any S3 call or write;
  - a payload whose owner_prefix disagrees with the runtime is rejected before
    any other effect; a payload without it is accepted;
  - with the prefix set, checkpoints, generations, writer claims and task
    status live under the owner segment; without it the keys are unchanged;
  - with the prefix set, nothing is written into the workspace root before
    the bootstrap restore completes, for the s3 and session backends, while
    concurrent invocations arrive;
  - task prompts never reach the logs.

Every path the shim touches is redirected into a temporary directory, and the
module is loaded with threading.Thread patched, so the live session of the
microVM running the tests is never disturbed.
"""

from __future__ import annotations

import __future__
import asyncio
import importlib.util
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
import types
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

APP_DIR = Path(__file__).parent
PREFIX = "o.0123456789abcdef"
OTHER_PREFIX = "o.fedcba9876543210"
WS = "ws-" + "a" * 40


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


_SANDBOX = tempfile.TemporaryDirectory(prefix="sch-owner-prefix-")
SANDBOX = Path(_SANDBOX.name)
_LOAD_COUNT = [0]


def load_main(owner_prefix):
    """A fresh shim module whose import-time environment carries
    ``owner_prefix`` (None = variable unset) and sandboxed paths."""
    _LOAD_COUNT[0] += 1
    sys.path.insert(0, str(APP_DIR))
    bedrock = types.ModuleType("bedrock_agentcore")
    bedrock.BedrockAgentCoreApp = FakeApp
    boto3 = types.ModuleType("boto3")
    boto3.client = lambda *_args, **_kwargs: None
    module_name = f"sch_owner_prefix_main_{_LOAD_COUNT[0]}"
    spec = importlib.util.spec_from_file_location(module_name, APP_DIR / "main.py")
    module = importlib.util.module_from_spec(spec)
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    env = {
        "SCH_TELEGRAM_ENABLED_MARKER": str(SANDBOX / "telegram-enabled"),
        "SCH_PROVIDER_KEYS_FILE": str(SANDBOX / "provider-keys.env"),
        "SCH_GIT_CREDENTIALS_FILE": str(SANDBOX / "git-credentials"),
        "SCH_CHECKPOINT_TMP_DIR": str(SANDBOX / "checkpoint-tmp"),
        "SCH_WORKSPACE_ROOT": str(SANDBOX / "import-root"),
        "SCH_RUN_ONCE_MARKER": str(SANDBOX / "run-once.json"),
        "SCH_COMMAND_SHELL_PRESENCE_FILE": str(SANDBOX / "presence.json"),
        "SCH_OPENCODE_SERVE_PASSWORD_FILE": str(SANDBOX / "serve.password"),
        "SCH_TELEGRAM_SPOOL_DIR": str(SANDBOX / "spool"),
        "SCH_CHECKPOINT_BUCKET": "bucket",
    }
    for name in ("SCH_TELEGRAM_BOT_TOKEN", "SCH_TELEGRAM_CHAT_ID"):
        env[name] = ""
    with patch.dict(os.environ, env), patch.dict(
        sys.modules,
        {module_name: module, "bedrock_agentcore": bedrock, "boto3": boto3},
    ), patch("threading.Thread"):
        os.environ.pop("SCH_OWNER_PREFIX", None)
        if owner_prefix is not None:
            os.environ["SCH_OWNER_PREFIX"] = owner_prefix
        source = (APP_DIR / "main.py").read_text(encoding="utf-8")
        code = compile(
            source, str(APP_DIR / "main.py"), "exec",
            flags=__future__.annotations.compiler_flag, dont_inherit=True,
        )
        exec(code, module.__dict__)
    return module


class MissingObject(Exception):
    def __init__(self):
        super().__init__("NoSuchKey")
        self.response = {"Error": {"Code": "NoSuchKey"}}


class FakeS3:
    """Minimal conditional-write S3; records every key it is asked about."""

    def __init__(self):
        self.objects = {}
        self.etags = {}
        self.counter = 0
        self.keys_seen = []
        self.lock = threading.Lock()

    def _see(self, key):
        with self.lock:
            self.keys_seen.append(key)

    def get_object(self, *, Key, **_kwargs):
        self._see(Key)
        if Key not in self.objects:
            raise MissingObject()
        return {"Body": BytesIO(self.objects[Key]), "ETag": self.etags[Key]}

    def head_object(self, *, Key, **_kwargs):
        self._see(Key)
        if Key not in self.objects:
            raise MissingObject()
        return {"ETag": self.etags[Key]}

    def put_object(self, *, Key, Body, IfMatch=None, IfNoneMatch=None, **_kwargs):
        self._see(Key)
        with self.lock:
            if IfNoneMatch == "*" and Key in self.objects:
                raise RuntimeError("precondition failed")
            if IfMatch is not None and self.etags.get(Key) != IfMatch:
                raise RuntimeError("precondition failed")
            self.counter += 1
            self.objects[Key] = Body
            self.etags[Key] = f'"etag-{self.counter}"'
            return {"ETag": self.etags[Key]}

    def put_object_tagging(self, *, Key, **_kwargs):
        self._see(Key)

    def upload_file(self, filename, _bucket, key, **_kwargs):
        self._see(key)
        with self.lock:
            self.counter += 1
            self.objects[key] = Path(filename).read_bytes()
            self.etags[key] = f'"etag-{self.counter}"'


class ExplodingS3:
    def __getattr__(self, name):
        raise AssertionError(f"S3 must not be called ({name})")


def entries(path: Path) -> list:
    return sorted(p.name for p in path.iterdir()) if path.exists() else []


# --- Prefix loading --------------------------------------------------------------

class LoadOwnerPrefixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = load_main(None)

    def test_unset_and_empty_mean_isolation_off(self):
        self.assertEqual(self.main._load_owner_prefix(None), (None, None))
        self.assertEqual(self.main._load_owner_prefix(""), (None, None))
        self.assertEqual(self.main._load_owner_prefix("  "), (None, None))

    def test_valid_prefix(self):
        self.assertEqual(self.main._load_owner_prefix(PREFIX), (PREFIX, None))

    def test_invalid_prefixes(self):
        for raw in ("o.0123456789ABCDEF", "o.0123", "0123456789abcdef",
                    "o.0123456789abcdef/", "../o.0123456789abcdef", "o.0123456789abcdeg"):
            prefix, error = self.main._load_owner_prefix(raw)
            self.assertIsNone(prefix, raw)
            self.assertIn("SCH_OWNER_PREFIX", error)


# --- Key layout ------------------------------------------------------------------

class KeyLayoutTests(unittest.TestCase):
    def test_isolation_off_keys_are_unchanged(self):
        main = load_main(None)
        self.assertEqual(main._s3_key("ws", "manifest.json"), "checkpoints/ws/manifest.json")
        self.assertEqual(main._s3_key("ws", "task-status.json"), "checkpoints/ws/task-status.json")
        self.assertEqual(
            main._generation_key("ws", "g1", "repo.tar.gz"),
            "checkpoint-generations/ws/g1/repo.tar.gz",
        )
        self.assertEqual(main._writer_claim_key("ws"), "workspace-writers/ws.json")

    def test_isolation_on_keys_carry_the_owner_segment(self):
        main = load_main(PREFIX)
        self.assertEqual(main._s3_key(WS, "manifest.json"), f"checkpoints/{PREFIX}/{WS}/manifest.json")
        self.assertEqual(
            main._s3_key(WS, main.TASK_STATUS_OBJECT),
            f"checkpoints/{PREFIX}/{WS}/task-status.json",
        )
        self.assertEqual(
            main._s3_key(WS, main.TELEGRAM_TOPIC_OBJECT),
            f"checkpoints/{PREFIX}/{WS}/telegram-topic.json",
        )
        self.assertEqual(
            main._generation_key(WS, "g1", "repo.tar.gz"),
            f"checkpoint-generations/{PREFIX}/{WS}/g1/repo.tar.gz",
        )
        self.assertEqual(main._writer_claim_key(WS), f"workspace-writers/{PREFIX}/{WS}.json")

    def test_no_other_key_literal_in_the_shim(self):
        # Every key goes through the three helpers: a new literal would
        # silently bypass the owner segment.
        source = (APP_DIR / "main.py").read_text(encoding="utf-8")
        for top in ("checkpoints", "checkpoint-generations", "workspace-writers"):
            found = re.findall(r'f"%s/(\{[^}]*\})?' % re.escape(top), source)
            self.assertTrue(found, top)
            for segment in found:
                self.assertIn(segment, ("{_owner_segment()}", "{OWNER_PREFIX}"), top)


class CheckpointLayoutTests(unittest.TestCase):
    """End to end through the real checkpoint pass, writer claim and task
    status upload, on the s3 backend."""

    def run_checkpoint(self, owner_prefix, workspace):
        main = load_main(owner_prefix)
        with tempfile.TemporaryDirectory(dir=SANDBOX) as td:
            root = Path(td)
            main.ACTIVE_WORKSPACE_FILE = root / "active.json"
            main.S3_WORKSPACE_ROOT = root / "s3-root"
            main.CHECKPOINT_TMP_DIR = root / "tmp"
            main.OPENCODE_DB_LOCAL = root / "local" / "opencode.db"
            main.CLAUDE_CONFIG_DIR_LOCAL = root / "local" / "claude"
            main.PI_CONFIG_DIR_LOCAL = root / "local" / "pi"
            with patch.dict(os.environ, {}):
                self.assertTrue(main._set_storage_backend("s3"))
            main.REPO_DIR.mkdir(parents=True)
            (main.REPO_DIR / "file.txt").write_text("repo")
            (main.STATE_DIR / "config").mkdir(parents=True)
            main._WORKSPACE_NAME["value"] = workspace
            main._HARNESS["value"] = "opencode"
            main._SESSION_EPOCH["value"] = 1
            main._WORKSPACE_READY.set()
            fake = FakeS3()
            with patch.object(main, "_s3", return_value=fake):
                # invoke() claims the writer before any action runs.
                main._assert_writer_claim(workspace)
                result = main._do_checkpoint(force=True)
                self.assertTrue(main._upload_task_status(workspace, {"state": "none"}))
                manifest = main._download_manifest(workspace)
        self.assertEqual(result.get("status"), "ok", result)
        return fake, manifest

    def test_isolation_on_layout(self):
        fake, manifest = self.run_checkpoint(PREFIX, WS)
        for key in fake.objects:
            self.assertTrue(
                key.startswith((f"checkpoints/{PREFIX}/{WS}/",
                                f"checkpoint-generations/{PREFIX}/{WS}/",
                                f"workspace-writers/{PREFIX}/{WS}.json")),
                key,
            )
        self.assertIn(f"workspace-writers/{PREFIX}/{WS}.json", fake.objects)
        self.assertIn(f"checkpoints/{PREFIX}/{WS}/manifest.json", fake.objects)
        self.assertIn(f"checkpoints/{PREFIX}/{WS}/task-status.json", fake.objects)
        for key in manifest["artifacts"].values():
            self.assertTrue(key.startswith(f"checkpoint-generations/{PREFIX}/{WS}/"), key)

    def test_isolation_off_layout(self):
        fake, manifest = self.run_checkpoint(None, "my-ws")
        self.assertIn("workspace-writers/my-ws.json", fake.objects)
        self.assertIn("checkpoints/my-ws/manifest.json", fake.objects)
        self.assertIn("checkpoints/my-ws/task-status.json", fake.objects)
        for key in fake.objects:
            self.assertNotIn("/o.", key)
        for key in manifest["artifacts"].values():
            self.assertTrue(key.startswith("checkpoint-generations/my-ws/"), key)

    def test_manifest_pointing_outside_the_owner_is_rejected(self):
        main = load_main(PREFIX)
        manifest = {
            "harness": "opencode", "storage": "s3", "session_epoch": 1,
            "artifacts": {
                "repo.tar.gz": f"checkpoint-generations/{OTHER_PREFIX}/{WS}/g/repo.tar.gz",
                "state.tar.gz": f"checkpoint-generations/{PREFIX}/{WS}/g/state.tar.gz",
            },
        }
        with self.assertRaises(main.ManifestReadError):
            main._validate_manifest(dict(manifest), "s3")
        manifest["artifacts"]["repo.tar.gz"] = f"checkpoint-generations/{PREFIX}/{WS}/g/repo.tar.gz"
        main._validate_manifest(dict(manifest), "s3")


# --- Invocation gate -------------------------------------------------------------

class InvokeGateTests(unittest.TestCase):
    def sandbox(self, main):
        td = tempfile.TemporaryDirectory(dir=SANDBOX)
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        main.ACTIVE_WORKSPACE_FILE = root / "active.json"
        main.S3_WORKSPACE_ROOT = root / "s3-root"
        main.SESSION_WORKSPACE_ROOT = root / "session-root"
        env = patch.dict(os.environ, {})
        env.start()
        self.addCleanup(env.stop)
        return root

    def test_invalid_prefix_fails_every_invocation_without_effects(self):
        main = load_main("o.NOT-VALID")
        root = self.sandbox(main)
        self.assertIsNone(main.OWNER_PREFIX)
        with patch.object(main, "_s3", return_value=ExplodingS3()), \
                patch.object(main, "_stage_provider_keys") as stage:
            for action in ("noop", "info", "task", "checkpoint", "prepare-run"):
                response = main.invoke({
                    "action": action, "workspace": WS, "storage_backend": "s3",
                    "session_epoch": 3, "prompt": "p",
                })
                self.assertEqual(response["status"], "error", action)
                self.assertIn("SCH_OWNER_PREFIX", response["error"])
            stage.assert_not_called()
        self.assertEqual(main._SESSION_EPOCH["value"], 0)
        self.assertIsNone(main._STORAGE_BACKEND["value"])
        self.assertIsNone(main._WORKSPACE_NAME["value"])
        self.assertEqual(entries(root), [])

    def test_invalid_prefix_bootstrap_stops_before_storage(self):
        main = load_main("o.bad")
        self.sandbox(main)
        with patch.object(main, "_s3", return_value=ExplodingS3()), \
                patch.object(main, "_wait_for_mount_restore") as wait:
            main._bootstrap()
        wait.assert_not_called()
        self.assertEqual(main.BOOT_STATE["phase"], "error")
        self.assertIn("SCH_OWNER_PREFIX", main.BOOT_STATE["error"])

    def test_mismatching_payload_prefix_is_rejected_before_any_effect(self):
        main = load_main(PREFIX)
        root = self.sandbox(main)
        for claimed in (OTHER_PREFIX, "", "o.x", 7):
            with patch.object(main, "_s3", return_value=ExplodingS3()), \
                    patch.object(main, "_stage_provider_keys") as stage:
                response = main.invoke({
                    "action": "noop", "owner_prefix": claimed, "workspace": WS,
                    "storage_backend": "s3", "session_epoch": 3,
                })
            self.assertEqual(response["status"], "rejected", claimed)
            stage.assert_not_called()
        self.assertEqual(main._SESSION_EPOCH["value"], 0)
        self.assertIsNone(main._STORAGE_BACKEND["value"])
        self.assertEqual(entries(root), [])

    def test_payload_prefix_on_a_runtime_without_isolation_is_rejected(self):
        main = load_main(None)
        self.sandbox(main)
        response = main.invoke({"action": "noop", "owner_prefix": PREFIX})
        self.assertEqual(response["status"], "rejected")

    def test_matching_or_absent_payload_prefix_is_accepted(self):
        for payload_prefix in (PREFIX, None):
            main = load_main(PREFIX)
            self.sandbox(main)
            payload = {"action": "noop", "workspace": WS}
            if payload_prefix:
                payload["owner_prefix"] = payload_prefix
            with patch.object(main, "_s3", return_value=FakeS3()):
                response = main.invoke(payload)
            self.assertEqual(response["status"], "ok", payload)
            self.assertEqual(main._WORKSPACE_NAME["value"], WS)

    def test_non_registry_workspace_is_rejected_with_isolation_on(self):
        main = load_main(PREFIX)
        self.sandbox(main)
        with patch.object(main, "_s3", return_value=ExplodingS3()):
            response = main.invoke({"action": "noop", "workspace": "my-ws"})
        self.assertEqual(response["status"], "rejected")
        self.assertIsNone(main._WORKSPACE_NAME["value"])

    def test_session_id_fallback_is_off_with_isolation_on(self):
        context = types.SimpleNamespace(
            session_id="sch-my-ws-12345678-1234-1234-1234-123456789abc"
        )
        main = load_main(PREFIX)
        self.sandbox(main)
        self.assertEqual(main.invoke({"action": "noop"}, context)["status"], "ok")
        self.assertIsNone(main._WORKSPACE_NAME["value"])
        main = load_main(None)
        self.sandbox(main)
        with patch.object(main, "_s3", return_value=FakeS3()):
            main.invoke({"action": "noop"}, context)
        self.assertEqual(main._WORKSPACE_NAME["value"], "my-ws")

    def test_marker_workspace_must_be_a_registry_identity_with_isolation_on(self):
        main = load_main(PREFIX)
        with patch.object(main, "_read_marker", return_value={"workspace": "my-ws"}):
            self.assertIsNone(main._resolve_workspace_name())
        with patch.object(main, "_read_marker", return_value={"workspace": WS}):
            self.assertEqual(main._resolve_workspace_name(), WS)

    def test_websocket_first_message_is_gated_too(self):
        main = load_main(PREFIX)
        self.sandbox(main)

        class FakeSocket:
            def __init__(self, first):
                self.first = first
                self.sent = []
                self.closed = None

            async def accept(self):
                return None

            async def receive_json(self):
                return self.first

            async def send_json(self, data):
                self.sent.append(data)

            async def close(self, code=1000):
                self.closed = code

        starlette = types.ModuleType("starlette")
        websockets = types.ModuleType("starlette.websockets")
        websockets.WebSocketDisconnect = type("WebSocketDisconnect", (Exception,), {})
        for first in ({"tunnel_id": "t", "mode": "tcp", "port": 1, "owner_prefix": OTHER_PREFIX},
                      {"tunnel_id": "t", "mode": "tcp", "port": 1, "workspace": "my-ws"}):
            socket = FakeSocket(first)
            with patch.dict(sys.modules, {"starlette": starlette,
                                          "starlette.websockets": websockets}), \
                    patch.object(main, "_ensure_tunnel_sweep_started"):
                asyncio.run(main.tunnel_websocket_handler(socket, None))
            self.assertEqual(socket.closed, 1008, first)
            self.assertEqual(socket.sent[0]["type"], "error")
            self.assertEqual(main._SESSION_EPOCH["value"], 0)


# --- No early writes (R31) -------------------------------------------------------

class NoEarlyWritesTests(unittest.TestCase):
    """The bootstrap is held inside the restore while invocations of every
    action arrive; the workspace root must stay empty until the restore
    promotes, and the bootstrap must still reach `ready` afterwards."""

    def setUp(self):
        self.main = load_main(PREFIX)
        td = tempfile.TemporaryDirectory(dir=SANDBOX)
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)
        main = self.main
        main.ACTIVE_WORKSPACE_FILE = self.root / "active.json"
        main.S3_WORKSPACE_ROOT = self.root / "s3-root"
        main.SESSION_WORKSPACE_ROOT = self.root / "session-root"
        main.SESSION_RESTORE_MARKER = main.SESSION_WORKSPACE_ROOT / ".sch-restore-promotion.json"
        main.CHECKPOINT_TMP_DIR = self.root / "tmp"
        local = self.root / "local"
        main.OPENCODE_DB_LOCAL = local / "opencode" / "opencode.db"
        main.READY_MARKER = local / "opencode" / ".ready"
        main.CLAUDE_CONFIG_DIR_LOCAL = local / "claude"
        main.CLAUDE_READY_MARKER = local / "claude" / ".ready"
        main.PI_CONFIG_DIR_LOCAL = local / "pi"
        main.PI_READY_MARKER = local / "pi" / ".ready"
        main.RUN_ONCE_MARKER = local / "run-once.json"
        main.OPENCODE_SERVE_PASSWORD_FILE = local / "serve.password"
        main.FS_WORKSPACE_READY_TIMEOUT_S = 0.3
        env = patch.dict(os.environ, {})
        env.start()
        self.addCleanup(env.stop)
        self.fake_s3 = FakeS3()
        self.release = threading.Event()
        self.in_restore = threading.Event()
        self.reconcile_calls = []
        self.task_workers = []
        patches = [
            patch.object(main, "_s3", return_value=self.fake_s3),
            patch.object(main, "_run_init_workspace", return_value={"exit": 0}),
            patch.object(main, "_verify_workspace_seeded", return_value=True),
            patch.object(main, "_verify_l2_restore", return_value=True),
            patch.object(main, "_restore_db", return_value="no-backup"),
            patch.object(main, "_checkpoint_loop", return_value=None),
            patch.object(main, "_busy_keepalive_loop", return_value=None),
            patch.object(main, "_session_restore_guard_loop", return_value=None),
            patch.object(main, "_start_telegram_notifier", return_value=None),
            patch.object(main.atexit, "register", return_value=None),
            patch.object(main, "_opencode_version", return_value="2.0.18"),
            patch.object(main, "_claude_version", return_value="x"),
            patch.object(main, "_pi_version", return_value="x"),
            patch.object(main, "_ensure_serve_supervisor_started", return_value=None),
            patch.object(main, "_serve_is_alive", return_value=True),
            patch.object(main, "_reconcile_github_access", side_effect=self.record_reconcile),
            patch.object(main, "_run_task", side_effect=self.fake_run_task),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def record_reconcile(self):
        self.reconcile_calls.append(self.main._WORKSPACE_READY.is_set()
                                    or threading.current_thread().name == "boot")

    def fake_run_task(self, *_args, **_kwargs):
        # The real worker only polls for readiness before the harness starts.
        self.task_workers.append(threading.current_thread().name)

    def promote(self, main):
        """What a completed restore leaves behind."""
        main.REPO_DIR.mkdir(parents=True, exist_ok=True)
        (main.REPO_DIR / "file.txt").write_text("restored")
        (main.STATE_DIR / "config").mkdir(parents=True, exist_ok=True)

    def blocking_l2_restore(self, _workspace):
        self.in_restore.set()
        self.assertTrue(self.release.wait(30))
        self.promote(self.main)
        return {"attempted": True, "result": "downloaded"}

    def blocking_mount_restore(self):
        # Session storage restored asynchronously by the platform.
        self.in_restore.set()
        self.assertTrue(self.release.wait(30))
        self.promote(self.main)
        return "resumed-settled"

    def concurrent_invokes(self, backend):
        main = self.main
        base = {"workspace": WS, "storage_backend": backend, "owner_prefix": PREFIX,
                "harness": "opencode", "session_epoch": 1}
        payloads = [
            {"action": "noop"},
            {"action": "info"},
            {"action": "checkpoint"},
            {"action": "task", "prompt": "do the thing"},
            {"action": "prepare-run"},
            {"action": "mark-interactive", "active": True},
            {"action": "command-shell-presence", "shellId": "s1", "attachmentId": "a1",
             "state": "attached", "ttl_s": 30},
            {"action": "serve-ensure"},
            {"action": "git-seed", "branch": "sch/x"},
            {"action": "git-snapshot"},
            {"action": "session-import"},
        ]
        responses = {}

        def call(payload):
            responses[payload["action"]] = main.invoke({**base, **payload})

        threads = [threading.Thread(target=call, args=(p,)) for p in payloads]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        return responses

    def run_scenario(self, backend, mount_patch=None):
        main = self.main
        # A local session DB (left by a shim restart, for example) is what a
        # premature checkpoint would back up into the root.
        main.OPENCODE_DB_LOCAL.parent.mkdir(parents=True, exist_ok=True)
        sqlite3.connect(str(main.OPENCODE_DB_LOCAL)).close()
        # First invocation publishes the backend and identity, as `sch` does.
        with patch.object(main, "_restore_l2", side_effect=self.blocking_l2_restore), \
                patch.object(main, "_wait_for_mount_restore",
                             side_effect=mount_patch or (lambda: "s3-local" if backend == "s3"
                                                         else "fresh (hinted)")):
            self.assertEqual(main.invoke({
                "action": "noop", "workspace": WS, "storage_backend": backend,
                "owner_prefix": PREFIX, "harness": "opencode", "session_epoch": 1,
            })["status"], "ok")
            root = main.WORKSPACE_ROOT
            boot = threading.Thread(target=main._bootstrap, name="boot")
            boot.start()
            self.assertTrue(self.in_restore.wait(30))
            self.assertEqual(entries(root), [])
            responses = self.concurrent_invokes(backend)
            # Every invocation answered while the restore was still pending,
            # and none of them wrote into the root.
            self.assertEqual(entries(root), [])
            self.assertFalse(main._WORKSPACE_READY.is_set())
            self.assertEqual(responses["checkpoint"]["result"]["status"], "skipped-not-ready")
            self.assertEqual(responses["task"]["status"], "accepted")
            self.assertEqual(responses["git-seed"]["status"], "error")
            self.assertFalse(any(self.reconcile_calls), "reconcile ran before readiness")
            self.release.set()
            boot.join(30)
        self.assertEqual(main.BOOT_STATE["phase"], "ready", main.BOOT_STATE)
        self.assertIn("repo", entries(root))
        # The bootstrap ran the deferred reconciliation once, before readiness.
        self.assertTrue(self.reconcile_calls and self.reconcile_calls[-1])
        for key in self.fake_s3.keys_seen:
            self.assertIn(f"/{PREFIX}/", key)
        return responses

    def test_s3_restore_with_concurrent_invokes(self):
        self.run_scenario("s3")
        self.assertEqual(self.main.BOOT_STATE["restore_l2"]["result"], "restored")

    def test_session_l2_restore_with_concurrent_invokes(self):
        self.run_scenario("session")
        self.assertEqual(self.main.BOOT_STATE["restore_l2"]["result"], "restored")

    def test_session_platform_restore_with_concurrent_invokes(self):
        self.run_scenario("session", mount_patch=self.blocking_mount_restore)
        # The platform restore populated the mount: no L2 restore needed.
        self.assertEqual(self.main.BOOT_STATE["restore_l2"]["result"], "not-applicable")

    def test_checkpoint_after_readiness_runs(self):
        self.run_scenario("s3")
        with patch.object(self.main, "_backup_db_once", return_value="no-local-db"):
            response = self.main.invoke({"action": "checkpoint", "workspace": WS,
                                         "storage_backend": "s3", "session_epoch": 1})
        self.assertEqual(response["result"]["status"], "ok", response)


# --- Prompts out of the logs (R32) ----------------------------------------------

class PromptRedactionTests(unittest.TestCase):
    PROMPT = "SECRET-PROMPT refactor the billing module"

    def test_redact_argv(self):
        main = load_main(None)
        argv = ["opencode", "run", "--standalone", "--auto", "--", self.PROMPT]
        redacted = main._redact_argv(argv, self.PROMPT)
        self.assertEqual(redacted[:-1], argv[:-1])
        self.assertEqual(redacted[-1], f"<prompt: {len(self.PROMPT)} chars>")

    def test_task_run_logs_no_prompt_in_any_mode(self):
        for owner_prefix in (None, PREFIX):
            main = load_main(owner_prefix)
            with tempfile.TemporaryDirectory(dir=SANDBOX) as td:
                root = Path(td)
                repo = root / "repo"
                repo.mkdir()
                ready = root / ".ready"
                ready.write_text("")

                class FakeProc:
                    returncode = 0
                    pid = 1

                    def communicate(self, timeout=None):
                        return ("", "")

                    def poll(self):
                        return 0

                    def wait(self, timeout=None):
                        return 0

                with patch.object(main, "REPO_DIR", repo), \
                        patch.object(main, "_harness_ready_marker", return_value=ready), \
                        patch.object(main, "_run_task_heartbeat", return_value=None), \
                        patch.object(main, "_finish_task", return_value=None), \
                        patch.object(main, "_opencode_agent_available", return_value=True), \
                        patch.object(main, "_opencode_continue_model", return_value=(None, None)), \
                        patch.object(main.subprocess, "Popen", return_value=FakeProc()), \
                        self.assertLogs(main.logger, level="DEBUG") as logs:
                    main._run_task("t1", self.PROMPT, 60, False, None, WS, "opencode")
                    for harness in ("claude", "pi"):
                        main._run_task("t2", self.PROMPT, 60, False, None, WS, harness)
            text = "\n".join(logs.output)
            self.assertIn("running (harness=opencode)", text)
            self.assertIn(f"<prompt: {len(self.PROMPT)} chars>", text)
            self.assertNotIn("SECRET-PROMPT", text)

    def test_prompt_only_payload_is_not_logged_as_action(self):
        main = load_main(None)
        with patch.dict(os.environ, {}), self.assertLogs(main.logger, level="INFO") as logs:
            main.ACTIVE_WORKSPACE_FILE = SANDBOX / "active-prompt.json"
            main.invoke({"prompt": "SECRET-PROMPT please"})
        self.assertNotIn("SECRET-PROMPT", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
