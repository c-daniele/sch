"""Target resolution for the live verify scripts (cli/sch/verify_support.py,
TASK-20.5).

The helper is what lets ``bin/verify-*.sh`` address a workspace like ``sch``
on registry and isolation stacks: the plane runtime (never the shared one),
the registry session and identity, owner-segment keys, and reads through the
access role.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_CLI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _CLI_DIR not in sys.path:
    sys.path.insert(0, _CLI_DIR)

from sch import harness, plane as plane_mod, verify_support, workspace_registry

ACCOUNT = "111122223333"
KEY = "0123456789abcdef"
PREFIX = "o." + KEY
RUNTIME_ARN = "arn:aws:bedrock-agentcore:eu-west-1:{}:runtime/sch_dev_o_{}-AbCdEf1234".format(ACCOUNT, KEY)
ROLE_ARN = "arn:aws:iam::{}:role/sch-dev-o-{}-access".format(ACCOUNT, KEY)
SHARED_ARN = "arn:aws:bedrock-agentcore:eu-west-1:{}:runtime/sch_dev_shared-AbCdEf1234".format(ACCOUNT)
URL = "https://example.execute-api.eu-west-1.amazonaws.com/v1"
PLANE = {"runtimeArn": RUNTIME_ARN, "accessRoleArn": ROLE_ARN, "ownerPrefix": PREFIX}


def _cfg(tmp, registry=True):
    base = Path(tmp)
    return SimpleNamespace(
        region="eu-west-1", project="sch", env="dev", default_harness="opencode",
        default_storage="s3", workspace_registry_url=URL if registry else "",
        ws_dir=base / "workspaces", config_dir=base,
        runtime_arn_cache=base / "runtime-arn", checkpoint_bucket_cache=base / "checkpoint-bucket",
        runtime_arn_override=SHARED_ARN, checkpoint_bucket_override="bucket",
        acp_mirror_root=base / "mirrors", provider_keys={},
        stack_name=lambda: "sch-dev-runtime", isolation=None, plane=None,
    )


def _record(name="ws"):
    return {"logicalWorkspace": name, "runtimeSessionId": "sch-registry-sid-1",
            "harness": "opencode", "workspaceIdentity": "ws-" + name,
            "storage": "s3", "sessionEpoch": 2}


class _Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class ProbeTests(unittest.TestCase):
    def test_registry_off_non_isolated_stack(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp, registry=False)
            calls = []

            def run(argv, **kwargs):
                calls.append(argv)
                return _Result(0, "false\n")

            self.assertEqual(verify_support.probe(cfg, run),
                             {"registry": False, "isolation": False, "plane": None})
            self.assertIn("IsolationStatus", " ".join(calls[0]))

    def test_registry_off_on_isolated_stack_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp, registry=False)
            with self.assertRaises(verify_support.SupportError) as ctx:
                verify_support.probe(cfg, lambda argv, **kw: _Result(0, "true\n"))
            self.assertEqual(ctx.exception.code, verify_support.EXIT_REGISTRY_REQUIRED)
            self.assertIn("SCH_WORKSPACE_REGISTRY_URL", str(ctx.exception))

    def test_unreadable_stack_is_treated_as_not_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp, registry=False)
            out = verify_support.probe(cfg, lambda argv, **kw: _Result(255, "", "AccessDenied"))
            self.assertFalse(out["isolation"])

    def test_isolated_registry_returns_the_plane(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            body = {"workspaces": [], "isolation": True, "plane": dict(PLANE)}
            with patch.object(workspace_registry, "_request", return_value=body):
                out = verify_support.probe(cfg)
            self.assertEqual(out["registry"], True)
            self.assertEqual(out["isolation"], True)
            self.assertEqual(out["plane"]["runtimeArn"], RUNTIME_ARN)
            self.assertEqual(out["plane"]["ownerPrefix"], PREFIX)
            self.assertEqual(out["plane"]["ownerKey"], KEY)

    def test_registry_refusal_exits_with_its_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            message = ("workspace registry rejected request: caller arn:aws:iam::111122223333:user/carol "
                       "is not an isolated principal of this deployment; add user:carol to "
                       "ISOLATED_PRINCIPALS and redeploy")
            with patch.object(workspace_registry, "_request", side_effect=RuntimeError(message)):
                with self.assertRaises(verify_support.SupportError) as ctx:
                    verify_support.probe(cfg)
            self.assertEqual(ctx.exception.code, verify_support.EXIT_REGISTRY_REFUSED)
            self.assertIn("add user:carol to ISOLATED_PRINCIPALS", str(ctx.exception))

    def test_malformed_plane_is_a_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            body = {"workspaces": [], "isolation": True,
                    "plane": dict(PLANE, runtimeArn=SHARED_ARN)}
            with patch.object(workspace_registry, "_request", return_value=body):
                with self.assertRaises(verify_support.SupportError):
                    verify_support.probe(cfg)


class WorkspaceTests(unittest.TestCase):
    def test_isolated_workspace_addressing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            body = {"workspace": dict(_record(), plane=dict(PLANE)), "created": True,
                    "isolation": True, "plane": dict(PLANE)}
            with patch.object(workspace_registry, "_request", return_value=body) as request:
                out = verify_support.describe_workspace(cfg, "ws", "opencode", "s3")
            self.assertEqual(request.call_args[0][1:3], ("POST", "/workspaces/ws/resolve"))
            self.assertEqual(out["runtimeArn"], RUNTIME_ARN)  # never the shared ARN
            self.assertEqual(out["sessionId"], "sch-registry-sid-1")
            self.assertEqual(out["runtimeWorkspace"], "ws-ws")
            self.assertEqual(out["sessionEpoch"], 2)
            self.assertEqual(out["checkpointPrefix"], "checkpoints/{}/ws-ws/".format(PREFIX))
            self.assertEqual(out["writerKey"], "workspace-writers/{}/ws-ws.json".format(PREFIX))
            self.assertTrue(out["isolation"])
            self.assertTrue(out["created"])
            # sch mirrors the registry record into the local index.
            index = json.loads((cfg.ws_dir / "ws").read_text())
            self.assertEqual(index["runtimeSessionId"], "sch-registry-sid-1")
            self.assertEqual(index["workspaceIdentity"], "ws-ws")

    def test_registry_without_isolation_uses_the_flat_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            body = {"workspace": _record(), "created": False}
            with patch.object(workspace_registry, "_request", return_value=body):
                out = verify_support.describe_workspace(cfg, "ws")
            self.assertFalse(out["isolation"])
            self.assertEqual(out["runtimeArn"], SHARED_ARN)
            self.assertEqual(out["checkpointPrefix"], "checkpoints/ws-ws/")
            self.assertEqual(out["writerKey"], "workspace-writers/ws-ws.json")
            self.assertEqual(out["ownerPrefix"], "")

    def test_registry_off_is_a_usage_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp, registry=False)
            with self.assertRaises(verify_support.SupportError) as ctx:
                verify_support.describe_workspace(cfg, "ws")
            self.assertEqual(ctx.exception.code, verify_support.EXIT_USAGE)


class OwnerExecTests(unittest.TestCase):
    def tearDown(self):
        plane_mod.reset_sessions()

    def test_isolation_off_runs_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp, registry=False)
            seen = {}

            def run(argv, **kwargs):
                seen["argv"], seen["kwargs"] = argv, kwargs
                return _Result(3)

            self.assertEqual(verify_support.owner_exec(cfg, ["aws", "s3api", "x"], run), 3)
            self.assertEqual(seen, {"argv": ["aws", "s3api", "x"], "kwargs": {}})

    def test_isolation_on_passes_access_role_credentials_in_the_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            cfg.isolation = True
            cfg.plane = plane_mod.parse_plane(cfg, PLANE)
            creds = {"Credentials": {"AccessKeyId": "ASIAEXAMPLE", "SecretAccessKey": "secret",
                                     "SessionToken": "token",
                                     "Expiration": "2099-01-01T00:00:00Z"}}
            seen = {}

            def assume(argv, **kwargs):
                seen["assume"] = argv
                return _Result(0, json.dumps(creds))

            def run(argv, **kwargs):
                seen["argv"], seen["env"] = argv, kwargs["env"]
                return _Result(0)

            with patch.dict(os.environ, {"AWS_PROFILE": "alice"}), \
                    patch.object(plane_mod.subprocess, "run", side_effect=assume):
                rc = verify_support.owner_exec(cfg, ["aws", "s3api", "head-object"], run)
            self.assertEqual(rc, 0)
            self.assertIn(ROLE_ARN, seen["assume"])
            self.assertEqual(seen["env"]["AWS_ACCESS_KEY_ID"], "ASIAEXAMPLE")
            self.assertNotIn("AWS_PROFILE", seen["env"])
            self.assertNotIn("secret", " ".join(seen["argv"]))

    def test_missing_command_is_a_usage_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(verify_support.SupportError):
                verify_support.owner_exec(_cfg(tmp, registry=False), [])


class MainTests(unittest.TestCase):
    def _main(self, argv, cfg):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = verify_support.main(argv, cfg_factory=lambda: cfg)
        return rc, out.getvalue(), err.getvalue()

    def test_probe_prints_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            body = {"workspaces": [], "isolation": True, "plane": dict(PLANE)}
            with patch.object(workspace_registry, "_request", return_value=body):
                rc, out, _ = self._main(["probe"], cfg)
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(out)["plane"]["accessRoleArn"], ROLE_ARN)

    def test_refusal_goes_to_stderr_with_exit_4(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(workspace_registry, "_request", side_effect=RuntimeError("denied")):
                rc, out, err = self._main(["probe"], _cfg(tmp))
            self.assertEqual((rc, out), (4, ""))
            self.assertIn("denied", err)

    def test_workspace_argument_parsing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            with patch.object(verify_support, "describe_workspace", return_value={"ok": 1}) as describe:
                rc, out, _ = self._main(["workspace", "ws", "--storage", "session", "--harness", "pi"], cfg)
            self.assertEqual(rc, 0)
            describe.assert_called_once_with(cfg, "ws", "pi", "session")
            for bad in (["workspace"], ["workspace", "--harness"], ["workspace", "ws", "--x", "1"],
                        ["workspace", "ws", "--harness"]):
                self.assertEqual(self._main(bad, cfg)[0], verify_support.EXIT_USAGE, bad)

    def test_unknown_subcommand(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._main(["nope"], _cfg(tmp))[0], verify_support.EXIT_USAGE)
            self.assertEqual(self._main([], _cfg(tmp))[0], verify_support.EXIT_USAGE)

    def test_dashboard_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            row = SimpleNamespace(name="ws", harness="opencode", storage="s3",
                                  task_state="succeeded", manifest_age_s=4.0, error="")
            snapshot = SimpleNamespace(workspaces=(row,))
            with patch.object(verify_support.dashboard_mod, "aggregate_snapshot", return_value=snapshot):
                rc, out, _ = self._main(["dashboard"], cfg)
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(out)[0]["taskState"], "succeeded")


class ScriptEntryTests(unittest.TestCase):
    def test_runs_by_path_without_package_context(self):
        import subprocess
        env = {k: v for k, v in os.environ.items() if not k.startswith("SCH_")}
        result = subprocess.run(
            [sys.executable, os.path.join(_CLI_DIR, "sch", "verify_support.py")],
            capture_output=True, text=True, env=env,
        )
        self.assertEqual(result.returncode, verify_support.EXIT_USAGE)
        self.assertIn("probe", result.stderr)


if __name__ == "__main__":
    unittest.main()
