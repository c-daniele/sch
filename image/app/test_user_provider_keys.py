"""Offline tests for the per-user provider keys inside the microVM
(add-user-provider-keys, task 2.4).

Four concerns, all exercised against the real shim module and the real
``init-workspace.sh``:

* :class:`StagingTests` — the staging of the payload field: total
  replacement, ``0600``, atomic, absence of the field clears the set, allowlist,
  and no key value in any log or ``info`` output.
* :class:`NoPersistenceTests` — nothing about the keys is ever written to the
  checkpointed mount or to the shim's persisted state (spec: "Nessuna
  persistenza": the L2 checkpoint and its S3 artifacts must be key-free).
* :class:`ChildProcessEnvTests` — every process the shim spawns (headless task,
  the `opencode web` supervisor, the workspace seeding script) receives exactly
  the staged set, with a removed key really removed.
* :class:`ClaudeReconciliationTests` — design D6: the claude Bedrock/API
  reconciliation is recomputed at every bootstrap from the keys actually present
  in the session, in BOTH directions, including the checkpointed-workspace case.
"""

from __future__ import annotations

import __future__
import asyncio
import importlib.util
import json
import logging
import os
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
INIT_SCRIPT = ROOT / "scripts" / "init-workspace.sh"
CLAUDE_TEMPLATES = ROOT / "claude-templates"

ANTHROPIC_KEY = "sk-ant-api03-0123456789abcdefghijKLMNOPQRSTUV"
ANTHROPIC_SUFFIX = ANTHROPIC_KEY[-20:]
OTHER_ANTHROPIC_KEY = "sk-ant-api03-zzzzzzzzzzzzzzzzzzzzWXYZ01234567"
OTHER_SUFFIX = OTHER_ANTHROPIC_KEY[-20:]
OPENROUTER_KEY = "sk-or-v1-secret-openrouter-value"
KILO_KEY = "kilo-secret-value"
# Amazon Bedrock API key (bearer token) issued by a different account (TASK-19).
BEARER_TOKEN = "bedrock-api-key-verify-0123456789abcdef"


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
    module_name = "sch_user_provider_keys_main"
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

_SPOOL_TMP = None
_SPOOL_SAVED = None


def setUpModule():
    # Same hermetic-spool discipline as test_task_model: this module invokes the
    # shim, and a live microVM's SCH_TELEGRAM_* would otherwise deliver for real.
    global _SPOOL_TMP, _SPOOL_SAVED  # noqa: PLW0603
    _SPOOL_TMP = tempfile.TemporaryDirectory()
    _SPOOL_SAVED = main.telegram_notifier.SPOOL_DIR
    main.telegram_notifier.SPOOL_DIR = Path(_SPOOL_TMP.name) / "spool"


def tearDownModule():
    main.telegram_notifier.SPOOL_DIR = _SPOOL_SAVED
    _SPOOL_TMP.cleanup()


class _ShimFixture(unittest.TestCase):
    """A shim with its staging file redirected into a temp dir."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.staging_dir = self.root / "run" / "sch"
        self.staging_file = self.staging_dir / "provider-keys.env"
        self._saved_staging = main.PROVIDER_KEYS_FILE
        main.PROVIDER_KEYS_FILE = self.staging_file
        self.addCleanup(setattr, main, "PROVIDER_KEYS_FILE", self._saved_staging)
        main._HARNESS["value"] = "opencode"
        main._WORKSPACE_NAME["value"] = "workspace"
        main._SESSION_EPOCH["value"] = 0
        main._STORAGE_BACKEND["value"] = "session"
        main.CHECKPOINT_BUCKET = ""
        # A deployment that still had deploy-time keys in the container ENV must
        # not be able to influence the staged set (transition safety).
        for name in main.PROVIDER_KEY_ENV_NAMES:
            os.environ.pop(name, None)

    def invoke(self, keys=None, action="noop", **updates):
        payload = {"action": action, "workspace": "workspace", "harness": "opencode"}
        if keys is not None:
            payload["provider_keys"] = keys
        payload.update(updates)
        return main.invoke(payload)

    def staged_text(self):
        return self.staging_file.read_text(encoding="utf-8")


class StagingTests(_ShimFixture):
    def test_bedrock_api_key_stages_with_the_sch_prefix(self):
        # TASK-19 (cross-account Bedrock): the bearer token rides the same
        # staging pipeline as the external provider keys.
        response = self.invoke({"BEDROCK_API_KEY": BEARER_TOKEN})
        self.assertEqual(response["status"], "ok")
        self.assertEqual(
            self.staged_text(),
            f"SCH_BEDROCK_API_KEY={BEARER_TOKEN}\n",
        )
        self.assertEqual(
            main._read_staged_provider_keys(),
            {"SCH_BEDROCK_API_KEY": BEARER_TOKEN},
        )

    def test_keys_from_the_payload_land_in_the_staging_file_with_the_sch_prefix(self):
        response = self.invoke({
            "ANTHROPIC_API_KEY": ANTHROPIC_KEY, "KILO_API_KEY": KILO_KEY,
        })
        self.assertEqual(response["status"], "ok")
        self.assertEqual(
            self.staged_text(),
            f"SCH_ANTHROPIC_API_KEY={ANTHROPIC_KEY}\nSCH_KILO_API_KEY={KILO_KEY}\n",
        )
        self.assertEqual(
            main._read_staged_provider_keys(),
            {"SCH_ANTHROPIC_API_KEY": ANTHROPIC_KEY, "SCH_KILO_API_KEY": KILO_KEY},
        )

    def test_staging_file_and_directory_are_owner_only(self):
        self.invoke({"KILO_API_KEY": KILO_KEY})
        self.assertEqual(stat.S_IMODE(self.staging_file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.staging_dir.stat().st_mode), 0o700)

    def test_a_later_invoke_replaces_the_whole_set(self):
        self.invoke({"ANTHROPIC_API_KEY": ANTHROPIC_KEY, "KILO_API_KEY": KILO_KEY})
        self.invoke({"OPENROUTER_API_KEY": OPENROUTER_KEY})
        # No merge, no leftovers: "ultimo invoke vince, sostituzione totale".
        self.assertEqual(
            self.staged_text(), f"SCH_OPENROUTER_API_KEY={OPENROUTER_KEY}\n"
        )
        self.assertNotIn(ANTHROPIC_KEY, self.staged_text())

    def test_an_invoke_without_the_field_clears_the_set(self):
        self.invoke({"ANTHROPIC_API_KEY": ANTHROPIC_KEY})
        self.assertTrue(self.staging_file.exists())
        self.invoke()
        self.assertFalse(self.staging_file.exists())
        self.assertEqual(main._read_staged_provider_keys(), {})

    def test_an_empty_key_set_clears_the_set(self):
        self.invoke({"ANTHROPIC_API_KEY": ANTHROPIC_KEY})
        self.invoke({})
        self.assertFalse(self.staging_file.exists())

    def test_allowlist_and_empty_values_are_enforced(self):
        self.invoke({
            "ANTHROPIC_API_KEY": ANTHROPIC_KEY,
            "AWS_SECRET_ACCESS_KEY": "must-not-be-staged",
            "SCH_TELEGRAM_BOT_TOKEN": "must-not-be-staged",
            "KILO_API_KEY": "",
            "OPENROUTER_API_KEY": "   ",
        })
        self.assertEqual(
            self.staged_text(), f"SCH_ANTHROPIC_API_KEY={ANTHROPIC_KEY}\n"
        )
        self.assertNotIn("must-not-be-staged", self.staged_text())

    def test_a_malformed_field_is_ignored_and_never_fails_the_invocation(self):
        for value in ("a string", 42, ["ANTHROPIC_API_KEY"], None):
            with self.subTest(value=value):
                response = self.invoke(action="noop", provider_keys=value)
                self.assertEqual(response["status"], "ok")
                self.assertFalse(self.staging_file.exists())

    def test_values_are_taken_verbatim_including_shell_metacharacters(self):
        hostile = "$(touch /tmp/nope); `id`; \"quoted\" 'single' \\ end"
        self.invoke({"KILO_API_KEY": hostile})
        self.assertEqual(main._read_staged_provider_keys()["SCH_KILO_API_KEY"], hostile)

    def test_no_temp_file_is_left_behind(self):
        self.invoke({"KILO_API_KEY": KILO_KEY})
        self.assertEqual(
            sorted(p.name for p in self.staging_dir.iterdir()), ["provider-keys.env"]
        )

    def test_a_rejected_invocation_does_not_touch_the_staged_set(self):
        main._SESSION_EPOCH["value"] = 7
        self.invoke({"ANTHROPIC_API_KEY": ANTHROPIC_KEY}, session_epoch=7)
        before = self.staged_text()
        # A mismatched session epoch is refused before anything is applied, so a
        # superseded client cannot rotate the live session's key set.
        response = self.invoke({"KILO_API_KEY": KILO_KEY}, session_epoch=99)
        self.assertEqual(response["status"], "rejected")
        self.assertEqual(self.staged_text(), before)

    def test_no_key_value_reaches_the_shim_log(self):
        with self.assertLogs(main.logger, level="INFO") as captured:
            self.invoke({
                "ANTHROPIC_API_KEY": ANTHROPIC_KEY, "OPENROUTER_API_KEY": OPENROUTER_KEY,
            })
        blob = "\n".join(captured.output)
        self.assertIn("ANTHROPIC_API_KEY", blob)  # names are fine...
        for value in (ANTHROPIC_KEY, OPENROUTER_KEY):  # ...values are not
            self.assertNotIn(value, blob)

    def test_info_reports_names_only(self):
        self.invoke({
            "ANTHROPIC_API_KEY": ANTHROPIC_KEY, "OPENROUTER_API_KEY": OPENROUTER_KEY,
        })
        info = self.invoke(
            {"ANTHROPIC_API_KEY": ANTHROPIC_KEY, "OPENROUTER_API_KEY": OPENROUTER_KEY},
            action="info",
        )
        self.assertEqual(
            info["provider_keys"]["staged"],
            ["SCH_ANTHROPIC_API_KEY", "SCH_OPENROUTER_API_KEY"],
        )
        blob = json.dumps(info)
        for value in (ANTHROPIC_KEY, OPENROUTER_KEY):
            self.assertNotIn(value, blob)

    def test_staging_failure_is_loud_but_never_fails_the_invocation(self):
        # e.g. a /run/sch the runtime user cannot write (the image creates it at
        # build time): the session degrades to Bedrock-only, and that must be
        # reported — a silent capability loss would be undiagnosable.
        with patch.object(main.os, "open", side_effect=OSError("read-only")), \
             self.assertLogs(main.logger, level="ERROR") as captured:
            response = self.invoke({"KILO_API_KEY": KILO_KEY})
        self.assertEqual(response["status"], "ok")
        self.assertFalse(self.staging_file.exists())
        blob = "\n".join(captured.output)
        self.assertIn("Bedrock only", blob)
        self.assertNotIn(KILO_KEY, blob)


class NoPersistenceTests(_ShimFixture):
    def test_the_staging_path_is_outside_every_checkpointed_root(self):
        # The default (the real one in the image) is under /run: not part of the
        # session mount nor of the s3-backed workspace root, so no L2 artifact and
        # no S3 object can ever contain a key.
        default = self._saved_staging
        self.assertEqual(str(default), "/run/sch/provider-keys.env")
        for checkpointed in (main.SESSION_WORKSPACE_ROOT, main.S3_WORKSPACE_ROOT):
            self.assertFalse(
                str(default).startswith(str(checkpointed)),
                f"{default} must not live under {checkpointed}",
            )

    def test_nothing_under_the_workspace_root_mentions_a_key(self):
        workspace = self.root / "workspace"
        (workspace / "state").mkdir(parents=True)
        (workspace / "repo").mkdir()
        with patch.object(main, "WORKSPACE_ROOT", workspace), \
             patch.object(main, "STATE_DIR", workspace / "state"):
            self.invoke({"ANTHROPIC_API_KEY": ANTHROPIC_KEY})
        for path in workspace.rglob("*"):
            if not path.is_file():
                continue
            self.assertNotIn(
                ANTHROPIC_KEY, path.read_text(errors="replace"), f"key found in {path}"
            )

    def test_the_key_is_not_kept_in_the_shim_state_surfaces(self):
        self.invoke({"ANTHROPIC_API_KEY": ANTHROPIC_KEY})
        blob = json.dumps({
            "boot": dict(main.BOOT_STATE),
            "task_status": dict(main._TASK_STATUS),
            "checkpoint": {k: str(v) for k, v in main.CHECKPOINT_STATE.items()},
        })
        self.assertNotIn(ANTHROPIC_KEY, blob)


class ChildProcessEnvTests(_ShimFixture):
    def test_child_env_carries_the_staged_set(self):
        self.invoke({"ANTHROPIC_API_KEY": ANTHROPIC_KEY})
        env = main._child_env_with_provider_keys()
        self.assertEqual(env["SCH_ANTHROPIC_API_KEY"], ANTHROPIC_KEY)

    def test_child_env_drops_a_key_the_user_removed(self):
        # A stale value in the shim's own environment (an old deployment, or a
        # previous session of the same microVM) must not survive: this is the
        # mechanism behind the claude Bedrock rollback.
        with patch.dict(os.environ, {"SCH_ANTHROPIC_API_KEY": "stale-value"}):
            self.invoke()
            env = main._child_env_with_provider_keys()
        self.assertNotIn("SCH_ANTHROPIC_API_KEY", env)

    def test_headless_task_spawns_with_the_keys(self):
        self.invoke({"ANTHROPIC_API_KEY": ANTHROPIC_KEY, "KILO_API_KEY": KILO_KEY})
        repo = self.root / "repo"
        repo.mkdir()
        ready = self.root / ".ready"
        ready.touch()
        recorded = {}

        class FakeProc:
            pid = 4242
            returncode = 0

            def communicate(self, timeout=None):
                return "done", ""

            def poll(self):
                return 0

        def fake_popen(argv, **kwargs):
            recorded["env"] = kwargs["env"]
            return FakeProc()

        with patch.object(main, "REPO_DIR", repo), \
             patch.object(main, "_harness_ready_marker", return_value=ready), \
             patch.object(main.subprocess, "Popen", side_effect=fake_popen), \
             patch.object(main, "_finish_task"), \
             patch.object(main, "_build_headless_argv", return_value=["/bin/true"]):
            main._run_task(
                task_id="t1", prompt="hi", timeout_s=5, continue_session=False,
                session_id_hint=None, workspace="workspace", harness="opencode",
            )
        self.assertEqual(recorded["env"]["SCH_ANTHROPIC_API_KEY"], ANTHROPIC_KEY)
        self.assertEqual(recorded["env"]["SCH_KILO_API_KEY"], KILO_KEY)
        self.assertEqual(recorded["env"]["SCH_HARNESS"], "opencode")

    def test_serve_supervisor_spawns_with_the_keys(self):
        self.invoke({"OPENROUTER_API_KEY": OPENROUTER_KEY})
        repo = self.root / "repo"
        repo.mkdir()
        ready = self.root / "serve-ready"
        ready.touch()
        recorded = {}

        class StopLoop(Exception):
            pass

        def fake_popen(argv, **kwargs):
            recorded["env"] = kwargs["env"]
            proc = types.SimpleNamespace(pid=1, returncode=None)
            proc.poll = lambda: None
            return proc

        with patch.object(main, "REPO_DIR", repo), \
             patch.object(main, "READY_MARKER", ready), \
             patch.object(main.subprocess, "Popen", side_effect=fake_popen), \
             patch.object(main.time, "sleep", side_effect=StopLoop):
            with self.assertRaises(StopLoop):
                main._serve_supervisor_loop()
        self.assertEqual(recorded["env"]["SCH_OPENROUTER_API_KEY"], OPENROUTER_KEY)
        self.assertEqual(recorded["env"]["SCH_HARNESS"], "opencode")
        main._SERVE_STATE["proc"] = None

    def test_workspace_seeding_script_sees_the_keys(self):
        self.invoke({"ANTHROPIC_API_KEY": ANTHROPIC_KEY})
        dump = self.root / "init-env.json"
        dumper = self.root / "dump-env.py"
        dumper.write_text(
            "import json, os\n"
            f"json.dump(dict(os.environ), open({str(dump)!r}, 'w'))\n"
        )
        script = self.root / "fake-init.sh"
        script.write_text(f"#!/bin/bash\nexec python3 {dumper}\n")
        with patch.object(main, "INIT_SCRIPT", script):
            result = main._run_init_workspace()
        self.assertEqual(result["status"], "ok")
        env = json.loads(dump.read_text())
        self.assertEqual(env["SCH_ANTHROPIC_API_KEY"], ANTHROPIC_KEY)


class ClaudeReconciliationTests(_ShimFixture):
    """Design D6: recomputed at every bootstrap from the session's own keys."""

    def setUp(self):
        super().setUp()
        self.workspace = self.root / "workspace"
        self.claude_config = self.root / "claude"

    def bootstrap(self, key=None):
        """Run the REAL seeding script the way _bootstrap does, with the keys
        staged by the current session (or none at all)."""
        self.invoke({"ANTHROPIC_API_KEY": key} if key else None)
        env = {
            "SCH_HARNESS": "claude",
            "SCH_WORKSPACE_ROOT": str(self.workspace),
            "XDG_DATA_HOME": str(self.workspace / "state" / "data"),
            "XDG_CONFIG_HOME": str(self.workspace / "state" / "config"),
            "CLAUDE_CONFIG_DIR": str(self.claude_config),
            "SCH_CLAUDE_TEMPLATE_DIR": str(CLAUDE_TEMPLATES),
        }
        with patch.object(main, "INIT_SCRIPT", INIT_SCRIPT), \
             patch.dict(os.environ, env):
            result = main._run_init_workspace()
        self.assertEqual(result["status"], "ok", result)
        return result

    @property
    def global_config(self) -> Path:
        return self.claude_config / ".claude.json"

    @property
    def marker(self) -> Path:
        return self.claude_config / ".sch-approved-api-key"

    def settings(self) -> dict:
        return json.loads((self.claude_config / "settings.json").read_text())

    def approved(self) -> list:
        if not self.global_config.exists():
            return []
        data = json.loads(self.global_config.read_text())
        return data.get("customApiKeyResponses", {}).get("approved", [])

    def test_key_present_switches_to_the_api_and_approves_the_suffix(self):
        self.bootstrap(key=ANTHROPIC_KEY)
        self.assertEqual(self.settings()["env"]["CLAUDE_CODE_USE_BEDROCK"], "0")
        self.assertEqual(self.approved(), [ANTHROPIC_SUFFIX])
        # Only the suffix, never the key — not even in the sidecar marker.
        self.assertNotIn(ANTHROPIC_KEY, self.global_config.read_text())
        self.assertEqual(self.marker.read_text().strip(), ANTHROPIC_SUFFIX)
        self.assertEqual(stat.S_IMODE(self.marker.stat().st_mode), 0o600)

    def test_no_key_keeps_bedrock_and_writes_nothing(self):
        self.bootstrap()
        self.assertEqual(self.settings()["env"]["CLAUDE_CODE_USE_BEDROCK"], "1")
        self.assertFalse(self.global_config.exists())
        self.assertFalse(self.marker.exists())

    def test_a_checkpointed_workspace_restarting_without_the_key_returns_to_bedrock(self):
        # The exact window design D6 closes: settings.json is checkpointed, so a
        # workspace switched to the API keeps saying "0" — a session started
        # without the key would sit on an API it has no credential for.
        self.bootstrap(key=ANTHROPIC_KEY)
        self.assertEqual(self.settings()["env"]["CLAUDE_CODE_USE_BEDROCK"], "0")
        self.assertEqual(self.approved(), [ANTHROPIC_SUFFIX])

        self.bootstrap()

        self.assertEqual(self.settings()["env"]["CLAUDE_CODE_USE_BEDROCK"], "1")
        self.assertEqual(self.approved(), [])
        self.assertFalse(self.marker.exists())

    def test_the_key_can_come_back(self):
        self.bootstrap(key=ANTHROPIC_KEY)
        self.bootstrap()
        self.bootstrap(key=ANTHROPIC_KEY)
        self.assertEqual(self.settings()["env"]["CLAUDE_CODE_USE_BEDROCK"], "0")
        self.assertEqual(self.approved(), [ANTHROPIC_SUFFIX])

    def test_a_rotated_key_replaces_the_previous_approval(self):
        self.bootstrap(key=ANTHROPIC_KEY)
        self.bootstrap(key=OTHER_ANTHROPIC_KEY)
        self.assertEqual(self.approved(), [OTHER_SUFFIX])
        self.assertEqual(self.marker.read_text().strip(), OTHER_SUFFIX)

    def test_an_approval_the_operator_added_is_never_withdrawn(self):
        self.bootstrap(key=ANTHROPIC_KEY)
        data = json.loads(self.global_config.read_text())
        data["customApiKeyResponses"]["approved"].insert(0, "operatorownapprovalxx")
        data["numStartups"] = 9
        self.global_config.write_text(json.dumps(data))

        self.bootstrap()

        self.assertEqual(self.approved(), ["operatorownapprovalxx"])
        self.assertEqual(json.loads(self.global_config.read_text())["numStartups"], 9)

    def test_reconciliation_is_idempotent(self):
        self.bootstrap(key=ANTHROPIC_KEY)
        first = self.global_config.read_text()
        self.bootstrap(key=ANTHROPIC_KEY)
        self.assertEqual(self.global_config.read_text(), first)
        self.bootstrap()
        withdrawn = self.global_config.read_text()
        self.bootstrap()
        self.assertEqual(self.global_config.read_text(), withdrawn)

    def test_an_operator_chosen_setting_value_is_preserved(self):
        self.claude_config.mkdir(parents=True)
        (self.claude_config / "settings.json").write_text(json.dumps({
            "env": {"CLAUDE_CODE_USE_BEDROCK": "true", "FOO": "bar"},
        }))

        self.bootstrap(key=ANTHROPIC_KEY)

        settings = self.settings()
        self.assertEqual(settings["env"]["CLAUDE_CODE_USE_BEDROCK"], "true")
        self.assertEqual(settings["env"]["FOO"], "bar")

    def test_no_key_value_reaches_the_seeding_output(self):
        for key in (ANTHROPIC_KEY, None):
            with self.subTest(key=bool(key)):
                with self.assertLogs(main.logger, level="INFO") as captured:
                    self.bootstrap(key=key)
                blob = "\n".join(captured.output)
                self.assertNotIn(ANTHROPIC_KEY, blob)
                self.assertNotIn(ANTHROPIC_SUFFIX, blob)


if __name__ == "__main__":
    logging.disable(logging.NOTSET)
    unittest.main()
