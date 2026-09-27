#!/usr/bin/env python3
"""Evaluate the isolation policies with the IAM policy simulator.

Spec: docs/specs/security/per-principal-isolation.md, "Evidence each slice
produces" and residual risk X4 (TASK-20.3 AC #6 and #7). Read-only: the AWS
calls are iam:SimulateCustomPolicy (evaluates documents, stores nothing) and
iam:GetPolicy/GetPolicyVersion to read the current AWS managed
ReadOnlyAccess document, which plane execution roles attach.

Every document comes from the rendered templates (placeholder account
111122223333, see infra/cfn_render.py); callers are simulated in custom mode
with explicit context entries (aws:userid, aws:PrincipalArn,
aws:PrincipalTag/sch-owner, s3:prefix), because the simulator would otherwise
fill aws:userid from the caller ARN.

Usage (dev extra installed):

    .venv/bin/python infra/simulate_isolation.py [--report docs/history/isolation-evidence-simulator.md]

Exit status 1 when any decision differs from the expected one.
"""

import argparse
import json
import sys

import boto3

import cfn_render
from cfn_render import FAKE_ACCOUNT, FAKE_REGION, render_plane, render_runtime

ACCOUNT_ROOT = f"arn:aws:iam::{FAKE_ACCOUNT}:root"
SIM_CALLER = f"arn:aws:iam::{FAKE_ACCOUNT}:user/simulated-caller"
RUNTIME_OVERRIDES = {
    "EnableWorkspaceRegistry": "true",
    "IsolationEnabled": "true",
    "EnableSessionImageRebuild": "true",
    "RuntimeCapTranscribeEnabled": "true",
    "RuntimeDataBucketArn": "arn:aws:s3:::example-sch-data",
}
KEY_A = "3f9c0e2a7b1d4c65"
KEY_B = "a04be91c22d7f310"
ALICE = "AIDAEXAMPLEALICE0001"
CAROL = "AIDAEXAMPLECAROL0001"
DEVS = "AROAEXAMPLEDEVS00001"
ALLOW_ALL_AGENTCORE = json.dumps({"Version": "2012-10-17", "Statement": [
    {"Effect": "Allow", "Action": "bedrock-agentcore:*", "Resource": "*"}]})
ALLOW_ALL_S3 = json.dumps({"Version": "2012-10-17", "Statement": [
    {"Effect": "Allow", "Action": ["s3:*"], "Resource": "*"}]})

EXPECT_ALLOW = "allowed"
EXPECT_DENY = "denied"


def compact(document):
    return json.dumps(json.loads(cfn_render.policy_json(document)), separators=(",", ":"))


def context(**entries):
    out = []
    for key, value in entries.items():
        name = {"userid": "aws:userid", "arn": "aws:PrincipalArn", "tag": "aws:PrincipalTag/sch-owner",
                "prefix": "s3:prefix"}[key]
        out.append({"ContextKeyName": name, "ContextKeyValues": [value],
                    "ContextKeyType": "string"})
    return out


class World:
    """The rendered documents the cases evaluate."""

    def __init__(self, iam):
        self.runtime = render_runtime(RUNTIME_OVERRIDES)
        out = self.runtime.outputs()
        self.bucket = out["CheckpointBucketName"]
        self.registry_role = out["WorkspaceRegistryRoleArn"]
        res = self.runtime.resources()
        self.shared_policies = [
            compact(res[name]["Properties"]["PolicyDocument"])
            for name in ("AgentRuntimeBasePolicy", "ImageRebuildSessionManagedPolicy",
                         "RuntimeTranscribeCapabilityManagedPolicy")
        ]
        self.bucket_policy = compact(res["CheckpointBucketPolicy"]["Properties"]["PolicyDocument"])
        self.shared_lock = compact(res["SharedRuntimeLock"]["Properties"]["Policy"])
        self.shared_endpoint_lock = compact(res["SharedRuntimeEndpointLock"]["Properties"]["Policy"])
        self.shared_runtime_arn = res["SharedRuntimeLock"]["Properties"]["ResourceArn"]
        registry = res["WorkspaceRegistryRole"]["Properties"]["Policies"][0]["PolicyDocument"]
        self.registry_policy = compact(registry)
        policy = iam.get_policy(PolicyArn="arn:aws:iam::aws:policy/ReadOnlyAccess")["Policy"]
        self.readonly_version = policy["DefaultVersionId"]
        version = iam.get_policy_version(PolicyArn=policy["Arn"], VersionId=policy["DefaultVersionId"])
        self.readonly = json.dumps(version["PolicyVersion"]["Document"], separators=(",", ":"))
        self.planes = {
            "alice": self.plane(KEY_A, "user", ALICE),
            "bob": self.plane(KEY_B, "sso", f"{DEVS}:bob@example.com"),
        }

    def plane(self, key, kind, pattern):
        renderer = render_plane({
            "OwnerKey": key, "OwnerKind": kind, "OwnerUserIdPattern": pattern,
            "CheckpointBucket": self.bucket,
            "SharedPolicyArns": self.runtime.outputs()["SharedRuntimePolicyArns"],
            "RegistryRoleArn": self.registry_role,
            "ImageRebuildProject": self.runtime.outputs()["ImageRebuildProjectName"],
        })
        res = renderer.resources()
        return {
            "key": key,
            "runtime": renderer.outputs()["RuntimeArn"],
            "endpoint": renderer.outputs()["RuntimeArn"] + "/runtime-endpoint/DEFAULT",
            "exec_role": renderer.outputs()["ExecutionRoleArn"],
            "access_role": renderer.outputs()["AccessRoleArn"],
            "boundary": compact(res["PlaneBoundary"]["Properties"]["PolicyDocument"]),
            "access_policy": compact(res["AccessRole"]["Properties"]["Policies"][0]["PolicyDocument"]),
            "lock": compact(res["RuntimeLock"]["Properties"]["Policy"]),
            "endpoint_lock": compact(res["EndpointLock"]["Properties"]["Policy"]),
        }

    def obj(self, key):
        return f"arn:aws:s3:::{self.bucket}/{key}"

    @property
    def bucket_arn(self):
        return f"arn:aws:s3:::{self.bucket}"


def storage_cases(w):
    """R18-R24: execution role (+ ReadOnlyAccess, boundary, bucket policy)."""
    a = w.planes["alice"]
    k, other = a["key"], w.planes["bob"]["key"]
    exec_identity = w.shared_policies + [w.readonly]
    exec_ctx = dict(userid="AROAEXAMPLEEXECA0001:BedrockAgentCore-session", arn=a["exec_role"], tag=k)
    untagged_ctx = dict(userid="AROAEXAMPLEEXECA0001:s", arn=a["exec_role"])
    registry_ctx = dict(userid="AROAEXAMPLEREGISTRY1:fn", arn=w.registry_role)
    human_ctx = dict(userid="AIDAEXAMPLEHUMAN0001", arn=f"arn:aws:iam::{FAKE_ACCOUNT}:user/admin")
    access_ctx = dict(userid="AROAEXAMPLEACCESSA01:cli", arn=a["access_role"], tag=k)
    lambda_arn = f"arn:aws:lambda:{FAKE_REGION}:{FAKE_ACCOUNT}:function:sch-dev-workspace-registry"
    cases = []

    def add(label, identity, boundary, ctx, action, resource, resource_policy, expected, **extra):
        cases.append(dict(group="storage", label=label, identity=identity, boundary=boundary,
                          context=context(**dict(ctx, **extra)), action=action, resource=resource,
                          resource_policy=resource_policy, expected=expected))

    B = w.bucket_policy
    E, BD = exec_identity, [a["boundary"]]
    for tree, key in (("checkpoints", f"checkpoints/o.{k}/ws1/worktree.tar.zst"),
                      ("checkpoint-generations", f"checkpoint-generations/o.{k}/ws1/g1/worktree.tar.zst"),
                      ("workspace-writers", f"workspace-writers/o.{k}/ws1.json")):
        add(f"own plane role writes its {tree} tree", E, BD, exec_ctx, "s3:PutObject", w.obj(key), B, EXPECT_ALLOW)
        add(f"own plane role reads its {tree} tree", E, BD, exec_ctx, "s3:GetObject", w.obj(key), B, EXPECT_ALLOW)
    add("own plane role lists its checkpoints", E, BD, exec_ctx, "s3:ListBucket", w.bucket_arn, B,
        EXPECT_ALLOW, prefix=f"checkpoints/o.{k}/ws1/")
    add("own plane role uploads a build source", E, BD, exec_ctx, "s3:PutObject",
        w.obj(f"builds/o.{k}/ws1/source.zip"), B, EXPECT_ALLOW)
    add("own plane role writes to the data bucket", E, BD, exec_ctx, "s3:PutObject",
        "arn:aws:s3:::example-sch-data/transcribe/job.json", None, EXPECT_ALLOW)
    add("Bedrock model invocation still works", E, BD, exec_ctx, "bedrock:InvokeModel",
        "arn:aws:bedrock:eu-west-1::foundation-model/anthropic.claude-sonnet-4-6", None, EXPECT_ALLOW)
    add("plane role reads another owner's checkpoint", E, BD, exec_ctx, "s3:GetObject",
        w.obj(f"checkpoints/o.{other}/ws1/worktree.tar.zst"), B, EXPECT_DENY)
    add("plane role overwrites another owner's generation", E, BD, exec_ctx, "s3:PutObject",
        w.obj(f"checkpoint-generations/o.{other}/ws1/g1/x"), B, EXPECT_DENY)
    add("plane role reads another owner's writer claim", E, BD, exec_ctx, "s3:GetObject",
        w.obj(f"workspace-writers/o.{other}/ws1.json"), B, EXPECT_DENY)
    add("plane role reads another owner's build source", E, BD, exec_ctx, "s3:GetObject",
        w.obj(f"builds/o.{other}/ws1/source.zip"), B, EXPECT_DENY)
    add("plane role reads the flat (isolation-off) layout", E, BD, exec_ctx, "s3:GetObject",
        w.obj("checkpoints/ws1/worktree.tar.zst"), B, EXPECT_DENY)
    add("plane role lists another owner's checkpoints", E, BD, exec_ctx, "s3:ListBucket", w.bucket_arn, B,
        EXPECT_DENY, prefix=f"checkpoints/o.{other}/")
    add("plane role lists the bucket without a prefix", E, BD, exec_ctx, "s3:ListBucket", w.bucket_arn, B,
        EXPECT_DENY)
    add("untagged plane role reads its own tree (fail closed)", E, BD, untagged_ctx, "s3:GetObject",
        w.obj(f"checkpoints/o.{k}/ws1/worktree.tar.zst"), B, EXPECT_DENY)
    logs = f"arn:aws:logs:{FAKE_REGION}:{FAKE_ACCOUNT}:log-group:"
    ddb = f"arn:aws:dynamodb:{FAKE_REGION}:{FAKE_ACCOUNT}:table/"
    # (label, action, SCH resource, control resource outside SCH or None).
    # Each denial is paired with the same action on a non-SCH resource, which
    # ReadOnlyAccess allows: the denial is the boundary's, not a missing grant.
    for label, action, resource, control in (
        ("registry table (GetItem)", "dynamodb:GetItem", ddb + "sch-dev-workspace-registry", ddb + "other-app"),
        ("registry table (Scan)", "dynamodb:Scan", ddb + "sch-dev-workspace-registry", ddb + "other-app"),
        ("Telegram commands table", "dynamodb:Query", ddb + "sch-dev-telegram-commands", ddb + "other-app"),
        ("Telegram routing table", "dynamodb:GetItem", ddb + "sch-dev-telegram-routing", ddb + "other-app"),
        ("shared runtime log group", "logs:FilterLogEvents",
         logs + "/aws/bedrock-agentcore/runtimes/sch_dev_runtime-AbCdEf1234-DEFAULT", logs + "/app/other"),
        ("another plane's runtime log group", "logs:GetLogEvents",
         logs + f"/aws/bedrock-agentcore/runtimes/sch_dev_o_{other}-AbCdEf1234-DEFAULT:*", logs + "/app/other:*"),
        ("own runtime log group", "logs:StartLiveTail",
         logs + f"/aws/bedrock-agentcore/runtimes/sch_dev_o_{k}-AbCdEf1234-DEFAULT", logs + "/app/other"),
        ("SCH Lambda log group", "logs:StartQuery", logs + "/aws/lambda/sch-dev-workspace-registry",
         logs + "/aws/lambda/other-fn"),
        ("SCH runtime log group (/aws/bedrock/agentcore)", "logs:GetLogEvents",
         logs + "/aws/bedrock/agentcore/sch-dev-runtime:*", logs + "/app/other:*"),
        ("shared runtime configuration (environment)", "bedrock-agentcore:GetAgentRuntime", w.shared_runtime_arn,
         f"arn:aws:bedrock-agentcore:{FAKE_REGION}:{FAKE_ACCOUNT}:runtime/other_runtime-XyZ0123456"),
        ("another plane's lock policy", "bedrock-agentcore:GetResourcePolicy", w.planes["bob"]["runtime"],
         f"arn:aws:bedrock-agentcore:{FAKE_REGION}:{FAKE_ACCOUNT}:runtime/other_runtime-XyZ0123456"),
        ("SCH Lambda configuration", "lambda:GetFunctionConfiguration", lambda_arn,
         f"arn:aws:lambda:{FAKE_REGION}:{FAKE_ACCOUNT}:function:other-fn"),
        ("account-wide function listing", "lambda:ListFunctions", "*", None),
        ("plane-mapping parameter", "ssm:GetParameter",
         f"arn:aws:ssm:{FAKE_REGION}:{FAKE_ACCOUNT}:parameter/sch/dev/planes/{other}",
         f"arn:aws:ssm:{FAKE_REGION}:{FAKE_ACCOUNT}:parameter/other/app"),
    ):
        add(f"plane role reads the {label}", E, BD, exec_ctx, action, resource, None, EXPECT_DENY)
        if control:
            add(f"control: plane role, same action outside SCH ({label})", E, BD, exec_ctx, action, control,
                None, EXPECT_ALLOW)
    add("owner's access role reads its checkpoint", [a["access_policy"]], None, access_ctx, "s3:GetObject",
        w.obj(f"checkpoints/o.{k}/ws1/task-status.json"), B, EXPECT_ALLOW)
    add("owner's access role lists its checkpoints", [a["access_policy"]], None, access_ctx, "s3:ListBucket",
        w.bucket_arn, B, EXPECT_ALLOW, prefix=f"checkpoints/o.{k}/")
    add("owner's access role writes its checkpoint", [a["access_policy"]], None, access_ctx, "s3:PutObject",
        w.obj(f"checkpoints/o.{k}/ws1/task-status.json"), B, EXPECT_DENY)
    add("owner's access role reads another owner's checkpoint", [a["access_policy"]], None, access_ctx,
        "s3:GetObject", w.obj(f"checkpoints/o.{other}/ws1/task-status.json"), B, EXPECT_DENY)
    add("unlisted principal with s3:* reads an owner tree", [ALLOW_ALL_S3], None, human_ctx, "s3:GetObject",
        w.obj(f"checkpoints/o.{k}/ws1/worktree.tar.zst"), B, EXPECT_DENY)
    add("unlisted principal with s3:* deletes an owner generation", [ALLOW_ALL_S3], None, human_ctx,
        "s3:DeleteObjectVersion", w.obj(f"checkpoint-generations/o.{k}/ws1/g1/x"), B, EXPECT_DENY)
    add("registry role purges an owner tree", [w.registry_policy], None, registry_ctx, "s3:DeleteObjectVersion",
        w.obj(f"checkpoints/o.{k}/ws1/worktree.tar.zst"), B, EXPECT_ALLOW)
    return cases


DATA_PLANE = [
    "bedrock-agentcore:InvokeAgentRuntime",
    "bedrock-agentcore:InvokeAgentRuntimeForUser",
    "bedrock-agentcore:InvokeAgentRuntimeCommand",
    "bedrock-agentcore:InvokeAgentRuntimeCommandShell",
    "bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStream",
    "bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStreamForUser",
    "bedrock-agentcore:GetAgentCard",
]


def lock_cases(w):
    """R16/R17: each lock simulated on its own (X4)."""
    cases = []
    callers = {
        "owner IAM user alice": dict(userid=ALICE, arn=f"arn:aws:iam::{FAKE_ACCOUNT}:user/alice"),
        "other IAM user carol": dict(userid=CAROL, arn=f"arn:aws:iam::{FAKE_ACCOUNT}:user/carol"),
        "owner Identity Center user bob": dict(
            userid=f"{DEVS}:bob@example.com",
            arn=f"arn:aws:iam::{FAKE_ACCOUNT}:role/aws-reserved/sso.amazonaws.com/eu-west-1/AWSReservedSSO_Developers_0123456789abcdef"),
        "Identity Center user dave, same permission set": dict(
            userid=f"{DEVS}:dave@example.com",
            arn=f"arn:aws:iam::{FAKE_ACCOUNT}:role/aws-reserved/sso.amazonaws.com/eu-west-1/AWSReservedSSO_Developers_0123456789abcdef"),
        "unlisted role session": dict(userid="AROAEXAMPLEOTHER0001:botocore-session-1",
                                      arn=f"arn:aws:iam::{FAKE_ACCOUNT}:role/some-automation"),
    }
    owners = {"alice": "owner IAM user alice", "bob": "owner Identity Center user bob"}
    for plane_name, owner_label in owners.items():
        plane = w.planes[plane_name]
        for lock_name, resource, policy in (("runtime", plane["runtime"], plane["lock"]),
                                            ("endpoint", plane["endpoint"], plane["endpoint_lock"])):
            for caller_label, ctx in callers.items():
                if caller_label.startswith("owner") and caller_label != owner_label:
                    continue
                expected = EXPECT_ALLOW if caller_label == owner_label else EXPECT_DENY
                for action in DATA_PLANE + ["bedrock-agentcore:StopRuntimeSession"]:
                    cases.append(dict(
                        group="locks", label=f"{plane_name}'s plane {lock_name} lock: {caller_label}",
                        identity=[ALLOW_ALL_AGENTCORE], boundary=None, context=context(**ctx),
                        action=action, resource=resource, resource_policy=policy, expected=expected))
            registry_ctx = context(userid="AROAEXAMPLEREGISTRY1:fn", arn=w.registry_role)
            cases.append(dict(group="locks", label=f"{plane_name}'s plane {lock_name} lock: registry role stops a session",
                              identity=[w.registry_policy], boundary=None, context=registry_ctx,
                              action="bedrock-agentcore:StopRuntimeSession", resource=resource,
                              resource_policy=policy, expected=EXPECT_ALLOW))
            cases.append(dict(group="locks", label=f"{plane_name}'s plane {lock_name} lock: registry role (with agentcore:*) invokes",
                              identity=[ALLOW_ALL_AGENTCORE], boundary=None, context=registry_ctx,
                              action="bedrock-agentcore:InvokeAgentRuntime", resource=resource,
                              resource_policy=policy, expected=EXPECT_DENY))
    for lock_name, resource, policy in (("runtime", w.shared_runtime_arn, w.shared_lock),
                                        ("endpoint", w.shared_runtime_arn + "/runtime-endpoint/DEFAULT",
                                         w.shared_endpoint_lock)):
        for caller_label in ("owner IAM user alice", "unlisted role session"):
            for action in DATA_PLANE + ["bedrock-agentcore:StopRuntimeSession"]:
                cases.append(dict(group="locks", label=f"shared {lock_name} lock: {caller_label}",
                                  identity=[ALLOW_ALL_AGENTCORE], boundary=None,
                                  context=context(**callers[caller_label]), action=action, resource=resource,
                                  resource_policy=policy, expected=EXPECT_DENY))
        cases.append(dict(group="locks", label=f"shared {lock_name} lock: registry role stops a session",
                          identity=[w.registry_policy], boundary=None,
                          context=context(userid="AROAEXAMPLEREGISTRY1:fn", arn=w.registry_role),
                          action="bedrock-agentcore:StopRuntimeSession", resource=resource,
                          resource_policy=policy, expected=EXPECT_ALLOW))
    return cases


def simulate(iam, case):
    kwargs = dict(PolicyInputList=case["identity"], ActionNames=[case["action"]],
                  ResourceArns=[case["resource"]], ContextEntries=case["context"])
    if case["boundary"]:
        kwargs["PermissionsBoundaryPolicyInputList"] = case["boundary"]
    if case["resource_policy"]:
        kwargs.update(ResourcePolicy=case["resource_policy"], CallerArn=SIM_CALLER, ResourceOwner=ACCOUNT_ROOT)
    result = iam.simulate_custom_policy(**kwargs)["EvaluationResults"][0]
    decision = result["EvalDecision"]
    return decision, EXPECT_ALLOW if decision == "allowed" else EXPECT_DENY


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", default="")
    args = parser.parse_args(argv)
    iam = boto3.client("iam")
    world = World(iam)
    cases = storage_cases(world) + lock_cases(world)
    rows = {"storage": [], "locks": []}
    failures = 0
    for case in cases:
        decision, verdict = simulate(iam, case)
        ok = verdict == case["expected"]
        failures += not ok
        rows[case["group"]].append((case["label"], case["action"], case["resource"], case["expected"], decision, ok))
    lines = [
        "# Per-principal isolation: IAM policy simulator evidence",
        "",
        "> Generated by `infra/simulate_isolation.py` (TASK-20.3). Non-normative evidence for",
        "> [per-principal-isolation](../specs/security/per-principal-isolation.md). Documents rendered",
        f"> from the templates with the placeholder account `{FAKE_ACCOUNT}`; the AWS calls are",
        "> `iam:SimulateCustomPolicy` and reading the AWS managed `ReadOnlyAccess` document",
        f"> (version {world.readonly_version} at generation time). Re-run after any policy change.",
        "",
        "## Simulator limits (residual risk X4)",
        "",
        "- One resource policy per call: the runtime lock and the endpoint lock are simulated",
        "  separately, and the joint evaluation AgentCore performs is not modeled.",
        "- Custom mode with explicit context entries: `aws:userid`, `aws:PrincipalArn`,",
        "  `aws:PrincipalTag/sch-owner` and `s3:prefix` are supplied per case (the simulator would",
        "  fill `aws:userid` from the caller ARN, which cannot express an Identity Center session).",
        "  The simulated caller ARN is a placeholder IAM user required by the API whenever a",
        "  resource policy is given; the conditions evaluate the context entries.",
        "- The simulator reports `implicitDeny` for `logs:GetLogEvents` on a `:log-stream:<name>` ARN",
        "  even under `logs:Get*` on `*`; log reads are therefore simulated on `log-group:<name>:*`,",
        "  which the boundary patterns match the same way. Every boundary denial is paired with a",
        "  control case (same action outside SCH, allowed by `ReadOnlyAccess`).",
        "- Plane execution roles are simulated with the shared managed policies (base, image-rebuild",
        "  session, one capability with a data bucket), the real `ReadOnlyAccess` document, the plane",
        "  boundary and, for checkpoint-bucket objects, the bucket policy.",
        "- Only the operator-side live check (`bin/verify-isolation.sh`, two principals) covers the",
        "  joint runtime and endpoint evaluation and the real session context.",
        "",
    ]
    titles = {"storage": "Execution role, boundary, access role and bucket policy (R18-R24)",
              "locks": "Runtime and endpoint locks (R16, R17); every caller holds bedrock-agentcore:* on *"}
    for group in ("storage", "locks"):
        lines += [f"## {titles[group]}", "",
                  "| Case | Action | Resource | Expected | Simulator | OK |", "| --- | --- | --- | --- | --- | --- |"]
        for label, action, resource, expected, decision, ok in rows[group]:
            lines.append(f"| {label} | `{action}` | `{resource}` | {expected} | {decision} | {'yes' if ok else 'NO'} |")
        lines.append("")
    lines.append(f"Result: {'FAIL' if failures else 'PASS'} ({len(cases)} cases, {failures} unexpected).")
    report = "\n".join(lines) + "\n"
    if args.report:
        with open(args.report, "w") as fh:
            fh.write(report)
    sys.stdout.write(report)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
