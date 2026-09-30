"""Hermetic checks of the live verification scripts (TASK-20.5).

The scripts talk to AWS, so these tests run them against a small fake world:
``aws``, ``agentcore``, ``sch`` and the ``verify_support`` helper are
replaced by stubs that implement the isolation rules (a caller reaches only
its own plane and owner tree). They prove the scripts' logic: that
``bin/verify-isolation.sh`` passes on an isolated world, fails when any
boundary leaks, masks account IDs, and that ``bin/lib/verify-target.sh``
resolves the plane on isolation stacks and stays inert on registry-off
stacks. Whether AWS enforces the rules is the operator-side live check.
"""

import json
import os
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BIN = REPO / "bin"
ACCOUNT = "111122223333"
KEYS = {"a": "a" * 16, "b": "b" * 16, "d": "d" * 16}
SHARED_ARN = "arn:aws:bedrock-agentcore:eu-west-1:{}:runtime/sch_dev_shared-AbCdEf1234".format(ACCOUNT)


def _runtime_arn(profile):
    return "arn:aws:bedrock-agentcore:eu-west-1:{}:runtime/sch_dev_o_{}-AbCdEf1234".format(
        ACCOUNT, KEYS[profile])


def _access_arn(profile):
    return "arn:aws:iam::{}:role/sch-dev-o-{}-access".format(ACCOUNT, KEYS[profile])


# Shared by the stubs: which profile owns which key, runtime and role.
WORLD = {
    "account": ACCOUNT,
    "keys": KEYS,
    "runtimes": {p: _runtime_arn(p) for p in KEYS},
    "access": {p: _access_arn(p) for p in KEYS},
    "shared": SHARED_ARN,
}

FAKE_AWS = r'''
import json, os, sys
world = json.load(open(os.environ["FAKE_WORLD"]))
argv = sys.argv[1:]
profile = os.environ.get("AWS_PROFILE", "")
agent = os.environ.get("FAKE_AGENT", "")
access = os.environ.get("FAKE_ACCESS_ROLE", "")
leak = os.environ.get("FAKE_LEAK", "")
outputs = json.loads(os.environ.get("FAKE_OUTPUTS", "{}"))

def opt(name):
    return argv[argv.index(name) + 1] if name in argv else ""

def deny(op):
    sys.stderr.write("An error occurred (AccessDeniedException) when calling the %s operation: "
                     "User: arn:aws:iam::%s:user/%s is not authorized\n" % (op, world["account"], profile))
    sys.exit(254)

def owner_of(path):
    for p, key in world["keys"].items():
        if "/o.%s/" % key in path:
            return p
    return ""

service, op = argv[0], argv[1]
if service == "cloudformation" and op == "describe-stacks":
    query = opt("--query")
    for key, value in outputs.items():
        if "'%s'" % key in query:
            print(value)
            sys.exit(0)
    print("None")
    sys.exit(0)
if service == "sts" and op == "get-caller-identity":
    print(("AIDA" + profile.upper() + "EXAMPLE") if opt("--query") == "UserId"
          else "arn:aws:iam::%s:user/%s" % (world["account"], profile))
    sys.exit(0)
if service == "sts" and op == "assume-role":
    deny("AssumeRole")
if service == "bedrock-agentcore":
    arn = opt("--agent-runtime-arn")
    owner = [p for p, r in world["runtimes"].items() if r == arn]
    allowed = bool(owner) and owner[0] == profile and not agent
    if leak == "join" and profile == "b":
        allowed = True
    if not allowed:
        deny("InvokeAgentRuntime" if op == "invoke-agent-runtime" else "StopRuntimeSession")
    if op == "invoke-agent-runtime":
        open(argv[-1], "w").write("{}")
    sys.exit(0)
if service == "s3api":
    target = opt("--key") or opt("--prefix")
    if agent:
        allowed = owner_of(target) == agent
        if leak == "agent-read" and target.endswith("task-status.json"):
            allowed = True
    elif access:
        allowed = owner_of(target) == access
    else:
        allowed = leak == "own-creds"
    if not allowed:
        deny("HeadObject" if op == "head-object" else "ListObjectsV2")
    print("{}")
    sys.exit(0)
if service in ("dynamodb", "ssm", "bedrock-agentcore-control"):
    deny(op)
sys.stderr.write("fake aws: unexpected call %r\n" % argv)
sys.exit(99)
'''

FAKE_AGENTCORE = r'''
import json, os, subprocess, sys
world = json.load(open(os.environ["FAKE_WORLD"]))
argv = sys.argv[1:]
runtime = argv[argv.index("--runtime") + 1]
profile = os.environ.get("AWS_PROFILE", "")
if world["runtimes"].get(profile) != runtime:
    # Like the real CLI (0.24.x): with --json a denial is reported as a bare
    # {"success": false, ...} and the AWS error text is dropped.
    if "--json" in argv:
        print(json.dumps({"success": False, "stdout": "", "stderr": ""}))
    else:
        sys.stderr.write("Error: User: arn:aws:iam::111122223333:user/" + profile
                         + " is not authorized to perform: bedrock-agentcore:InvokeAgentRuntimeCommand"
                         " on resource: " + runtime + " with an explicit deny in a resource-based policy\n")
    sys.exit(1)
command = " ".join(argv[argv.index("--") + 1:])
env = dict(os.environ, FAKE_AGENT=profile)
env.pop("FAKE_ACCESS_ROLE", None)
result = subprocess.run(command, shell=True, capture_output=True, text=True, env=env)
print(json.dumps({"stdout": result.stdout, "stderr": result.stderr, "exitCode": result.returncode}))
'''

FAKE_SUPPORT = r'''
import json, os, subprocess, sys
world = json.load(open(os.environ["FAKE_WORLD"]))
state = os.environ["FAKE_STATE"]
profile = os.environ.get("AWS_PROFILE", "")
argv = sys.argv[1:]

def plane(p):
    return {"runtimeArn": world["runtimes"][p], "accessRoleArn": world["access"][p],
            "ownerPrefix": "o." + world["keys"][p], "ownerKey": world["keys"][p]}

if os.environ.get("FAKE_REGISTRY_OFF"):
    if argv[0] == "probe":
        print(json.dumps({"registry": False, "isolation": False, "plane": None}))
        sys.exit(0)
    sys.exit(2)
if profile not in world["keys"]:
    sys.stderr.write("verify-support: workspace registry rejected request: caller "
                     "arn:aws:iam::%s:user/%s is not an isolated principal of this deployment; "
                     "add user:%s to ISOLATED_PRINCIPALS and redeploy\n" % (world["account"], profile, profile))
    sys.exit(4)
if argv[0] == "probe":
    print(json.dumps({"registry": True, "isolation": True, "plane": plane(profile)}))
elif argv[0] == "workspace":
    ident = "ident-" + profile
    print(json.dumps({"workspace": argv[1], "sessionId": "sid-" + profile, "harness": "opencode",
                      "storage": "s3", "sessionEpoch": 1, "runtimeWorkspace": ident,
                      "isolation": True, "runtimeArn": world["runtimes"][profile],
                      "ownerPrefix": "o." + world["keys"][profile],
                      "accessRoleArn": world["access"][profile],
                      "checkpointPrefix": "checkpoints/o.%s/%s/" % (world["keys"][profile], ident)}))
elif argv[0] == "owner-exec":
    command = argv[2:] if argv[1:2] == ["--"] else argv[1:]
    sys.exit(subprocess.run(command, env=dict(os.environ, FAKE_ACCESS_ROLE=profile)).returncode)
elif argv[0] == "dashboard":
    ws = open(os.path.join(state, "ws-" + profile)).read().strip()
    print(json.dumps([{"name": ws, "taskState": "succeeded"}]))
'''

FAKE_SCH = r'''
import json, os, sys
state = os.environ["FAKE_STATE"]
profile = os.environ.get("AWS_PROFILE", "")
argv = sys.argv[1:]
marker = os.path.join(state, "ws-" + profile)
if argv[0] == "task":
    open(marker, "w").write(argv[1])
    print("task-" + profile)
elif argv[0] == "status":
    print(json.dumps({"state": "succeeded"}))
elif argv[0] == "list":
    print("WORKSPACE HARNESS STORAGE SESSION")
    if os.path.exists(marker):
        print("%s opencode s3 sid-%s" % (open(marker).read().strip(), profile))
elif argv[0] == "delete":
    # The registry URL is recorded too: a delete without it would run in
    # local-index mode against the locked shared runtime (regression check).
    open(os.path.join(state, "deleted-" + profile), "w").write(
        argv[1] + "\n" + os.environ.get("SCH_WORKSPACE_REGISTRY_URL", ""))
'''


def _write_exec(path, body):
    path.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(body))
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class _FakeWorld:
    def __init__(self, tmp):
        self.root = Path(tmp)
        self.bin = self.root / "fakebin"
        self.state = self.root / "state"
        self.bin.mkdir()
        self.state.mkdir()
        (self.root / "world.json").write_text(json.dumps(WORLD))
        _write_exec(self.bin / "aws", FAKE_AWS)
        _write_exec(self.bin / "agentcore", FAKE_AGENTCORE)
        _write_exec(self.root / "support.py", FAKE_SUPPORT)
        _write_exec(self.root / "sch", FAKE_SCH)

    def env(self, **extra):
        outputs = {
            "IsolationStatus": "true",
            "WorkspaceRegistryUrl": "https://example.execute-api.eu-west-1.amazonaws.com/v1",
            "CheckpointBucketName": "sch-dev-checkpoints-" + ACCOUNT,
            "WorkspaceRegistryTableName": "sch-dev-workspace-registry",
            "RuntimeArn": SHARED_ARN,
        }
        env = {
            "PATH": "{}:/usr/bin:/bin".format(self.bin),
            "HOME": str(self.root),
            "TMPDIR": str(self.root),
            "FAKE_WORLD": str(self.root / "world.json"),
            "FAKE_STATE": str(self.state),
            "FAKE_OUTPUTS": json.dumps(outputs),
            "SCH_VERIFY_SCH": str(self.root / "sch"),
            "SCH_VERIFY_SUPPORT": str(self.root / "support.py"),
            "SCH_TARGET_SUPPORT": str(self.root / "support.py"),
        }
        env.update(extra)
        return env


class VerifyIsolationScriptTests(unittest.TestCase):
    def _run(self, *extra_args, **env):
        with tempfile.TemporaryDirectory() as tmp:
            world = _FakeWorld(tmp)
            args = [str(BIN / "verify-isolation.sh"), "--profile-a", "a", "--profile-b", "b",
                    "--profile-c", "c", *extra_args]
            result = subprocess.run(args, capture_output=True, text=True, env=world.env(**env),
                                    timeout=120)
            deleted = sorted(p.name for p in world.state.glob("deleted-*"))
            delete_urls = [p.read_text().split("\n")[1] for p in sorted(world.state.glob("deleted-*"))]
        self.assertTrue(all(url.startswith("https://") for url in delete_urls), delete_urls)
        return result, deleted

    def test_passes_on_an_isolated_world_and_cleans_up(self):
        result, deleted = self._run("--profile-sso", "d")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("FAIL:", result.stdout)
        for expected in (
            "C is refused by the registry with the entry to add: add user:c to ISOLATED_PRINCIPALS",
            "A invokes its own session (positive control)",
            "B invokes A's session: denied",
            "B stops A's session: denied",
            "B invokes the shared runtime: denied",
            "D (Identity Center) invokes A's session: denied",
            "B reads A's task status through B's access role: denied",
            "B assumes A's access role: denied",
            "agent of A reads its own task status (positive control)",
            "agent of A, read B's task status: denied",
            "agent of A, scan the registry table: denied",
            "agent of A, read B's plane parameter: denied",
            "a: the dashboard data path reads the task state",
        ):
            self.assertIn("PASS: " + expected, result.stdout)
        self.assertEqual(deleted, ["deleted-a", "deleted-b"])

    def test_masks_account_ids(self):
        result, _ = self._run()
        self.assertNotIn(ACCOUNT, result.stdout + result.stderr)
        self.assertIn("<account-id>", result.stdout)

    def test_a_join_leak_fails(self):
        result, _ = self._run(FAKE_LEAK="join")
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL: B invokes A's session: NOT denied", result.stdout)

    def test_an_agent_read_leak_fails(self):
        result, _ = self._run(FAKE_LEAK="agent-read")
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL: agent of A, read B's task status: NOT denied", result.stdout)

    def test_a_bucket_policy_leak_fails(self):
        result, _ = self._run(FAKE_LEAK="own-creds")
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL: B reads A's task status with its own credentials: NOT denied", result.stdout)

    def test_refuses_a_non_isolated_stack(self):
        outputs = json.dumps({"IsolationStatus": "false"})
        result, deleted = self._run(FAKE_OUTPUTS=outputs)
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not report IsolationStatus=true", result.stdout)

    def test_usage(self):
        result = subprocess.run([str(BIN / "verify-isolation.sh")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        result = subprocess.run([str(BIN / "verify-isolation.sh"), "--help"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("--profile-a", result.stdout)


class VerifyTargetLibTests(unittest.TestCase):
    def _bash(self, script, **env):
        with tempfile.TemporaryDirectory() as tmp:
            world = _FakeWorld(tmp)
            full = '. "{}"\n{}'.format(BIN / "lib" / "verify-target.sh", script)
            return subprocess.run(["bash", "-c", full], capture_output=True, text=True,
                                  env=world.env(**env), timeout=60)

    def test_isolation_resolves_plane_session_and_owner_keys(self):
        result = self._bash(textwrap.dedent('''
            set -euo pipefail
            sch_target_init
            echo "arn=$(sch_target_runtime_arn)"
            echo "prefix=$(sch_target_ckpt_prefix ident-a)"
            sch_target_workspace ws opencode s3
            echo "sid=${SCH_TARGET_SID} ws=${SCH_TARGET_WS} ckpt=${SCH_TARGET_CKPT_PREFIX}"
            sch_target_owner_aws s3api head-object --key "$(sch_target_ckpt_prefix ident-a)manifest.json" >/dev/null
            echo owner-read-ok
        '''), AWS_PROFILE="a")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("arn=" + _runtime_arn("a"), result.stdout)
        self.assertIn("prefix=checkpoints/o.{}/ident-a/".format(KEYS["a"]), result.stdout)
        self.assertIn("sid=sid-a ws=ident-a ckpt=checkpoints/o.{}/ident-a/".format(KEYS["a"]), result.stdout)
        self.assertIn("owner-read-ok", result.stdout)

    def test_registry_off_is_inert(self):
        result = self._bash(textwrap.dedent('''
            set -uo pipefail
            sch_target_init
            echo "arn=[$(sch_target_runtime_arn)]"
            echo "prefix=$(sch_target_ckpt_prefix ws)"
            if sch_target_workspace ws; then echo resolved; else echo local-index; fi
        '''), AWS_PROFILE="a", FAKE_REGISTRY_OFF="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("arn=[]", result.stdout)
        self.assertIn("prefix=checkpoints/ws/", result.stdout)
        self.assertIn("local-index", result.stdout)

    def test_unlisted_caller_stops_the_script(self):
        result = self._bash("sch_target_init\necho reached\n", AWS_PROFILE="c")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("reached", result.stdout)
        self.assertIn("add user:c to ISOLATED_PRINCIPALS", result.stderr)

    def test_telegram_scripts_skip_on_isolation_stacks(self):
        with tempfile.TemporaryDirectory() as tmp:
            world = _FakeWorld(tmp)
            for script, args in (("verify-telegram-notifications.sh", ["ws-a", "ws-b"]),
                                 ("verify-telegram-interaction.sh", ["ws-a"])):
                result = subprocess.run([str(BIN / script), *args], capture_output=True, text=True,
                                        env=world.env(AWS_PROFILE="a"), timeout=60)
                self.assertEqual(result.returncode, 0, script + result.stdout + result.stderr)
                self.assertIn("SKIP: Telegram is not available with per-principal isolation on",
                              result.stdout)


class ScriptSyntaxTests(unittest.TestCase):
    def test_every_script_parses(self):
        scripts = sorted(BIN.glob("verify-*.sh")) + sorted((BIN / "lib").glob("*.sh"))
        self.assertTrue(scripts)
        for script in scripts:
            result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, "{}: {}".format(script.name, result.stderr))

    def test_live_scripts_that_address_the_runtime_use_the_target_library(self):
        # Every script that calls the AgentCore data plane or reads checkpoint
        # keys itself must resolve the plane (no shared-runtime fallback, R40).
        for script in sorted(BIN.glob("verify-*.sh")):
            text = script.read_text()
            direct = ("OutputKey=='RuntimeArn'" in text or "checkpoints/" in text) \
                and script.name not in (
                    "verify-isolation.sh", "verify-docs.sh",
                    # Operator-level stack check: it counts top-level
                    # checkpoint prefixes and handles isolation itself.
                    "verify-runtime-iam-tuning.sh",
                )
            if direct:
                self.assertTrue(
                    "lib/verify-target.sh" in text or "verify_support.py" in text,
                    "{} addresses the runtime or checkpoints without bin/lib/verify-target.sh".format(script.name),
                )


if __name__ == "__main__":
    unittest.main()
