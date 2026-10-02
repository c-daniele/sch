"""Tests of infra/isolation_plan.py, the isolation preflight of deploy.sh.

Spec: docs/specs/security/per-principal-isolation.md R1-R7, R12, R44, X9.

The helper runs against a stub `aws` executable put first on PATH: the stub
answers from a JSON table keyed by "<service> <command>" and logs every
invocation, so the tests see exactly which AWS requests a plan makes (and
that none is a write). Every request the helper builds is also validated
against the botocore service model with ParamValidator.
"""

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import botocore.session
from botocore import xform_name
from botocore.validate import ParamValidator

import isolation_plan as plan

INFRA_DIR = Path(__file__).resolve().parent
HELPER = INFRA_DIR / "isolation_plan.py"

ALICE_ID = "AIDAEXAMPLEALICE0001"
CAROL_ID = "AIDAEXAMPLECAROL0001"
DEVS_ROLE_ID = "AROAEXAMPLEDEVS00001"
BOTS_ROLE_ID = "AROAEXAMPLEBOTS00001"
DEVS_ROLE = "AWSReservedSSO_Developers_0123456789abcdef"

STUB = r'''#!{python}
import json, os, sys
args = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as fh:
    fh.write(json.dumps(args) + "\n")
table = json.load(open(os.environ["STUB_TABLE"]))
key = " ".join(args[:2])
name = None
for flag in ("--user-name", "--role-name", "--stack-name"):
    if flag in args:
        name = args[args.index(flag) + 1]
entry = table.get(key + " " + name) if name else None
if entry is None:
    entry = table.get(key)
if entry is None:
    sys.stderr.write("An error occurred (AccessDenied): stub has no answer for " + key + "\n")
    sys.exit(254)
if "error" in entry:
    sys.stderr.write(entry["error"] + "\n")
    sys.exit(254)
sys.stdout.write(json.dumps(entry["out"]))
'''


def user(user_id):
    return {"out": {"User": {"UserId": user_id, "UserName": "x", "Arn": "arn:aws:iam::111122223333:user/x"}}}


def default_table():
    return {
        "iam get-user alice": user(ALICE_ID),
        "iam get-user carol": user(CAROL_ID),
        "iam get-user ghost": {"error": "An error occurred (NoSuchEntity) when calling the GetUser operation: The user with name ghost cannot be found."},
        "iam get-role bots": {"out": {"Role": {"RoleId": BOTS_ROLE_ID, "RoleName": "bots", "Path": "/"}}},
        "iam get-role " + DEVS_ROLE: {"out": {"Role": {"RoleId": DEVS_ROLE_ID, "RoleName": DEVS_ROLE,
                                                        "Path": "/aws-reserved/sso.amazonaws.com/eu-west-1/"}}},
        "iam list-roles": {"out": {"Roles": [
            {"RoleName": DEVS_ROLE, "RoleId": DEVS_ROLE_ID, "Path": "/aws-reserved/sso.amazonaws.com/eu-west-1/"},
            {"RoleName": "AWSReservedSSO_Admins_1111111111111111", "RoleId": "AROAEXAMPLEADMINS001",
             "Path": "/aws-reserved/sso.amazonaws.com/"},
            {"RoleName": "AWSReservedSSO_DevelopersPlus_2222222222222222", "RoleId": "AROAEXAMPLEDEVPLUS01",
             "Path": "/aws-reserved/sso.amazonaws.com/"},
        ]}},
        "cloudformation list-stacks": {"out": {"StackSummaries": []}},
        "bedrock-agentcore-control list-agent-runtimes": {"out": {"agentRuntimes": [
            {"agentRuntimeName": "sch_dev_runtime"}]}},
        "service-quotas get-service-quota": {"out": {"Quota": {"Value": 100.0}}},
    }


def key_of(owner):
    return hashlib.sha256(owner.encode()).hexdigest()[:16]


class StubbedAws:
    def __init__(self, testcase, table):
        self.tmp = tempfile.TemporaryDirectory()
        testcase.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.bin = root / "bin"
        self.bin.mkdir()
        stub = self.bin / "aws"
        stub.write_text(STUB.replace("{python}", sys.executable))
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        self.table_path = root / "table.json"
        self.table_path.write_text(json.dumps(table))
        self.log = root / "log"
        self.log.write_text("")
        self.out = root / "plan.tsv"

    def run(self, **env):
        base = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "STUB_LOG": str(self.log),
            "STUB_TABLE": str(self.table_path),
            "PROJECT_NAME": "sch",
            "ENVIRONMENT": "dev",
            "REGION": "eu-west-1",
            "ENABLE_WORKSPACE_REGISTRY": "true",
        }
        base.update(env)
        proc = subprocess.run([sys.executable, str(HELPER), "plan", "--out", str(self.out)],
                              env=base, capture_output=True, text=True)
        lines = self.out.read_text().splitlines() if proc.returncode == 0 and self.out.exists() else []
        return proc, [line.split("\t") for line in lines]

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]


class OwnerKeyTest(unittest.TestCase):
    """R7: ownerId = sha256("<kind>:<boundId>"), ownerKey = its first 16 hex."""

    def test_owner_key_is_the_prefix_of_the_owner_id(self):
        owner = f"user:{ALICE_ID}"
        self.assertEqual(plan.owner_id(owner), hashlib.sha256(owner.encode()).hexdigest())
        self.assertEqual(plan.owner_key(owner), plan.owner_id(owner)[:16])
        self.assertRegex(plan.owner_key(owner), r"^[0-9a-f]{16}$")

    def test_kind_is_part_of_the_owner(self):
        self.assertNotEqual(plan.owner_key(f"sso:{DEVS_ROLE_ID}:bob"), plan.owner_key(f"role:{DEVS_ROLE_ID}"))


class ParseEntriesTest(unittest.TestCase):
    """R1/R3/R4: entry forms, whitespace, malformed entries."""

    def test_forms_and_whitespace(self):
        entries = plan.parse_entries(" user:alice , sso:Developers/bob@example.com,role:bots,, ")
        self.assertEqual([(e.kind, e.name, e.username) for e in entries],
                         [("user", "alice", ""), ("sso", "Developers", "bob@example.com"), ("role", "bots", "")])

    def test_empty_is_off(self):
        self.assertEqual(plan.parse_entries(""), [])
        self.assertEqual(plan.parse_entries(" , "), [])

    def test_malformed_entries_fail(self):
        for raw in ("alice", "group:devs", "user:", "sso:Developers", "sso:/bob", "sso:Developers/",
                    'sso:Developers/bo"b', "sso:Developers/b*b", "user:al ice", "role:a/b"):
            with self.subTest(raw=raw):
                with self.assertRaises(plan.PlanError):
                    plan.parse_entries(raw)


class KebabTest(unittest.TestCase):
    """The CLI spelling of every operation the helper uses matches botocore's."""

    def test_operations_translate_like_botocore(self):
        for operation in ("GetUser", "GetRole", "ListRoles", "ListStacks", "DescribeStacks",
                          "GetServiceQuota", "GetAWSDefaultServiceQuota", "ListAgentRuntimes"):
            with self.subTest(operation=operation):
                self.assertEqual(plan._kebab(operation), xform_name(operation, "-"))
        for param in ("UserName", "RoleName", "PathPrefix", "StackName", "ServiceCode", "QuotaCode"):
            self.assertEqual(plan._kebab(param), xform_name(param, "-"))


class PlanTest(unittest.TestCase):
    def stub(self, **table_updates):
        table = default_table()
        table.update(table_updates)
        return StubbedAws(self, table)

    def validate_requests(self, calls):
        """Every request must be a valid botocore request and a read."""
        session = botocore.session.get_session()
        validator = ParamValidator()
        for argv in calls:
            service, command = argv[0], argv[1]
            model = session.get_service_model(service)
            operations = {xform_name(op, "-"): op for op in model.operation_names}
            self.assertIn(command, operations, argv)
            operation = model.operation_model(operations[command])
            self.assertRegex(operation.name, r"^(Get|List|Describe)", argv)
            params = {}
            rest = argv[2:]
            for i in range(0, len(rest), 2):
                flag, value = rest[i], rest[i + 1]
                if flag in ("--region", "--output"):
                    continue
                members = {xform_name(m, "-"): m for m in operation.input_shape.members} \
                    if operation.input_shape else {}
                self.assertIn(flag[2:], members, argv)
                params[members[flag[2:]]] = value
            report = validator.validate(params, operation.input_shape) if operation.input_shape else None
            if report is not None:
                self.assertFalse(report.has_errors(), report.generate_report())

    def test_user_entry_resolves_to_its_unique_id(self):
        stub = self.stub()
        proc, lines = stub.run(ISOLATED_PRINCIPALS="user:alice")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        key = key_of(f"user:{ALICE_ID}")
        self.assertEqual(lines, [["PLANE", "user:alice", "user", key, ALICE_ID, f"sch-dev-plane-{key}", "NEW",
                                  "false"]])
        self.validate_requests(stub.calls())

    def test_sso_entry_binds_role_id_and_username_and_warns(self):
        stub = self.stub()
        proc, lines = stub.run(ISOLATED_PRINCIPALS="sso:Developers/bob@example.com")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        bound = f"{DEVS_ROLE_ID}:bob@example.com"
        key = key_of(f"sso:{bound}")
        self.assertEqual(lines[0][1:6], ["sso:Developers/bob@example.com", "sso", key, bound, f"sch-dev-plane-{key}"])
        self.assertIn("WARNING", proc.stderr)
        self.assertIn("bob@example.com", proc.stderr)
        self.assertIn("not verified", proc.stderr)
        list_roles = [c for c in stub.calls() if c[:2] == ["iam", "list-roles"]]
        self.assertEqual(list_roles[0][2:4], ["--path-prefix", "/aws-reserved/sso.amazonaws.com/"])
        self.validate_requests(stub.calls())

    def test_two_users_of_one_permission_set_are_two_owners(self):
        stub = self.stub()
        proc, lines = stub.run(ISOLATED_PRINCIPALS="sso:Developers/bob,sso:Developers/dave")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len({line[3] for line in lines}), 2)
        # The role list is read once for all sso entries.
        self.assertEqual(sum(1 for c in stub.calls() if c[:2] == ["iam", "list-roles"]), 1)

    def test_role_entry_matches_every_session(self):
        stub = self.stub()
        proc, lines = stub.run(ISOLATED_PRINCIPALS="role:bots")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(lines[0][2:5], ["role", key_of(f"role:{BOTS_ROLE_ID}"), f"{BOTS_ROLE_ID}:*"])

    def test_failures_before_any_change(self):
        cases = {
            "unknown user": ("user:ghost", "does not exist"),
            "unknown permission set": ("sso:Nobody/bob", "no IAM Identity Center role"),
            "role under the sso path": (f"role:{DEVS_ROLE}", "use"),
            "duplicate user": ("user:alice,user:alice", "same identity"),
            "duplicate sso user": ("sso:Developers/bob,sso:Developers/bob", "same identity"),
        }
        for label, (raw, message) in cases.items():
            with self.subTest(label=label):
                stub = self.stub()
                proc, _ = stub.run(ISOLATED_PRINCIPALS=raw)
                self.assertEqual(proc.returncode, 2)
                self.assertIn(message if message != "use" else "sso:<permission-set>/<username>", proc.stderr)
                self.assertIn("no stack has been changed", proc.stderr)
                self.assertFalse(stub.out.exists() and stub.out.read_text())

    def test_ambiguous_permission_set_fails(self):
        roles = default_table()["iam list-roles"]["out"]["Roles"] + [
            {"RoleName": "AWSReservedSSO_Developers_3333333333333333", "RoleId": "AROAEXAMPLEDEVS00002",
             "Path": "/aws-reserved/sso.amazonaws.com/us-east-1/"}]
        stub = self.stub(**{"iam list-roles": {"out": {"Roles": roles}}})
        proc, _ = stub.run(ISOLATED_PRINCIPALS="sso:Developers/bob")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("matches several roles", proc.stderr)

    def test_permission_set_name_must_match_exactly(self):
        # DevelopersPlus must not satisfy sso:Developers, nor the reverse prefix.
        stub = self.stub()
        proc, lines = stub.run(ISOLATED_PRINCIPALS="sso:Developers/bob")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(lines[0][4].startswith(DEVS_ROLE_ID))

    def test_sso_and_role_entries_on_the_same_role_fail(self):
        stub = self.stub(**{"iam get-role twin": {"out": {"Role": {
            "RoleId": DEVS_ROLE_ID, "RoleName": "twin", "Path": "/"}}}})
        proc, _ = stub.run(ISOLATED_PRINCIPALS="sso:Developers/bob,role:twin")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("resolve to the same role", proc.stderr)

    def test_registry_is_required(self):
        stub = self.stub()
        proc, _ = stub.run(ISOLATED_PRINCIPALS="user:alice", ENABLE_WORKSPACE_REGISTRY="false")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("ENABLE_WORKSPACE_REGISTRY=true", proc.stderr)
        self.assertEqual(stub.calls(), [])

    def test_telegram_without_a_bound_principal_is_refused(self):
        """R44: the Telegram switches need TELEGRAM_PRINCIPAL with isolation on."""
        for env in ({"TELEGRAM_BOT_TOKEN": "t"}, {"TELEGRAM_CHAT_ID": "1"},
                    {"ENABLE_TELEGRAM_INTERACTION": "true"}):
            with self.subTest(env=env):
                stub = self.stub()
                proc, _ = stub.run(ISOLATED_PRINCIPALS="user:alice", **env)
                self.assertEqual(proc.returncode, 2)
                self.assertIn("set TELEGRAM_PRINCIPAL", proc.stderr)
                self.assertEqual(stub.calls(), [])

    def test_telegram_principal_must_be_listed_and_single(self):
        telegram = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}
        for principal, message in (("user:bob", "is not in ISOLATED_PRINCIPALS"),
                                   ("user:alice,user:carol", "exactly one entry"),
                                   ("alice", "expected user:")):
            with self.subTest(principal=principal):
                stub = self.stub()
                proc, _ = stub.run(ISOLATED_PRINCIPALS="user:alice,user:carol",
                                   TELEGRAM_PRINCIPAL=principal, **telegram)
                self.assertEqual(proc.returncode, 2)
                self.assertIn(message, proc.stderr)
                self.assertEqual(stub.calls(), [])

    def test_telegram_principal_needs_telegram_and_isolation(self):
        stub = self.stub()
        proc, _ = stub.run(ISOLATED_PRINCIPALS="user:alice", TELEGRAM_PRINCIPAL="user:alice")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("TELEGRAM_PRINCIPAL requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID", proc.stderr)
        self.assertEqual(stub.calls(), [])
        stub = self.stub()
        proc, _ = stub.run(ISOLATED_PRINCIPALS="", TELEGRAM_PRINCIPAL="user:alice",
                           TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="1")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("ISOLATED_PRINCIPALS is empty", proc.stderr)
        self.assertEqual(stub.calls(), [])

    def test_the_bound_plane_is_marked_in_the_plan(self):
        stub = self.stub()
        proc, lines = stub.run(ISOLATED_PRINCIPALS="user:alice, user:carol", TELEGRAM_PRINCIPAL=" user:alice ",
                               TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual([(line[1], line[7]) for line in lines], [("user:alice", "true"), ("user:carol", "false")])
        # Without Telegram no plane is bound, whatever else is set.
        proc, lines = stub.run(ISOLATED_PRINCIPALS="user:alice, user:carol")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual([line[7] for line in lines], ["false", "false"])

    def test_quota_counts_existing_new_orphans_and_the_shared_runtime(self):
        runtimes = [{"agentRuntimeName": f"other_{i}"} for i in range(98)]
        stub = self.stub(**{"bedrock-agentcore-control list-agent-runtimes": {"out": {"agentRuntimes": runtimes}}})
        # 98 existing + 2 planes + 1 missing shared runtime = 101 > 100.
        proc, _ = stub.run(ISOLATED_PRINCIPALS="user:alice,user:carol")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("quota", proc.stderr)
        self.assertIn("1 shared runtime to create", proc.stderr)
        # One plane fits exactly: 98 + 1 + 1 = 100.
        proc, _ = stub.run(ISOLATED_PRINCIPALS="user:alice")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_quota_falls_back_to_the_default_then_to_100(self):
        runtimes = [{"agentRuntimeName": "sch_dev_runtime"}] + [{"agentRuntimeName": f"x{i}"} for i in range(99)]
        stub = self.stub(**{
            "bedrock-agentcore-control list-agent-runtimes": {"out": {"agentRuntimes": runtimes}},
            "service-quotas get-service-quota": {"error": "An error occurred (AccessDenied)"},
        })
        proc, _ = stub.run(ISOLATED_PRINCIPALS="user:alice")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("documented default", proc.stderr)
        called = [c[:2] for c in stub.calls()]
        self.assertIn(["service-quotas", "get-aws-default-service-quota"], called)
        self.validate_requests(stub.calls())

    def test_runtime_listing_failure_fails_the_preflight(self):
        stub = self.stub(**{"bedrock-agentcore-control list-agent-runtimes": {"error": "An error occurred (AccessDenied)"}})
        proc, _ = stub.run(ISOLATED_PRINCIPALS="user:alice")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("quota check", proc.stderr)

    def plane_stacks(self):
        alice = key_of(f"user:{ALICE_ID}")
        gone = "0123456789abcdef"
        foreign = "fedcba9876543210"
        return alice, gone, foreign, {
            "cloudformation list-stacks": {"out": {"StackSummaries": [
                {"StackName": f"sch-dev-plane-{alice}", "StackStatus": "UPDATE_COMPLETE"},
                {"StackName": f"sch-dev-plane-{gone}", "StackStatus": "CREATE_COMPLETE"},
                {"StackName": f"sch-dev-plane-{foreign}", "StackStatus": "CREATE_COMPLETE"},
                {"StackName": "sch-dev-plane-deadbeefdeadbeef", "StackStatus": "DELETE_COMPLETE"},
                {"StackName": "sch-dev-runtime", "StackStatus": "UPDATE_COMPLETE"},
            ]}},
            f"cloudformation describe-stacks sch-dev-plane-{alice}": {"out": {"Stacks": [{
                "StackStatus": "UPDATE_COMPLETE",
                "Tags": [{"Key": "sch:deployment", "Value": "sch-dev"}, {"Key": "sch:owner-key", "Value": alice}]}]}},
            f"cloudformation describe-stacks sch-dev-plane-{gone}": {"out": {"Stacks": [{
                "StackStatus": "CREATE_COMPLETE",
                "Tags": [{"Key": "sch:deployment", "Value": "sch-dev"}, {"Key": "sch:owner-key", "Value": gone}]}]}},
            # Same prefix, not this deployment's tag: never touched.
            f"cloudformation describe-stacks sch-dev-plane-{foreign}": {"out": {"Stacks": [{
                "StackStatus": "CREATE_COMPLETE",
                "Tags": [{"Key": "sch:deployment", "Value": "other"}, {"Key": "sch:owner-key", "Value": foreign}]}]}},
        }

    def test_existing_planes_are_updated_and_orphans_deleted(self):
        alice, gone, foreign, table = self.plane_stacks()
        stub = self.stub(**table)
        proc, lines = stub.run(ISOLATED_PRINCIPALS="user:alice")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(lines[0][0], "PLANE")
        self.assertEqual(lines[0][6], "UPDATE_COMPLETE")
        self.assertEqual(lines[1], ["ORPHAN", f"sch-dev-plane-{gone}", gone, "CREATE_COMPLETE"])
        self.assertEqual(len(lines), 2)
        described = [c[c.index("--stack-name") + 1] for c in stub.calls() if c[:2] == ["cloudformation", "describe-stacks"]]
        self.assertNotIn("sch-dev-plane-deadbeefdeadbeef", described)
        self.validate_requests(stub.calls())

    def test_empty_list_makes_every_plane_an_orphan_and_resolves_nothing(self):
        alice, gone, foreign, table = self.plane_stacks()
        stub = self.stub(**table)
        proc, lines = stub.run(ISOLATED_PRINCIPALS="", ENABLE_WORKSPACE_REGISTRY="false")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(sorted(line[2] for line in lines), sorted([alice, gone]))
        self.assertTrue(all(line[0] == "ORPHAN" for line in lines))
        services = {c[0] for c in stub.calls()}
        self.assertEqual(services, {"cloudformation"})

    def test_isolation_off_without_planes_writes_an_empty_plan(self):
        stub = self.stub()
        proc, lines = stub.run(ISOLATED_PRINCIPALS="")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(lines, [])
        self.assertEqual([c[:2] for c in stub.calls()], [["cloudformation", "list-stacks"]])

    def test_failed_creation_is_counted_as_a_creation(self):
        alice = key_of(f"user:{ALICE_ID}")
        table = {
            "cloudformation list-stacks": {"out": {"StackSummaries": [
                {"StackName": f"sch-dev-plane-{alice}", "StackStatus": "ROLLBACK_COMPLETE"}]}},
            f"cloudformation describe-stacks sch-dev-plane-{alice}": {"out": {"Stacks": [{
                "StackStatus": "ROLLBACK_COMPLETE",
                "Tags": [{"Key": "sch:deployment", "Value": "sch-dev"}, {"Key": "sch:owner-key", "Value": alice}]}]}},
        }
        stub = self.stub(**table)
        proc, lines = stub.run(ISOLATED_PRINCIPALS="user:alice")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(lines[0][6], "ROLLBACK_COMPLETE")


class PolicySizeTest(unittest.TestCase):
    """X9: the deploy refuses a base policy or escape hatch over 6,144 characters."""

    def run_size(self, **env):
        base = {"PATH": "/usr/bin:/bin"}
        base.update(env)
        return subprocess.run([sys.executable, str(HELPER), "policy-size"], env=base,
                              capture_output=True, text=True)

    def test_defaults_pass(self):
        self.assertEqual(self.run_size().returncode, 0)

    def test_long_allowlist_fails(self):
        allowlist = ",".join(f"eu.anthropic.claude-model-number-{i:03d}-v1:0" for i in range(60))
        proc = self.run_size(RUNTIME_BEDROCK_MODEL_ALLOWLIST=allowlist)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("too long", proc.stderr)

    def test_escape_hatch_size_and_syntax(self):
        big = json.dumps({"Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": f"translate:Action{i:04d}", "Resource": "*"} for i in range(200)]})
        proc = self.run_size(RUNTIME_EXTRA_POLICY_JSON=big)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("at most 6144", proc.stderr)
        proc = self.run_size(RUNTIME_EXTRA_POLICY_JSON="{not json")
        self.assertEqual(proc.returncode, 2)
        small = '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"translate:TranslateText","Resource":"*"}]}'
        self.assertEqual(self.run_size(RUNTIME_EXTRA_POLICY_JSON=small).returncode, 0)


if __name__ == "__main__":
    unittest.main()
