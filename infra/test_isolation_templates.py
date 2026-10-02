"""Rendering tests of the isolation templates (TASK-20.3).

Spec: docs/specs/security/per-principal-isolation.md R1, R10-R25, R39, R44, X9;
runtime-capability-tuning R1/I1 (effective-permission identity at defaults).

These tests pin template SHAPES. They do not prove the policies work: that
evidence comes from IAM Access Analyzer and the IAM policy simulator
(infra/validate_isolation_policies.py, infra/simulate_isolation.py) and from
the operator-side live check.
"""

import json
import re
import unittest

import cfn_render
import isolation_plan
from cfn_render import FAKE_ACCOUNT, render_plane, render_runtime

REGISTRY_ROLE = f"arn:aws:iam::{FAKE_ACCOUNT}:role/sch-dev-workspace-registry-role"
BUCKET = f"sch-dev-checkpoints-{FAKE_ACCOUNT}"
OWNER_KEY = "3f9c0e2a7b1d4c65"
DIGEST = "sha256:" + "0123456789abcdef" * 4

ISOLATION_TYPES = {
    "AWS::S3::BucketPolicy",
    "AWS::BedrockAgentCore::ResourcePolicy",
    "AWS::SSM::Parameter",
}

DATA_PLANE_ACTIONS = [
    "bedrock-agentcore:InvokeAgentRuntime",
    "bedrock-agentcore:InvokeAgentRuntimeForUser",
    "bedrock-agentcore:InvokeAgentRuntimeCommand",
    "bedrock-agentcore:InvokeAgentRuntimeCommandShell",
    "bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStream",
    "bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStreamForUser",
    "bedrock-agentcore:GetAgentCard",
]

# The resources of a default deployment before TASK-20.3; the only addition
# is the base policy moved out of the role (R18).
DEFAULT_RESOURCES_BEFORE = {
    "AgentRuntimeLogGroup", "CheckpointBucket", "TaskWatchdogRole", "TaskWatchdogFunction",
    "TaskWatchdogSchedule", "TaskWatchdogSchedulePermission", "AgentRuntimeRole", "AgentRuntime",
}

ISOLATION_ON = {"EnableWorkspaceRegistry": "true", "IsolationEnabled": "true"}


def plane_params(**overrides):
    params = {
        "OwnerKey": OWNER_KEY,
        "OwnerKind": "user",
        "OwnerUserIdPattern": "AIDAEXAMPLEALICE0001",
        "PrincipalEntry": "user:alice",
        "CheckpointBucket": BUCKET,
        "SharedPolicyArns": f"arn:aws:iam::{FAKE_ACCOUNT}:policy/sch-dev-agentcore-policy",
        "RegistryRoleArn": REGISTRY_ROLE,
    }
    params.update(overrides)
    return params


def as_list(value):
    return value if isinstance(value, list) else [value]


def all_actions(document):
    actions = []
    for statement in as_list(document.get("Statement", [])):
        if statement.get("Effect") == "Allow":
            actions.extend(as_list(statement.get("Action", [])))
    return actions


class DefaultRenderingTest(unittest.TestCase):
    """R1/I1: with isolation off nothing of the spec exists."""

    COMBINATIONS = [
        {},
        {"EnableWorkspaceRegistry": "true"},
        {"EnableWorkspaceRegistry": "true", "EnableSessionImageRebuild": "true",
         "TelegramBotToken": "t", "TelegramChatId": "1", "EnableTelegramInteraction": "true"},
        {"IsolationEnabled": "true"},  # without the registry: inert (R2)
        {"RuntimeCapTranscribeEnabled": "true", "RuntimeExtraPolicyJson": '{"Version":"2012-10-17","Statement":[]}'},
    ]

    def test_defaults_add_only_the_base_policy(self):
        resources = set(render_runtime().resources())
        self.assertEqual(resources, DEFAULT_RESOURCES_BEFORE | {"AgentRuntimeBasePolicy"})

    def test_no_isolation_resource_without_isolation(self):
        for overrides in self.COMBINATIONS:
            with self.subTest(overrides=overrides):
                renderer = render_runtime(overrides)
                types = {r["Type"] for r in renderer.resources().values()}
                self.assertFalse(types & ISOLATION_TYPES)
                self.assertEqual(renderer.outputs()["IsolationStatus"], "false")

    def test_registry_is_unchanged_without_isolation(self):
        renderer = render_runtime({"EnableWorkspaceRegistry": "true"})
        env = renderer.resource("WorkspaceRegistryFunction")["Properties"]["Environment"]["Variables"]
        self.assertNotIn("ISOLATION_ENABLED", env)
        self.assertNotIn("PLANE_PARAMETER_PREFIX", env)
        role = renderer.resource("WorkspaceRegistryRole")
        self.assertNotIn("ssm:", json.dumps(role))

    def test_shared_runtime_environment_is_unchanged(self):
        env = render_runtime().resource("AgentRuntime")["Properties"]["EnvironmentVariables"]
        self.assertNotIn("SCH_OWNER_PREFIX", env)


class ManagedPolicyTest(unittest.TestCase):
    """R18: shared SCH permissions are customer managed policies."""

    ALL_ON = {
        "RuntimeCapTranscribeEnabled": "true", "RuntimeCapTextractEnabled": "true",
        "RuntimeCapRekognitionEnabled": "true", "RuntimeCapPollyEnabled": "true",
        "RuntimeCapComprehendEnabled": "true",
        "RuntimeExtraPolicyJson": '{"Version":"2012-10-17","Statement":[]}',
        "EnableSessionImageRebuild": "true",
        "EnableWorkspaceRegistry": "true", "TelegramBotToken": "t", "TelegramChatId": "1",
        "EnableTelegramInteraction": "true",
    }

    def test_shared_policies_are_managed_and_attached_to_the_shared_role(self):
        renderer = render_runtime(self.ALL_ON)
        managed = {k: v for k, v in renderer.resources().items() if v["Type"] == "AWS::IAM::ManagedPolicy"}
        self.assertEqual(len(managed), 9)
        for name, resource in managed.items():
            with self.subTest(policy=name):
                self.assertEqual(resource["Properties"]["Roles"], ["sch-dev-BedrockAgentCore-role"])
                self.assertRegex(resource["Properties"]["ManagedPolicyName"], r"^sch-dev-")
        self.assertNotIn("Policies", renderer.resource("AgentRuntimeRole")["Properties"])
        self.assertFalse([k for k, v in renderer.resources().items() if v["Type"] == "AWS::IAM::Policy"])

    def test_telegram_policy_is_managed_and_exported_apart_from_the_shared_ones(self):
        """R44: the Telegram-bound plane attaches it through its own output."""
        renderer = render_runtime(self.ALL_ON)
        policy = renderer.resource("TelegramInteractionSessionManagedPolicy")
        self.assertEqual(policy["Type"], "AWS::IAM::ManagedPolicy")
        self.assertEqual(policy["Properties"]["ManagedPolicyName"], "sch-dev-telegram-interaction-session-policy")
        arn = f"arn:aws:iam::{FAKE_ACCOUNT}:policy/sch-dev-telegram-interaction-session-policy"
        self.assertEqual(renderer.outputs()["TelegramInteractionPolicyArn"], arn)
        self.assertNotIn(arn, renderer.outputs()["SharedRuntimePolicyArns"].split(","))
        statements = {s["Sid"]: s for s in policy["Properties"]["PolicyDocument"]["Statement"]}
        self.assertEqual(statements["ConsumeOwnCommands"]["Action"], ["dynamodb:Query", "dynamodb:DeleteItem"])
        self.assertEqual(statements["PublishTopicRouting"]["Action"], ["dynamodb:PutItem"])
        for overrides in ({}, {"TelegramBotToken": "t", "TelegramChatId": "1"},
                          {"EnableTelegramInteraction": "true"}):
            with self.subTest(overrides=overrides):
                plain = render_runtime(overrides)
                self.assertIsNone(plain.resource("TelegramInteractionSessionManagedPolicy"))
                self.assertNotIn("TelegramInteractionPolicyArn", plain.outputs())

    def test_output_lists_every_shared_policy_but_telegram_in_order(self):
        renderer = render_runtime(self.ALL_ON)
        arns = renderer.outputs()["SharedRuntimePolicyArns"].split(",")
        names = [arn.rsplit("/", 1)[1] for arn in arns]
        self.assertEqual(names, [
            "sch-dev-agentcore-policy", "sch-dev-runtime-cap-transcribe", "sch-dev-runtime-cap-textract",
            "sch-dev-runtime-cap-rekognition", "sch-dev-runtime-cap-polly", "sch-dev-runtime-cap-comprehend",
            "sch-dev-runtime-extra-policy", "sch-dev-image-rebuild-session-policy",
        ])
        self.assertEqual(render_runtime().outputs()["SharedRuntimePolicyArns"],
                         f"arn:aws:iam::{FAKE_ACCOUNT}:policy/sch-dev-agentcore-policy")

    def test_base_policy_budget_covers_the_rendered_size(self):
        """X9: the estimate deploy.sh uses never under-counts."""
        for project, region in (("sch", "eu-west-1"), ("abcdefgh", "ap-southeast-3")):
            for allowlist in ("", "*anthropic.claude-sonnet*",
                              ",".join(f"eu.anthropic.claude-model-{i:02d}-v1:0" for i in range(30))):
                with self.subTest(project=project, entries=allowlist.count(",") + bool(allowlist)):
                    overrides = {"ProjectName": project, "RuntimeBedrockModelAllowlist": allowlist}
                    renderer = cfn_render.Renderer(cfn_render.load(cfn_render.RUNTIME_TEMPLATE), overrides,
                                                   region=region)
                    size = cfn_render.policy_size(
                        renderer.resource("AgentRuntimeBasePolicy")["Properties"]["PolicyDocument"])
                    entries = [e for e in allowlist.split(",") if e]
                    estimate = isolation_plan.BASE_POLICY_BUDGET + sum(
                        2 * len(e) + isolation_plan.ALLOWLIST_ENTRY_OVERHEAD for e in entries)
                    self.assertGreaterEqual(estimate, size)
                    self.assertLess(estimate - size, 600)
        default_size = cfn_render.policy_size(
            render_runtime().resource("AgentRuntimeBasePolicy")["Properties"]["PolicyDocument"])
        self.assertLess(default_size, isolation_plan.MANAGED_POLICY_LIMIT)


class BucketPolicyTest(unittest.TestCase):
    """R24: one constant bucket policy with three deny statements."""

    def render(self, **extra):
        overrides = dict(ISOLATION_ON)
        overrides.update(extra)
        return render_runtime(overrides).resource("CheckpointBucketPolicy")["Properties"]["PolicyDocument"]

    def statements(self, **extra):
        return {s["Sid"]: s for s in self.render(**extra)["Statement"]}

    def test_only_deny_statements(self):
        statements = self.render()["Statement"]
        self.assertEqual([s["Sid"] for s in statements],
                         ["DenyOwnerTreesToOthers", "DenyForeignObjectsToPlaneRoles", "DenyForeignListingToPlaneRoles"])
        self.assertTrue(all(s["Effect"] == "Deny" and s["Principal"] == "*" for s in statements))

    def test_owner_trees_denied_to_everyone_but_sch_roles(self):
        statement = self.statements()["DenyOwnerTreesToOthers"]
        self.assertEqual(statement["Action"], "s3:*")
        self.assertEqual(statement["Resource"], [
            f"arn:aws:s3:::{BUCKET}/{tree}/o.*"
            for tree in ("checkpoints", "checkpoint-generations", "workspace-writers", "builds")])
        exempt = statement["Condition"]["ArnNotLike"]["aws:PrincipalArn"]
        self.assertEqual(exempt, [
            f"arn:aws:iam::{FAKE_ACCOUNT}:role/sch-dev-o-*", REGISTRY_ROLE,
            f"arn:aws:iam::{FAKE_ACCOUNT}:role/sch-dev-task-watchdog-role"])
        exempt = self.statements(EnableSessionImageRebuild="true", EnableTaskWatchdog="false")[
            "DenyOwnerTreesToOthers"]["Condition"]["ArnNotLike"]["aws:PrincipalArn"]
        self.assertEqual(exempt[-1], f"arn:aws:iam::{FAKE_ACCOUNT}:role/sch-dev-image-rebuild-role")
        self.assertEqual(len(exempt), 3)

    def test_plane_roles_confined_to_their_tag_segment(self):
        statement = self.statements()["DenyForeignObjectsToPlaneRoles"]
        self.assertNotIn("Resource", statement)
        self.assertEqual(statement["NotResource"], [f"arn:aws:s3:::{BUCKET}"] + [
            f"arn:aws:s3:::{BUCKET}/{tree}/o.${{aws:PrincipalTag/sch-owner}}/*"
            for tree in ("checkpoints", "checkpoint-generations", "workspace-writers", "builds")])
        self.assertEqual(statement["Condition"],
                         {"ArnLike": {"aws:PrincipalArn": f"arn:aws:iam::{FAKE_ACCOUNT}:role/sch-dev-o-*"}})

    def test_listing_limited_to_own_prefixes(self):
        statement = self.statements()["DenyForeignListingToPlaneRoles"]
        self.assertEqual(statement["Action"], ["s3:ListBucket", "s3:ListBucketVersions"])
        self.assertEqual(statement["Condition"]["StringNotLike"]["s3:prefix"], [
            f"{tree}/o.${{aws:PrincipalTag/sch-owner}}/*"
            for tree in ("checkpoints", "checkpoint-generations", "workspace-writers", "builds")])

    def test_lifecycle_rules_keep_their_prefixes(self):
        """R27: both layouts stay under the same top-level prefixes."""
        rules = render_runtime(ISOLATION_ON).resource("CheckpointBucket")["Properties"][
            "LifecycleConfiguration"]["Rules"]
        prefixes = {r["Id"]: r.get("Prefix") for r in rules}
        self.assertEqual(prefixes["ExpireUnpublishedCheckpointCandidates"], "checkpoint-generations/")
        self.assertEqual(prefixes["ExpireBuildSources"], "builds/")


class SharedRuntimeLockTest(unittest.TestCase):
    """R17: with isolation on, the shared runtime serves nobody."""

    def test_two_locks_deny_everything_but_registry_stop(self):
        renderer = render_runtime(ISOLATION_ON)
        runtime_arn = f"arn:aws:bedrock-agentcore:eu-west-1:{FAKE_ACCOUNT}:runtime/sch_dev_runtime-AbCdEf1234"
        for name, arn in (("SharedRuntimeLock", runtime_arn),
                          ("SharedRuntimeEndpointLock", runtime_arn + "/runtime-endpoint/DEFAULT")):
            with self.subTest(lock=name):
                props = renderer.resource(name)["Properties"]
                self.assertEqual(props["ResourceArn"], arn)
                policy = json.loads(props["Policy"])
                self.assertTrue(all(s["Effect"] == "Deny" for s in policy["Statement"]))
                data, stop = policy["Statement"]
                self.assertEqual(data["Action"], DATA_PLANE_ACTIONS)
                self.assertEqual(data["Resource"], arn)
                self.assertNotIn("Condition", data)
                self.assertEqual(stop["Action"], "bedrock-agentcore:StopRuntimeSession")
                self.assertEqual(stop["Resource"], arn)
                self.assertEqual(stop["Condition"], {"ArnNotEquals": {"aws:PrincipalArn": REGISTRY_ROLE}})


class RegistryRoleTest(unittest.TestCase):
    """R39: least-privilege registry role."""

    def test_registry_role_keeps_only_its_grants(self):
        renderer = render_runtime(ISOLATION_ON)
        statements = renderer.resource("WorkspaceRegistryRole")["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
        by_action = {}
        for statement in statements:
            for action in as_list(statement["Action"]):
                by_action[action] = statement["Resource"]
        self.assertEqual(set(by_action), {
            "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Query", "dynamodb:UpdateItem", "dynamodb:DeleteItem",
            "s3:ListBucketVersions", "s3:DeleteObjectVersion",
            "bedrock-agentcore:StopRuntimeSession", "ssm:GetParameter",
            "logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"})
        self.assertEqual(by_action["bedrock-agentcore:StopRuntimeSession"],
                         f"arn:aws:bedrock-agentcore:eu-west-1:{FAKE_ACCOUNT}:runtime/sch_dev_*")
        self.assertEqual(by_action["ssm:GetParameter"],
                         f"arn:aws:ssm:eu-west-1:{FAKE_ACCOUNT}:parameter/sch/dev/planes/*")
        self.assertTrue(all("/aws/lambda/sch-dev-workspace-registry" in r
                            for r in by_action["logs:PutLogEvents"]))
        env = renderer.resource("WorkspaceRegistryFunction")["Properties"]["Environment"]["Variables"]
        self.assertEqual(env["ISOLATION_ENABLED"], "true")
        self.assertEqual(env["PLANE_PARAMETER_PREFIX"], "/sch/dev/planes/")

    def test_no_request_time_role_can_change_iam_agentcore_or_plane_parameters(self):
        forbidden = re.compile(
            r"^(\*|iam:(?!Get|List|Simulate).*|sts:TagSession|"
            r"bedrock-agentcore:(Create|Update|Delete|Put).*|bedrock-agentcore:\*|"
            r"ssm:(Put|Delete|Label|Add|Remove).*|ssm:\*|cloudformation:(?!Get|List|Describe).*|"
            r"lambda:(Update|Create|Delete|Put|Add|Remove).*)$")
        runtime = render_runtime(dict(ISOLATION_ON, EnableSessionImageRebuild="true",
                                      RuntimeCapTranscribeEnabled="true"))
        plane = render_plane(plane_params())
        documents = {}
        for name, resource in list(runtime.resources().items()) + [("plane." + k, v) for k, v in plane.resources().items()]:
            props = resource["Properties"]
            if resource["Type"] == "AWS::IAM::Role":
                for policy in props.get("Policies", []):
                    documents[f"{name}/{policy['PolicyName']}"] = policy["PolicyDocument"]
            elif resource["Type"] in ("AWS::IAM::ManagedPolicy", "AWS::IAM::Policy") and name != "plane.PlaneBoundary":
                documents[name] = props["PolicyDocument"]
        self.assertGreaterEqual(len(documents), 7)
        for name, document in documents.items():
            for action in all_actions(document):
                with self.subTest(policy=name, action=action):
                    self.assertIsNone(forbidden.match(action))


class PlaneTemplateTest(unittest.TestCase):
    """R13-R16, R18-R23, R25: one plane per principal."""

    KINDS = {
        "user": "AIDAEXAMPLEALICE0001",
        "sso": "AROAEXAMPLEDEVS00001:bob@example.com",
        "role": "AROAEXAMPLEBOTS00001:*",
    }

    def plane(self, kind="user", **overrides):
        return render_plane(plane_params(OwnerKind=kind, OwnerUserIdPattern=self.KINDS[kind], **overrides))

    def test_exact_resource_set(self):
        for kind in self.KINDS:
            with self.subTest(kind=kind):
                types = {k: v["Type"] for k, v in self.plane(kind).resources().items()}
                self.assertEqual(types, {
                    "PlaneBoundary": "AWS::IAM::ManagedPolicy",
                    "ExecutionRole": "AWS::IAM::Role",
                    "UserRuntime": "AWS::BedrockAgentCore::Runtime",
                    "RuntimeLock": "AWS::BedrockAgentCore::ResourcePolicy",
                    "EndpointLock": "AWS::BedrockAgentCore::ResourcePolicy",
                    "AccessRole": "AWS::IAM::Role",
                    "PlaneParameter": "AWS::SSM::Parameter",
                })

    def test_deterministic_names_fit_the_limits(self):
        renderer = self.plane(ProjectName="abcdefgh", Environment="prd")
        res = renderer.resources()
        runtime_name = res["UserRuntime"]["Properties"]["AgentRuntimeName"]
        self.assertEqual(runtime_name, f"abcdefgh_prd_o_{OWNER_KEY}")
        self.assertRegex(runtime_name, r"^[a-zA-Z][a-zA-Z0-9_]{0,47}$")
        names = {
            "ExecutionRole": f"abcdefgh-prd-o-{OWNER_KEY}-BedrockAgentCore",
            "AccessRole": f"abcdefgh-prd-o-{OWNER_KEY}-access",
        }
        for logical, name in names.items():
            self.assertEqual(res[logical]["Properties"]["RoleName"], name)
            self.assertLessEqual(len(name), 64)
            self.assertEqual(res[logical]["Properties"]["Path"], "/")
            self.assertIn({"Key": "sch-owner", "Value": OWNER_KEY}, res[logical]["Properties"]["Tags"])
        self.assertIn("BedrockAgentCore", names["ExecutionRole"])
        self.assertEqual(res["PlaneBoundary"]["Properties"]["ManagedPolicyName"],
                         f"abcdefgh-prd-o-{OWNER_KEY}-boundary")
        self.assertEqual(res["PlaneParameter"]["Properties"]["Name"], f"/abcdefgh/prd/planes/{OWNER_KEY}")

    def test_execution_role_trust_is_the_shared_trust_without_tag_session(self):
        shared = render_runtime().resource("AgentRuntimeRole")["Properties"]["AssumeRolePolicyDocument"]
        for kind in self.KINDS:
            role = self.plane(kind).resource("ExecutionRole")["Properties"]
            self.assertEqual(role["AssumeRolePolicyDocument"], shared)
            self.assertNotIn("TagSession", json.dumps(role["AssumeRolePolicyDocument"]))
            self.assertEqual(role["PermissionsBoundary"],
                             f"arn:aws:iam::{FAKE_ACCOUNT}:policy/sch-dev-o-{OWNER_KEY}-boundary")

    def test_managed_policies_come_from_the_runtime_stack(self):
        shared = render_runtime({"RuntimeCapPollyEnabled": "true"}).outputs()["SharedRuntimePolicyArns"]
        role = self.plane(SharedPolicyArns=shared).resource("ExecutionRole")["Properties"]
        self.assertEqual(role["ManagedPolicyArns"], shared.split(",") + ["arn:aws:iam::aws:policy/ReadOnlyAccess"])
        role = self.plane(SharedPolicyArns=shared, AwsApiRead="false").resource("ExecutionRole")["Properties"]
        self.assertEqual(role["ManagedPolicyArns"], shared.split(","))
        self.assertNotIn("Policies", role)

    def test_runtime_environment_is_the_shared_one_minus_telegram_plus_owner_prefix(self):
        """R21, with every optional variable of the shared runtime present."""
        shared_overrides = {"ImageDigest": DIGEST, "EnableSessionImageRebuild": "true",
                            "TelegramBotToken": "t", "TelegramChatId": "1", "EnableWorkspaceRegistry": "true",
                            "EnableTelegramInteraction": "true", "PiDefaultModel": "eu.x", "NodeHeapMb": "1024",
                            "BuildJobs": "3"}
        shared = render_runtime(shared_overrides)
        shared_env = shared.resource("AgentRuntime")["Properties"]["EnvironmentVariables"]
        plane_env = self.plane(ImageDigest=DIGEST, ImageRebuildProject=shared.outputs()["ImageRebuildProjectName"],
                               PiDefaultModel="eu.x", NodeHeapMb="1024", BuildJobs="3") \
            .resource("UserRuntime")["Properties"]["EnvironmentVariables"]
        expected = {k: str(v) for k, v in shared_env.items() if not k.startswith("SCH_TELEGRAM_")}
        expected["SCH_OWNER_PREFIX"] = f"o.{OWNER_KEY}"
        self.assertEqual({k: str(v) for k, v in plane_env.items()}, expected)
        # Without digest or rebuild the optional variables are absent, as on the shared runtime.
        plain = self.plane().resource("UserRuntime")["Properties"]["EnvironmentVariables"]
        self.assertNotIn("SCH_IMAGE_DIGEST", plain)
        self.assertNotIn("SCH_IMAGE_REBUILD_PROJECT", plain)

    TELEGRAM_SHARED = {"TelegramBotToken": "t0k", "TelegramChatId": "-100", "EnableWorkspaceRegistry": "true",
                       "EnableTelegramInteraction": "true"}
    TELEGRAM_POLICY = f"arn:aws:iam::{FAKE_ACCOUNT}:policy/sch-dev-telegram-interaction-session-policy"
    TELEGRAM_BOUND = {"TelegramBinding": "true", "TelegramBotToken": "t0k", "TelegramChatId": "-100",
                      "TelegramCommandsTable": "sch-dev-telegram-commands",
                      "TelegramRoutingTable": "sch-dev-telegram-routing", "TelegramPolicyArn": TELEGRAM_POLICY}

    def test_bound_plane_gets_the_shared_telegram_environment_policy_and_table_carve_out(self):
        """R44: the one bound plane is the shared runtime's Telegram configuration plus the owner."""
        shared = render_runtime(self.TELEGRAM_SHARED)
        shared_env = shared.resource("AgentRuntime")["Properties"]["EnvironmentVariables"]
        self.assertEqual(shared_env["SCH_TELEGRAM_COMMANDS_TABLE"], "sch-dev-telegram-commands")
        plane = self.plane(**self.TELEGRAM_BOUND)
        env = plane.resource("UserRuntime")["Properties"]["EnvironmentVariables"]
        expected = {k: str(v) for k, v in shared_env.items()}
        expected["SCH_OWNER_PREFIX"] = f"o.{OWNER_KEY}"
        self.assertEqual({k: str(v) for k, v in env.items()}, expected)
        role = plane.resource("ExecutionRole")["Properties"]
        self.assertEqual(role["ManagedPolicyArns"], [
            f"arn:aws:iam::{FAKE_ACCOUNT}:policy/sch-dev-agentcore-policy",
            "arn:aws:iam::aws:policy/ReadOnlyAccess", self.TELEGRAM_POLICY])
        role = self.plane(AwsApiRead="false", **self.TELEGRAM_BOUND).resource("ExecutionRole")["Properties"]
        self.assertEqual(role["ManagedPolicyArns"],
                         [f"arn:aws:iam::{FAKE_ACCOUNT}:policy/sch-dev-agentcore-policy", self.TELEGRAM_POLICY])
        by_sid = {s["Sid"]: s for s in plane.resource("PlaneBoundary")["Properties"]["PolicyDocument"]["Statement"]}
        self.assertEqual(by_sid["DenySchTables"]["Resource"], [
            f"arn:aws:dynamodb:*:{FAKE_ACCOUNT}:table/sch-dev-workspace-registry",
            f"arn:aws:dynamodb:*:{FAKE_ACCOUNT}:table/sch-dev-workspace-registry/*"])
        self.assertEqual(by_sid["DenySchTables"]["Effect"], "Deny")
        # Everything else of the plane is untouched by the binding.
        for logical in ("RuntimeLock", "EndpointLock", "AccessRole", "PlaneParameter"):
            self.assertEqual(plane.resource(logical), self.plane().resource(logical), logical)

    def test_bound_plane_without_the_inbound_channel_keeps_every_table_denied(self):
        notifications_only = {k: v for k, v in self.TELEGRAM_BOUND.items()
                              if k in ("TelegramBinding", "TelegramBotToken", "TelegramChatId")}
        plane = self.plane(**notifications_only)
        env = plane.resource("UserRuntime")["Properties"]["EnvironmentVariables"]
        self.assertEqual(env["SCH_TELEGRAM_BOT_TOKEN"], "t0k")
        self.assertEqual(env["SCH_TELEGRAM_CHAT_ID"], "-100")
        self.assertNotIn("SCH_TELEGRAM_COMMANDS_TABLE", env)
        self.assertNotIn("SCH_TELEGRAM_ROUTING_TABLE", env)
        self.assertEqual(plane.resource("ExecutionRole"), self.plane().resource("ExecutionRole"))
        self.assertEqual(plane.resource("PlaneBoundary"), self.plane().resource("PlaneBoundary"))

    def test_half_a_telegram_binding_is_inert(self):
        """R44: no binding flag, or no credentials, leaves the R18/R21 plane."""
        unbound = dict(self.TELEGRAM_BOUND, TelegramBinding="false")
        no_token = dict(self.TELEGRAM_BOUND, TelegramBotToken="")
        no_policy = dict(self.TELEGRAM_BOUND, TelegramPolicyArn="")
        reference = self.plane()
        for name, overrides in (("unbound", unbound), ("no_token", no_token)):
            with self.subTest(case=name):
                plane = self.plane(**overrides)
                self.assertEqual(plane.resource("UserRuntime"), reference.resource("UserRuntime"))
                self.assertEqual(plane.resource("ExecutionRole"), reference.resource("ExecutionRole"))
                self.assertEqual(plane.resource("PlaneBoundary"), reference.resource("PlaneBoundary"))
        plane = self.plane(**no_policy)
        env = plane.resource("UserRuntime")["Properties"]["EnvironmentVariables"]
        self.assertIn("SCH_TELEGRAM_BOT_TOKEN", env)
        self.assertNotIn("SCH_TELEGRAM_COMMANDS_TABLE", env)
        self.assertEqual(plane.resource("ExecutionRole"), reference.resource("ExecutionRole"))
        self.assertEqual(plane.resource("PlaneBoundary"), reference.resource("PlaneBoundary"))
        parameters = cfn_render.load(cfn_render.PLANE_TEMPLATE)["Parameters"]
        self.assertTrue(parameters["TelegramBotToken"]["NoEcho"])
        for name in ("TelegramBinding", "TelegramBotToken", "TelegramChatId", "TelegramCommandsTable",
                     "TelegramRoutingTable", "TelegramPolicyArn"):
            self.assertIn("Default", parameters[name], name)

    def test_runtime_configuration_matches_the_shared_runtime(self):
        shared = render_runtime({"ImageDigest": DIGEST}).resource("AgentRuntime")["Properties"]
        plane = self.plane(ImageDigest=DIGEST).resource("UserRuntime")["Properties"]
        for key in ("AgentRuntimeArtifact", "NetworkConfiguration", "FilesystemConfigurations",
                    "LifecycleConfiguration"):
            self.assertEqual(json.dumps(plane[key], sort_keys=True, default=str),
                             json.dumps(shared[key], sort_keys=True, default=str), key)
        self.assertEqual(plane["RoleArn"],
                         f"arn:aws:iam::{FAKE_ACCOUNT}:role/sch-dev-o-{OWNER_KEY}-BedrockAgentCore")

    def test_locks_are_deny_only_on_exact_arns(self):
        for kind, pattern in self.KINDS.items():
            renderer = self.plane(kind)
            runtime_arn = renderer.outputs()["RuntimeArn"]
            for name, arn in (("RuntimeLock", runtime_arn),
                              ("EndpointLock", runtime_arn + "/runtime-endpoint/DEFAULT")):
                with self.subTest(kind=kind, lock=name):
                    props = renderer.resource(name)["Properties"]
                    self.assertEqual(props["ResourceArn"], arn)
                    policy = json.loads(props["Policy"])
                    self.assertNotIn('"Allow"', props["Policy"])
                    data, stop = policy["Statement"]
                    self.assertEqual((data["Sid"], stop["Sid"]), ("DenyNonOwnerDataPlane", "DenyNonOwnerStop"))
                    self.assertEqual(data["Action"], DATA_PLANE_ACTIONS)
                    self.assertEqual(data["Resource"], arn)
                    self.assertEqual(data["Principal"], "*")
                    self.assertEqual(data["Condition"], {"StringNotLike": {"aws:userid": pattern}})
                    self.assertEqual(stop["Action"], "bedrock-agentcore:StopRuntimeSession")
                    self.assertEqual(stop["Resource"], arn)
                    self.assertEqual(stop["Condition"], {"StringNotLike": {"aws:userid": pattern},
                                                         "ArnNotEquals": {"aws:PrincipalArn": REGISTRY_ROLE}})

    def test_access_role_is_owner_only_and_read_only(self):
        for kind, pattern in self.KINDS.items():
            with self.subTest(kind=kind):
                props = self.plane(kind).resource("AccessRole")["Properties"]
                self.assertEqual(props["MaxSessionDuration"], 3600)
                trust = props["AssumeRolePolicyDocument"]["Statement"]
                self.assertEqual(len(trust), 1)
                self.assertEqual(trust[0]["Principal"], {"AWS": f"arn:aws:iam::{FAKE_ACCOUNT}:root"})
                self.assertEqual(trust[0]["Action"], "sts:AssumeRole")
                self.assertEqual(trust[0]["Condition"], {"StringLike": {"aws:userid": pattern}})
                statements = props["Policies"][0]["PolicyDocument"]["Statement"]
                self.assertEqual(all_actions({"Statement": statements}), ["s3:GetObject", "s3:ListBucket"])
                self.assertEqual(statements[0]["Resource"], [
                    f"arn:aws:s3:::{BUCKET}/checkpoints/o.{OWNER_KEY}/*",
                    f"arn:aws:s3:::{BUCKET}/workspace-writers/o.{OWNER_KEY}/*"])
                self.assertEqual(statements[1]["Condition"]["StringLike"]["s3:prefix"],
                                 [f"checkpoints/o.{OWNER_KEY}/*", f"workspace-writers/o.{OWNER_KEY}/*"])

    def test_plane_parameter_waits_for_both_locks(self):
        renderer = self.plane()
        parameter = renderer.resource("PlaneParameter")
        self.assertEqual(sorted(parameter["DependsOn"]), ["EndpointLock", "RuntimeLock"])
        self.assertEqual(parameter["Properties"]["Type"], "String")
        value = json.loads(parameter["Properties"]["Value"])
        self.assertEqual(value, {
            "schemaVersion": 1, "ownerKey": OWNER_KEY, "ownerPrefix": f"o.{OWNER_KEY}",
            "runtimeArn": renderer.outputs()["RuntimeArn"],
            "accessRoleArn": f"arn:aws:iam::{FAKE_ACCOUNT}:role/sch-dev-o-{OWNER_KEY}-access"})

    def test_boundary_denies_only_sch_shared_resources(self):
        statements = self.plane().resource("PlaneBoundary")["Properties"]["PolicyDocument"]["Statement"]
        self.assertEqual(statements[0], {"Sid": "AllowWithinRolePolicies", "Effect": "Allow",
                                         "Action": "*", "Resource": "*"})
        denies = statements[1:]
        self.assertTrue(all(s["Effect"] == "Deny" for s in denies))
        blob = json.dumps(denies)
        self.assertNotIn('"s3:', blob)  # R20: S3 is the bucket policy's job
        by_sid = {s["Sid"]: s for s in denies}
        self.assertEqual(by_sid["DenySchTables"]["Resource"],
                         f"arn:aws:dynamodb:*:{FAKE_ACCOUNT}:table/sch-dev-*")
        self.assertIn("logs:GetLogEvents", by_sid["DenySchLogReads"]["Action"])
        self.assertIn("logs:StartQuery", by_sid["DenySchLogReads"]["Action"])
        self.assertIn(f"arn:aws:logs:*:{FAKE_ACCOUNT}:log-group:/aws/bedrock-agentcore/runtimes/sch_dev_*",
                      by_sid["DenySchLogReads"]["Resource"])
        self.assertEqual(by_sid["DenyListFunctions"]["Resource"], "*")
        self.assertEqual(by_sid["DenySchParameters"]["Resource"], f"arn:aws:ssm:*:{FAKE_ACCOUNT}:parameter/sch/dev/*")

    def test_owner_pattern_parameter_rejects_unsafe_values(self):
        allowed = re.compile(cfn_render.load(cfn_render.PLANE_TEMPLATE)["Parameters"]["OwnerUserIdPattern"]["AllowedPattern"])
        for value in self.KINDS.values():
            self.assertTrue(allowed.match(value), value)
        for value in ('AROAEXAMPLEDEVS00001:bo"b', "AROAEXAMPLEDEVS00001:b*", "AROAEXAMPLEDEVS00001:b?b",
                      "*", "AIDA", "aidaexamplealice0001"):
            self.assertIsNone(allowed.match(value), value)


if __name__ == "__main__":
    unittest.main()
