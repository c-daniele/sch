"""CLI behavior with per-principal isolation (spec: per-principal-isolation
R40-R43).

Every AWS CLI request the new code builds is converted back to API
parameters and checked with botocore's ``ParamValidator`` (botocore ships
with the dev extra; those checks are skipped without it).
"""

import contextlib
import datetime
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_CLI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _CLI_DIR not in sys.path:
    sys.path.insert(0, _CLI_DIR)

from sch import config as config_mod
from sch import dashboard, harness, plane as plane_mod, runtime, workspace, workspace_registry
from sch.commands import info as info_cmd
from sch.commands import list as list_cmd
from sch.commands import status as status_cmd

try:
    import botocore.session
    from botocore import xform_name
    from botocore.validate import ParamValidator
except ImportError:  # pragma: no cover - the dev extra is not installed
    botocore = None

ACCOUNT = "111122223333"
KEY = "0123456789abcdef"
OTHER = "fedcba9876543210"
PREFIX = "o." + KEY
RUNTIME_ARN = "arn:aws:bedrock-agentcore:eu-west-1:{}:runtime/sch_dev_o_{}-AbCdEf1234".format(ACCOUNT, KEY)
ROLE_ARN = "arn:aws:iam::{}:role/sch-dev-o-{}-access".format(ACCOUNT, KEY)
SHARED_ARN = "arn:aws:bedrock-agentcore:eu-west-1:{}:runtime/sch_dev_shared-AbCdEf1234".format(ACCOUNT)
PLANE = {"runtimeArn": RUNTIME_ARN, "accessRoleArn": ROLE_ARN, "ownerPrefix": PREFIX}
URL = "https://example.execute-api.eu-west-1.amazonaws.com/v1"


def _record(name="ws", sid="sch-registry-00000000-0000-0000-0000-000000000001", plane=True):
    record = {"logicalWorkspace": name, "runtimeSessionId": sid, "harness": "opencode",
              "workspaceIdentity": "ws-" + name, "storage": "s3", "sessionEpoch": 1}
    if plane:
        record["plane"] = dict(PLANE)
    return record


def _isolated(body):
    return dict(body, isolation=True, plane=dict(PLANE))


def _cfg(tmp, registry=True):
    base = Path(tmp)
    return SimpleNamespace(
        region="eu-west-1", project="sch", env="dev", default_harness="opencode",
        default_storage="s3", workspace_registry_url=URL if registry else "",
        ws_dir=base / "workspaces", config_dir=base,
        runtime_arn_override="", checkpoint_bucket_override="sch-dev-checkpoints-" + ACCOUNT,
        acp_mirror_root=base / "mirrors", provider_keys={},
        stack_name=lambda: "sch-dev-runtime",
    )


# --- botocore validation of AWS CLI argv ------------------------------------

_MODELS = {}
if botocore is not None:
    _SESSION = botocore.session.Session()
    for _name in ("s3", "sts", "bedrock-agentcore", "bedrock-agentcore-control"):
        _MODELS[_name] = _SESSION.get_service_model(_name)

EXPIRY_TEXT = "2026-09-27T13:00:00+00:00"
EXPIRY = datetime.datetime(2026, 9, 27, 13, 0, tzinfo=datetime.timezone.utc).timestamp()

_GLOBAL_WITH_VALUE = {"--region", "--output", "--query", "--cli-binary-format",
                      "--cli-read-timeout", "--profile"}
_SERVICES = {"s3api": "s3"}


def _positional(argv):
    """The positional outfile of an ``aws`` argv, or None."""
    i = 3
    while i < len(argv):
        if argv[i].startswith("--"):
            i += 2
            continue
        return argv[i]
    return None


def validate_cli(testcase, argv):
    """Map an ``aws <service> <command> --flag value ... [outfile]`` argv
    to API parameters and validate them against the service model."""
    if botocore is None:  # pragma: no cover
        return
    testcase.assertEqual(argv[0], "aws")
    service = _SERVICES.get(argv[1], argv[1])
    model = _MODELS[service]
    operations = {xform_name(op, "-"): op for op in model.operation_names}
    testcase.assertIn(argv[2], operations, argv)
    operation = model.operation_model(operations[argv[2]])
    shape = operation.input_shape
    members = {xform_name(m, "-"): m for m in shape.members} if shape else {}
    params = {}
    rest = list(argv[3:])
    i = 0
    while i < len(rest):
        flag = rest[i]
        if flag in _GLOBAL_WITH_VALUE:
            i += 2
            continue
        if not flag.startswith("--"):
            i += 1  # positional outfile
            continue
        name = flag[2:]
        testcase.assertIn(name, members, argv)
        member = members[name]
        value = rest[i + 1]
        if shape.members[member].type_name == "integer":
            value = int(value)
        params[member] = value
        i += 2
    report = ParamValidator().validate(params, shape)
    testcase.assertFalse(report.has_errors(), (argv, report.generate_report()))
    return operation.name


class FakeAws:
    """Records ``subprocess.run`` calls and answers like the AWS CLI."""

    def __init__(self, testcase, expiry=EXPIRY_TEXT, status=None):
        self.testcase = testcase
        self.calls = []
        self.expiry = expiry
        self.status = status if status is not None else {"state": "succeeded", "exit_code": 0}
        self.assumed = 0

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        validate_cli(self.testcase, argv)
        command = argv[1:3]
        out = ""
        if command == ["sts", "assume-role"]:
            self.assumed += 1
            out = json.dumps({"Credentials": {
                "AccessKeyId": "ASIAEXAMPLE{:04d}".format(self.assumed),
                "SecretAccessKey": "secret", "SessionToken": "token{}".format(self.assumed),
                "Expiration": self.expiry() if callable(self.expiry) else self.expiry}})
        elif command == ["s3api", "get-object"]:
            key = argv[argv.index("--key") + 1]
            with open(_positional(argv), "w", encoding="utf-8") as fh:
                json.dump({"session_id": "sid", "session_epoch": 1}
                          if key.startswith("workspace-writers/") else self.status, fh)
        elif command == ["s3api", "head-object"]:
            out = "2026-09-27T11:59:00+00:00\n"
        elif command == ["s3api", "list-objects-v2"]:
            prefix = argv[argv.index("--prefix") + 1]
            if "--delimiter" in argv:
                out = json.dumps({"CommonPrefixes": [
                    {"Prefix": prefix + "ws-a/"}, {"Prefix": prefix + "ws-gone/"}]})
            else:
                out = json.dumps({"Contents": [{"Key": prefix + "ws-a.json"}]})
        elif command == ["bedrock-agentcore", "invoke-agent-runtime"]:
            with open(_positional(argv), "w", encoding="utf-8") as fh:
                json.dump({"status": "ok", "storage": "s3"}, fh)
        elif command == ["bedrock-agentcore-control", "get-agent-runtime"]:
            out = "7\n"
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    def argvs(self, *command):
        return [argv for argv, _ in self.calls if argv[1:1 + len(command)] == list(command)]


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.cfg = _cfg(self.tmp)
        plane_mod.reset_sessions()
        self.addCleanup(plane_mod.reset_sessions)

    def adopt(self):
        plane_mod.adopt(self.cfg, _isolated({}))


# --- R41 ---------------------------------------------------------------------

class PlaneValidationTests(Base):
    def test_a_valid_plane_is_accepted(self):
        plane = plane_mod.parse_plane(self.cfg, PLANE)
        self.assertEqual((plane.runtime_arn, plane.access_role_arn, plane.owner_prefix, plane.owner_key),
                         (RUNTIME_ARN, ROLE_ARN, PREFIX, KEY))

    def test_malformed_planes_are_rejected(self):
        bad = [
            None, [], {},
            dict(PLANE, ownerPrefix="o.XYZ"),
            dict(PLANE, ownerPrefix="o." + OTHER),
            dict(PLANE, runtimeArn=SHARED_ARN),
            dict(PLANE, runtimeArn=RUNTIME_ARN.replace("sch_dev", "sch_prd")),
            dict(PLANE, runtimeArn="arn:aws:lambda:eu-west-1:{}:function:x".format(ACCOUNT)),
            dict(PLANE, accessRoleArn=ROLE_ARN.replace(KEY, OTHER)),
            dict(PLANE, accessRoleArn="arn:aws:iam::{}:user/sch-dev-o-{}-access".format(ACCOUNT, KEY)),
            dict(PLANE, accessRoleArn=1),
        ]
        for value in bad:
            with self.assertRaises(ValueError, msg=repr(value)):
                plane_mod.parse_plane(self.cfg, value)

    def test_isolation_without_plane_is_rejected(self):
        with self.assertRaises(ValueError):
            plane_mod.adopt(self.cfg, {"isolation": True, "workspaces": []})
        with self.assertRaises(ValueError):
            plane_mod.adopt(self.cfg, {"isolation": "yes", "plane": PLANE})
        with self.assertRaises(ValueError):
            plane_mod.adopt(self.cfg, {"plane": PLANE})


# --- registry client ------------------------------------------------------------

class RegistryClientTests(Base):
    def test_resolve_adopts_the_plane_of_the_response(self):
        body = _isolated({"workspace": _record(), "created": True})
        with patch.object(workspace_registry, "_request", return_value=body):
            record = workspace_registry.resolve(self.cfg, "ws")
        self.assertEqual(record.plane, plane_mod.parse_plane(self.cfg, PLANE))
        self.assertEqual(self.cfg.plane, record.plane)
        self.assertTrue(self.cfg.isolation)

    def test_record_without_plane_or_on_another_plane_fails(self):
        for record in (_record(plane=False),
                       dict(_record(), plane=dict(PLANE, ownerPrefix="o." + OTHER))):
            cfg = _cfg(self.tmp)
            with patch.object(workspace_registry, "_request",
                              return_value=_isolated({"workspace": record})):
                with self.assertRaises(ValueError):
                    workspace_registry.resolve(cfg, "ws")

    def test_list_rotate_delete_and_bulk_delete_adopt_the_plane(self):
        calls = [
            (workspace_registry.list_workspaces, (), _isolated({"workspaces": [_record()]})),
            (workspace_registry.rotate, ("ws",), _isolated({"workspace": _record()})),
            (workspace_registry.delete, ("ws",), _isolated({"workspace": _record(), "deleted": True})),
            (workspace_registry.delete_all, (), _isolated({"results": [], "deleted": 0, "failed": 0})),
        ]
        for function, args, body in calls:
            cfg = _cfg(self.tmp)
            with patch.object(workspace_registry, "_request", return_value=body):
                function(cfg, *args)
            self.assertEqual(cfg.plane.runtime_arn, RUNTIME_ARN, function.__name__)

    def test_isolation_off_responses_leave_no_plane(self):
        with patch.object(workspace_registry, "_request",
                          return_value={"workspace": _record(plane=False), "created": False}):
            record = workspace_registry.resolve(self.cfg, "ws")
        self.assertIsNone(record.plane)
        self.assertIsNone(self.cfg.plane)
        self.assertFalse(self.cfg.isolation)

    def test_resolve_harness_carries_the_plane_into_the_command(self):
        body = _isolated({"workspace": _record(), "created": False})
        with patch.object(workspace_registry, "_request", return_value=body):
            harness.resolve_harness(self.cfg, "ws", "")
        self.assertEqual(self.cfg.plane.owner_prefix, PREFIX)


# --- R40: runtime selection ------------------------------------------------------

class RuntimeSelectionTests(Base):
    def test_plane_runtime_wins_over_override_cache_and_stack(self):
        self.cfg.runtime_arn_override = SHARED_ARN
        cached = config_mod.stack_outputs_dir(self.cfg, ACCOUNT, "eu-west-1", "sch-dev-runtime")
        cached.mkdir(parents=True)
        (cached / "runtime-arn").write_text(SHARED_ARN + "\n")
        self.adopt()
        with patch("subprocess.run", side_effect=AssertionError("no AWS call expected")):
            self.assertEqual(config_mod.runtime_arn(self.cfg), RUNTIME_ARN)
        self.assertEqual((cached / "runtime-arn").read_text(), SHARED_ARN + "\n")

    def test_unknown_isolation_asks_the_registry_before_any_fallback(self):
        self.cfg.runtime_arn_override = SHARED_ARN
        with patch.object(workspace_registry, "_request",
                          return_value=_isolated({"workspaces": []})) as request:
            self.assertEqual(config_mod.runtime_arn(self.cfg), RUNTIME_ARN)
        request.assert_called_once_with(self.cfg, "GET", "/workspaces")

    def test_registry_failure_during_discovery_is_fatal(self):
        self.cfg.runtime_arn_override = SHARED_ARN
        with patch.object(workspace_registry, "_request", side_effect=RuntimeError("down")):
            with self.assertRaises(SystemExit):
                config_mod.runtime_arn(self.cfg)

    def test_registry_without_isolation_keeps_the_shared_runtime(self):
        self.cfg.runtime_arn_override = SHARED_ARN
        with patch.object(workspace_registry, "_request", return_value={"workspaces": []}):
            self.assertEqual(config_mod.runtime_arn(self.cfg), SHARED_ARN)

    def test_registry_off_never_calls_the_registry(self):
        cfg = _cfg(self.tmp, registry=False)
        cfg.runtime_arn_override = SHARED_ARN
        with patch.object(workspace_registry, "_request", side_effect=AssertionError("no registry")):
            self.assertEqual(config_mod.runtime_arn(cfg), SHARED_ARN)

    def test_invoke_uses_the_plane_runtime_and_sends_the_owner_prefix(self):
        self.adopt()
        fake = FakeAws(self)
        payload = runtime.payload_noop("ws-a", "opencode", "resumed", "s3", 1)
        with patch("subprocess.run", fake):
            self.assertTrue(runtime.invoke_verified(self.cfg, "s" * 40, payload, "warmup").ok)
            runtime.invoke_best_effort(self.cfg, "s" * 40, payload)
        argvs = fake.argvs("bedrock-agentcore", "invoke-agent-runtime")
        self.assertEqual(len(argvs), 2)
        for argv in argvs:
            self.assertEqual(argv[argv.index("--agent-runtime-arn") + 1], RUNTIME_ARN)
            sent = json.loads(argv[argv.index("--payload") + 1])
            self.assertEqual(sent["owner_prefix"], PREFIX)
            self.assertEqual({k: v for k, v in sent.items() if k != "owner_prefix"}, json.loads(payload))

    def test_isolation_off_payload_is_unchanged_byte_for_byte(self):
        cfg = _cfg(self.tmp, registry=False)
        cfg.runtime_arn_override = SHARED_ARN
        fake = FakeAws(self)
        payload = runtime.payload_noop("ws", "opencode", "resumed", "s3", 1)
        with patch("subprocess.run", fake):
            runtime.invoke_verified(cfg, "s" * 40, payload, "warmup")
        argv = fake.argvs("bedrock-agentcore", "invoke-agent-runtime")[0]
        self.assertEqual(argv[argv.index("--payload") + 1], payload)
        self.assertEqual(argv[argv.index("--agent-runtime-arn") + 1], SHARED_ARN)

    def test_runtime_version_pin_reads_the_plane_runtime(self):
        self.adopt()
        fake = FakeAws(self)
        with patch("subprocess.run", fake):
            self.assertEqual(config_mod.deployed_runtime_version(self.cfg), "7")
        argv = fake.argvs("bedrock-agentcore-control", "get-agent-runtime")[0]
        self.assertEqual(argv[argv.index("--agent-runtime-id") + 1], "sch_dev_o_{}-AbCdEf1234".format(KEY))

    def test_info_names_the_plane_only_with_isolation(self):
        self.adopt()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch("sch.userenv.user_env_path", return_value="~/.sch/env"):
            info_cmd.cmd_info(SimpleNamespace(**dict(vars(self.cfg), provider_keys={})), [])
        self.assertIn("runtime ARN : " + RUNTIME_ARN, out.getvalue())
        self.assertIn("isolation   : on (owner prefix {}".format(PREFIX), out.getvalue())


# --- R40: access-role reads --------------------------------------------------------

class AccessRoleTests(Base):
    def test_credentials_are_cached_and_refreshed_before_expiry(self):
        self.adopt()
        fake = FakeAws(self, expiry="2026-09-27T13:00:00Z")
        expiry = EXPIRY
        with patch("subprocess.run", fake):
            first = plane_mod.access_env(self.cfg, self.cfg.plane, clock=lambda: expiry - 3600)
            again = plane_mod.access_env(self.cfg, self.cfg.plane, clock=lambda: expiry - 301)
            self.assertEqual(fake.assumed, 1)
            renewed = plane_mod.access_env(self.cfg, self.cfg.plane, clock=lambda: expiry - 299)
        self.assertEqual(fake.assumed, 2)
        self.assertEqual(first, again)
        self.assertNotEqual(first["AWS_SESSION_TOKEN"], renewed["AWS_SESSION_TOKEN"])
        argv = fake.argvs("sts", "assume-role")[0]
        self.assertEqual(argv[argv.index("--role-arn") + 1], ROLE_ARN)
        self.assertEqual(argv[argv.index("--duration-seconds") + 1], "3600")

    def test_access_env_drops_profile_and_other_credentials(self):
        self.adopt()
        fake = FakeAws(self)
        with patch.dict(os.environ, {"AWS_PROFILE": "admin", "AWS_ACCESS_KEY_ID": "AKIAUSER",
                                     "AWS_SESSION_TOKEN": "user-token"}), patch("subprocess.run", fake):
            env = plane_mod.access_env(self.cfg, self.cfg.plane)
        self.assertNotIn("AWS_PROFILE", env)
        self.assertEqual(env["AWS_ACCESS_KEY_ID"], "ASIAEXAMPLE0001")
        self.assertEqual(env["AWS_SESSION_TOKEN"], "token1")

    def test_assume_failure_names_the_role(self):
        self.adopt()
        failed = SimpleNamespace(returncode=254, stdout="", stderr="An error occurred (AccessDenied)")
        with patch("subprocess.run", return_value=failed):
            with self.assertRaises(plane_mod.PlaneError) as caught:
                plane_mod.access_env(self.cfg, self.cfg.plane)
        self.assertIn(ROLE_ARN, str(caught.exception))
        self.assertIn("sts:AssumeRole", str(caught.exception))

    def test_status_reads_the_owner_key_through_the_access_role(self):
        self.adopt()
        fake = FakeAws(self)
        with patch("subprocess.run", fake):
            raw = status_cmd.read_offline_status(self.cfg, "ws-a")
        self.assertEqual(json.loads(raw)["state"], "succeeded")
        (argv, kwargs), = [c for c in fake.calls if c[0][1:3] == ["s3api", "get-object"]]
        self.assertEqual(argv[argv.index("--key") + 1], "checkpoints/{}/ws-a/task-status.json".format(PREFIX))
        self.assertEqual(kwargs["env"]["AWS_ACCESS_KEY_ID"], "ASIAEXAMPLE0001")
        self.assertNotIn("ASIAEXAMPLE0001", " ".join(argv))

    def test_dashboard_keeps_reading_past_the_first_expiry(self):
        self.adopt()
        expiry = EXPIRY
        clock = [expiry - 3600]
        fake = FakeAws(self, expiry=lambda: datetime.datetime.fromtimestamp(
            clock[0] + 3600, datetime.timezone.utc).isoformat())
        listed = _isolated({"workspaces": [_record()]})
        with patch("subprocess.run", fake), patch.object(plane_mod.time, "time", lambda: clock[0]), \
                patch.object(workspace_registry, "_request", return_value=listed):
            first = dashboard.aggregate_snapshot(self.cfg, clock=lambda: clock[0])
            clock[0] = expiry + 600  # well past the first session's expiry
            second = dashboard.aggregate_snapshot(self.cfg, clock=lambda: clock[0])
        self.assertEqual(fake.assumed, 2)
        for snap in (first, second):
            (row,) = snap.workspaces
            self.assertEqual(row.task_state, "succeeded")
            self.assertIsNotNone(row.manifest_age_s)
        heads = fake.argvs("s3api", "head-object")
        self.assertEqual(heads[0][heads[0].index("--key") + 1],
                         "checkpoints/{}/ws-ws/manifest.json".format(PREFIX))
        envs = [kw["env"]["AWS_SESSION_TOKEN"] for argv, kw in fake.calls if argv[1] == "s3api"]
        self.assertEqual(set(envs), {"token1", "token2"})

    def test_remote_check_lists_the_owner_trees(self):
        self.adopt()
        fake = FakeAws(self)
        record = list_cmd.WorkspaceRecord("a", "opencode", "s3", "sid", "created", "ws-a", "ws-a", 1)
        with patch("subprocess.run", fake):
            lines = list_cmd.remote_orphan_report(self.cfg, [record])
        prefixes = [argv[argv.index("--prefix") + 1] for argv in fake.argvs("s3api", "list-objects-v2")]
        self.assertEqual(prefixes, ["workspace-writers/{}/".format(PREFIX), "checkpoints/{}/".format(PREFIX)])
        get = fake.argvs("s3api", "get-object")[0]
        self.assertEqual(get[get.index("--key") + 1], "workspace-writers/{}/ws-a.json".format(PREFIX))
        self.assertEqual(len(lines), 1)
        self.assertIn("'ws-gone'", lines[0])
        self.assertTrue(all("env" in kw for argv, kw in fake.calls if argv[1] == "s3api"))


# --- R43: isolation off unchanged ---------------------------------------------------

class IsolationOffGoldenTests(Base):
    GOLDEN = ("state        : succeeded\n"
              "exit_code    : 0\n"
              "harness      : opencode\n"
              "storage      : s3\n"
              "task_id      : t-1\n"
              "started      : 2026-09-27T10:00:00Z\n"
              "finished     : 2026-09-27T10:05:00Z\n"
              "duration_s   : 300\n"
              "output:\n"
              "done\n"
              "checkpoint   : ok\n")

    def test_registry_off_status_output_and_request_are_unchanged(self):
        cfg = _cfg(self.tmp, registry=False)
        workspace.save_workspace_state(cfg, "ws", "sid-1", "opencode", storage="s3", epoch=1)
        status = {"state": "succeeded", "exit_code": 0, "harness": "opencode", "storage": "s3",
                  "task_id": "t-1", "started_utc": "2026-09-27T10:00:00Z",
                  "finished_utc": "2026-09-27T10:05:00Z", "duration_s": 300, "output": "done",
                  "checkpoint_status": "ok"}
        fake = FakeAws(self, status=status)
        out = io.StringIO()
        with patch("subprocess.run", fake), contextlib.redirect_stdout(out):
            rc = status_cmd.cmd_status(cfg, ["ws"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue(), self.GOLDEN)
        (argv, kwargs), = fake.calls
        self.assertEqual(argv[:9], ["aws", "s3api", "get-object", "--bucket",
                                    "sch-dev-checkpoints-" + ACCOUNT, "--key",
                                    "checkpoints/ws/task-status.json", "--region", "eu-west-1"])
        # Isolation off: the caller's own credentials (no `env` override).
        self.assertEqual(set(kwargs), {"stdout", "stderr", "text"})
        self.assertIsNone(getattr(cfg, "plane", None))

    def test_registry_off_dashboard_and_remote_check_use_flat_keys_and_own_credentials(self):
        cfg = _cfg(self.tmp, registry=False)
        fake = FakeAws(self)
        record = list_cmd.WorkspaceRecord("a", "opencode", "s3", "sid", "created", "a", "a", 1)
        with patch("subprocess.run", fake):
            dashboard.read_manifest_age(cfg, "a")
            list_cmd.remote_orphan_report(cfg, [record])
        head = fake.argvs("s3api", "head-object")[0]
        self.assertEqual(head[head.index("--key") + 1], "checkpoints/a/manifest.json")
        prefixes = [argv[argv.index("--prefix") + 1] for argv in fake.argvs("s3api", "list-objects-v2")]
        self.assertEqual(prefixes, ["workspace-writers/", "checkpoints/"])
        self.assertTrue(all("env" not in kw for _, kw in fake.calls))


# --- R42: no provisioning from the CLI ------------------------------------------------

class NoProvisioningTests(unittest.TestCase):
    FORBIDDEN = re.compile(
        r"create-agent-runtime|update-agent-runtime|put-resource-policy|delete-resource-policy|"
        r"create-role|update-role|put-role-policy|attach-role-policy|update-assume-role-policy|"
        r"create-policy|put-role-permissions-boundary|put-parameter|"
        r"CreateAgentRuntime|UpdateAgentRuntime|PutResourcePolicy|PutParameter"
    )

    def test_no_sch_command_creates_or_updates_runtimes_roles_or_policies(self):
        root = Path(_CLI_DIR) / "sch"
        hits = []
        for path in sorted(root.rglob("*.py")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if self.FORBIDDEN.search(line):
                    hits.append("{}:{}: {}".format(path.relative_to(root), number, line.strip()))
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
