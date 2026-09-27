#!/usr/bin/env python3
"""Validate every isolation policy document with IAM Access Analyzer.

Spec: docs/specs/security/per-principal-isolation.md, "Evidence each slice
produces" (TASK-20.3 AC #5). Read-only: the only AWS call is
access-analyzer:ValidatePolicy, which checks a document and stores nothing.

The documents are rendered from infra/agent_runtime.yaml (isolation on, every
optional feature that adds a policy switched on) and infra/user_plane.yaml
(one plane per entry kind) with the placeholder account 111122223333, so the
report contains no real account data.

Usage (dev extra installed: PyYAML, boto3; credentials allowed to call
access-analyzer:ValidatePolicy):

    .venv/bin/python infra/validate_isolation_policies.py [--region eu-west-1] \
        [--report docs/history/isolation-evidence-access-analyzer.md]

Exit status 1 when any document has an ERROR finding.
"""

import argparse
import json
import sys
from collections import Counter

import boto3

import cfn_render
from cfn_render import FAKE_ACCOUNT, render_plane, render_runtime

RUNTIME_OVERRIDES = {
    "EnableWorkspaceRegistry": "true",
    "IsolationEnabled": "true",
    "EnableSessionImageRebuild": "true",
    "EnableTaskWatchdog": "true",
    "RuntimeCapTranscribeEnabled": "true",
    "RuntimeCapTextractEnabled": "true",
    "RuntimeCapRekognitionEnabled": "true",
    "RuntimeCapPollyEnabled": "true",
    "RuntimeCapComprehendEnabled": "true",
    "RuntimeDataBucketArn": "arn:aws:s3:::example-sch-data",
    "RuntimeExtraPolicyJson": json.dumps({"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": "translate:TranslateText", "Resource": "*"}]}),
}
ALLOWLIST_OVERRIDES = dict(RUNTIME_OVERRIDES, RuntimeBedrockModelAllowlist="*anthropic.claude-sonnet*,openai.gpt-5.5",
                           RuntimeBedrockAllowlistHasOpenAIFamily="true")

PLANE_KINDS = {
    "user": ("3f9c0e2a7b1d4c65", "AIDAEXAMPLEALICE0001"),
    "sso": ("a04be91c22d7f310", "AROAEXAMPLEDEVS00001:bob@example.com"),
    "role": ("5b2d7e1c9a0f3e84", "AROAEXAMPLEBOTS00001:*"),
}


def plane_renderer(runtime, kind):
    key, pattern = PLANE_KINDS[kind]
    outputs = runtime.outputs()
    return render_plane({
        "OwnerKey": key, "OwnerKind": kind, "OwnerUserIdPattern": pattern,
        "PrincipalEntry": kind, "CheckpointBucket": outputs["CheckpointBucketName"],
        "SharedPolicyArns": outputs["SharedRuntimePolicyArns"],
        "RegistryRoleArn": outputs["WorkspaceRegistryRoleArn"],
        "ImageRebuildProject": outputs["ImageRebuildProjectName"],
    })


def collect():
    """[(label, policy type, resource type or None, document string)]."""
    docs = []
    runtime = render_runtime(RUNTIME_OVERRIDES)
    allowlisted = render_runtime(ALLOWLIST_OVERRIDES)
    for logical, resource in runtime.resources().items():
        props = resource["Properties"]
        kind = resource["Type"]
        if kind == "AWS::IAM::ManagedPolicy":
            docs.append((f"runtime/{logical}", "IDENTITY_POLICY", None, props["PolicyDocument"]))
        elif kind == "AWS::IAM::Role":
            docs.append((f"runtime/{logical} (trust)", "RESOURCE_POLICY", "AWS::IAM::AssumeRolePolicyDocument",
                         props["AssumeRolePolicyDocument"]))
            for policy in props.get("Policies", []):
                docs.append((f"runtime/{logical}/{policy['PolicyName']}", "IDENTITY_POLICY", None,
                             policy["PolicyDocument"]))
        elif kind == "AWS::S3::BucketPolicy":
            docs.append((f"runtime/{logical}", "RESOURCE_POLICY", "AWS::S3::Bucket", props["PolicyDocument"]))
        elif kind == "AWS::BedrockAgentCore::ResourcePolicy":
            docs.append((f"runtime/{logical}", "RESOURCE_POLICY", None, props["Policy"]))
    docs.append(("runtime/AgentRuntimeBasePolicy (allow-list variant)", "IDENTITY_POLICY", None,
                 allowlisted.resource("AgentRuntimeBasePolicy")["Properties"]["PolicyDocument"]))
    for kind in PLANE_KINDS:
        plane = plane_renderer(runtime, kind)
        for logical, resource in plane.resources().items():
            props = resource["Properties"]
            label = f"plane[{kind}]/{logical}"
            if resource["Type"] == "AWS::IAM::ManagedPolicy":
                docs.append((label + " (permissions boundary)", "IDENTITY_POLICY", None, props["PolicyDocument"]))
            elif resource["Type"] == "AWS::IAM::Role":
                docs.append((label + " (trust)", "RESOURCE_POLICY", "AWS::IAM::AssumeRolePolicyDocument",
                             props["AssumeRolePolicyDocument"]))
                for policy in props.get("Policies", []):
                    docs.append((label, "IDENTITY_POLICY", None, policy["PolicyDocument"]))
            elif resource["Type"] == "AWS::BedrockAgentCore::ResourcePolicy":
                docs.append((label, "RESOURCE_POLICY", None, props["Policy"]))
    return [(label, ptype, rtype, cfn_render.policy_json(doc)) for label, ptype, rtype, doc in docs]


def validate(client, policy_type, resource_type, document):
    kwargs = {"policyDocument": document, "policyType": policy_type}
    if resource_type:
        kwargs["validatePolicyResourceType"] = resource_type
    findings = []
    for page in client.get_paginator("validate_policy").paginate(**kwargs):
        findings.extend(page["findings"])
    return findings


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default="eu-west-1")
    parser.add_argument("--report", default="")
    args = parser.parse_args(argv)
    client = boto3.client("accessanalyzer", region_name=args.region)
    lines = [
        "# Per-principal isolation: IAM Access Analyzer evidence",
        "",
        "> Generated by `infra/validate_isolation_policies.py` (TASK-20.3). Non-normative evidence for",
        "> [per-principal-isolation](../specs/security/per-principal-isolation.md). Documents rendered",
        f"> from the templates with the placeholder account `{FAKE_ACCOUNT}`; the only AWS call is",
        "> `access-analyzer:ValidatePolicy`. Re-run after any policy change.",
        "",
        "Runtime stack rendered with isolation, registry, watchdog, in-session image rebuild, all five",
        "capabilities, a data bucket and an escape-hatch policy; the base policy also with a Bedrock",
        "allow-list. One plane per entry kind (`user`, `sso`, `role`).",
        "",
        "| Document | Type | ERROR | SECURITY_WARNING | WARNING | SUGGESTION |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    details = []
    errors = 0
    for label, policy_type, resource_type, document in collect():
        findings = validate(client, policy_type, resource_type, document)
        counts = Counter(f["findingType"] for f in findings)
        errors += counts.get("ERROR", 0)
        shown_type = policy_type + (f" ({resource_type})" if resource_type else "")
        lines.append(f"| {label} | {shown_type} | {counts.get('ERROR', 0)} | {counts.get('SECURITY_WARNING', 0)}"
                     f" | {counts.get('WARNING', 0)} | {counts.get('SUGGESTION', 0)} |")
        for finding in findings:
            details.append(f"- **{label}**: {finding['findingType']} `{finding['issueCode']}`: "
                           f"{finding['findingDetails']}")
    lines += ["", "## Findings", ""]
    lines += details or ["None."]
    lines += [
        "",
        "## Notes",
        "",
        "- The permissions boundary's `Allow *` is a ceiling, not a grant (R20): effective permissions",
        "  are the intersection with the role's own policies, which grant neither `iam:PassRole` nor",
        "  `iam:CreateServiceLinkedRole`. The two boundary warnings are inherent to that shape.",
        "- The `REDUNDANT_RESOURCE` suggestion on the image-rebuild session policy predates TASK-20.3",
        "  (the statement was moved unchanged into a managed policy).",
    ]
    lines += ["", f"Result: {'FAIL' if errors else 'PASS'} ({errors} ERROR finding(s))."]
    report = "\n".join(lines) + "\n"
    if args.report:
        with open(args.report, "w") as fh:
            fh.write(report)
    sys.stdout.write(report)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
