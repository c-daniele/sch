#!/usr/bin/env python3
"""Deploy-time planning of per-principal isolation (infra/deploy.sh helper).

Spec: docs/specs/security/per-principal-isolation.md (R1-R7, R10, R12, R44, X9).

`infra/deploy.sh` calls this helper before it changes any stack:

    python3 infra/isolation_plan.py plan --out <file>
    python3 infra/isolation_plan.py policy-size

`plan` parses ISOLATED_PRINCIPALS, resolves every entry to its bound identity
with IAM read calls, computes the owner keys, finds the existing plane stacks
of the deployment (name prefix AND sch:deployment tag), checks the AgentCore
runtime quota, and writes one tab-separated line per plane to create or
update and per orphan plane to delete. The plane line ends with the Telegram
binding (`true` on the one plane TELEGRAM_PRINCIPAL names, R44). Any error
exits non-zero before the deploy has touched anything.

`policy-size` checks that the customer managed policies the deploy is about
to create stay within the 6,144-character IAM limit (residual risk X9).

Stdlib only; every AWS call goes through the `aws` CLI found on PATH, like the
rest of deploy.sh, and is described as (service, operation, parameters) so
the tests can validate each request against the botocore service model.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

SSO_PATH_PREFIX = "/aws-reserved/sso.amazonaws.com/"
DEFAULT_RUNTIME_QUOTA = 100
# Service Quotas: "Total Agents per Account" of bedrock-agentcore, the quota
# that bounds the number of agent runtimes (read 2026-09-27; its AWS default
# then was 1000, the spec keeps the documented 100 as the fallback).
RUNTIME_QUOTA_CODE = "L-F4575653"
MANAGED_POLICY_LIMIT = 6144

# IAM names (users, roles): [\w+=,.@-]{1,64}. Permission-set names are at most
# 32 characters. Identity Center usernames are restricted to a set that keeps
# the lock policies' JSON well-formed and contains no IAM wildcard (* or ?).
IAM_NAME = re.compile(r"^[A-Za-z0-9_+=,.@-]{1,64}$")
PERMISSION_SET = re.compile(r"^[A-Za-z0-9_+=,.@-]{1,32}$")
SSO_USERNAME = re.compile(r"^[A-Za-z0-9._@+=,-]{1,64}$")
UNIQUE_ID = re.compile(r"^A[A-Z0-9]{15,127}$")
OWNER_KEY = re.compile(r"^[0-9a-f]{16}$")

# Base-policy size budget (X9). Measured on the rendered AgentRuntimeBasePolicy
# with the longest project name and region (infra/test_isolation_plan.py
# keeps it honest): the document without any Bedrock classic-plane statement,
# plus the fixed part of the allow-list statement; each allow-list entry adds
# its two ARN forms.
BASE_POLICY_BUDGET = 2300
ALLOWLIST_ENTRY_OVERHEAD = len("arn:aws:bedrock:*:*:inference-profile/") + len(
    "arn:aws:bedrock:*::foundation-model/") + 6


class PlanError(Exception):
    """A problem that must stop the deploy before any stack changes."""


# --- AWS access ------------------------------------------------------------

def _kebab(name):
    """CamelCase to the aws CLI spelling (GetAWSDefaultServiceQuota ->
    get-aws-default-service-quota), as botocore's xform_name does."""
    name = re.sub(r"(?<=[A-Z])([A-Z][a-z])", r"-\1", name)
    return re.sub(r"(?<=[a-z0-9])([A-Z])", r"-\1", name).lower()


def cli_args(service, operation, params):
    """Translates a (service, operation, params) request into aws CLI argv."""
    args = [service, _kebab(operation)]
    for key, value in params.items():
        args.append("--" + _kebab(key))
        args.append(str(value))
    return args


class Aws:
    """Runs requests through the aws CLI; records them for the tests."""

    def __init__(self, region):
        self.region = region
        self.requests = []

    def call(self, service, operation, **params):
        self.requests.append((service, operation, dict(params)))
        argv = ["aws", *cli_args(service, operation, params),
                "--region", self.region, "--output", "json"]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True)
        except FileNotFoundError:
            raise PlanError("the aws CLI is not installed or not on PATH")
        if proc.returncode != 0:
            raise AwsError(service, operation, proc.stderr.strip())
        out = proc.stdout.strip()
        return json.loads(out) if out else {}


class AwsError(Exception):
    def __init__(self, service, operation, stderr):
        super().__init__(f"{service} {operation}: {stderr}")
        self.stderr = stderr

    @property
    def not_found(self):
        return any(code in self.stderr for code in (
            "NoSuchEntity", "does not exist", "NotFound", "ResourceNotFound"))


# --- entries and owners -------------------------------------------------------

class Entry:
    def __init__(self, text, kind, name, username=""):
        self.text = text
        self.kind = kind
        self.name = name
        self.username = username
        self.bound_id = ""
        self.pattern = ""
        self.role_id = ""
        self.warning = ""

    @property
    def owner(self):
        return f"{self.kind}:{self.bound_id}"

    @property
    def owner_key(self):
        return owner_key(self.owner)


def owner_id(owner):
    """R7: sha256 of the owner string, 64 lowercase hex characters."""
    return hashlib.sha256(owner.encode("utf-8")).hexdigest()


def owner_key(owner):
    """R7: first 16 hex characters of the owner ID."""
    return owner_id(owner)[:16]


def parse_entries(raw):
    """R1/R3: the comma list; malformed entries fail (R4)."""
    entries = []
    for text in (raw or "").split(","):
        text = text.strip()
        if not text:
            continue
        kind, sep, rest = text.partition(":")
        if not sep:
            raise PlanError(f"entry {text!r}: expected user:<name>, sso:<permission-set>/<username> or role:<name>")
        if kind == "user" and IAM_NAME.match(rest):
            entries.append(Entry(text, "user", rest))
        elif kind == "role" and IAM_NAME.match(rest):
            entries.append(Entry(text, "role", rest))
        elif kind == "sso":
            permission_set, slash, username = rest.partition("/")
            if not slash or not PERMISSION_SET.match(permission_set) or not SSO_USERNAME.match(username):
                raise PlanError(
                    f"entry {text!r}: expected sso:<permission-set>/<username>; usernames may"
                    " contain letters, digits and . _ @ + = , -")
            entries.append(Entry(text, "sso", permission_set, username))
        else:
            raise PlanError(f"entry {text!r}: expected user:<name>, sso:<permission-set>/<username> or role:<name>")
    return entries


def _unique_id(value, what):
    if not isinstance(value, str) or not UNIQUE_ID.match(value):
        raise PlanError(f"{what}: unexpected unique ID {value!r}")
    return value


def resolve_entries(entries, aws):
    """R3/R4/R5: IAM reads only; unknown or ambiguous entries fail."""
    sso_roles = None
    for entry in entries:
        if entry.kind == "user":
            try:
                user = aws.call("iam", "GetUser", UserName=entry.name)["User"]
            except AwsError as exc:
                if exc.not_found:
                    raise PlanError(f"entry {entry.text!r}: IAM user {entry.name} does not exist")
                raise PlanError(f"entry {entry.text!r}: {exc}")
            entry.bound_id = _unique_id(user.get("UserId"), entry.text)
            entry.pattern = entry.bound_id
        elif entry.kind == "role":
            try:
                role = aws.call("iam", "GetRole", RoleName=entry.name)["Role"]
            except AwsError as exc:
                if exc.not_found:
                    raise PlanError(f"entry {entry.text!r}: IAM role {entry.name} does not exist")
                raise PlanError(f"entry {entry.text!r}: {exc}")
            if (role.get("Path") or "/").startswith(SSO_PATH_PREFIX):
                raise PlanError(
                    f"entry {entry.text!r}: {entry.name} is an IAM Identity Center role;"
                    " list its users as sso:<permission-set>/<username>")
            entry.role_id = entry.bound_id = _unique_id(role.get("RoleId"), entry.text)
            entry.pattern = f"{entry.bound_id}:*"
        else:
            if sso_roles is None:
                try:
                    sso_roles = aws.call("iam", "ListRoles", PathPrefix=SSO_PATH_PREFIX).get("Roles", [])
                except AwsError as exc:
                    raise PlanError(f"cannot list the IAM Identity Center roles: {exc}")
            name_pattern = re.compile(r"^AWSReservedSSO_" + re.escape(entry.name) + r"_[0-9a-f]{16}$")
            matches = [r for r in sso_roles
                       if name_pattern.match(r.get("RoleName", ""))
                       and (r.get("Path") or "").startswith(SSO_PATH_PREFIX)]
            if not matches:
                raise PlanError(
                    f"entry {entry.text!r}: no IAM Identity Center role for permission set"
                    f" {entry.name} (AWSReservedSSO_{entry.name}_<hex>); is it provisioned in this account?")
            if len(matches) > 1:
                names = ", ".join(sorted(r["RoleName"] for r in matches))
                raise PlanError(f"entry {entry.text!r}: permission set {entry.name} matches several roles: {names}")
            entry.role_id = _unique_id(matches[0].get("RoleId"), entry.text)
            entry.bound_id = f"{entry.role_id}:{entry.username}"
            entry.pattern = entry.bound_id
            # R5/X3: the username cannot be verified without identitystore.
            entry.warning = (
                f"entry {entry.text!r}: the Identity Center username {entry.username!r} was not"
                " verified; it must match the user's session name exactly (case-sensitive)")
    seen = {}
    for entry in entries:
        if entry.owner in seen:
            raise PlanError(f"entries {seen[entry.owner].text!r} and {entry.text!r} resolve to the same identity")
        seen[entry.owner] = entry
    sso_role_ids = {e.role_id: e for e in entries if e.kind == "sso"}
    for entry in entries:
        if entry.kind == "role" and entry.role_id in sso_role_ids:
            raise PlanError(
                f"entries {sso_role_ids[entry.role_id].text!r} and {entry.text!r} resolve to the same role")
    keys = {}
    for entry in entries:
        if entry.owner_key in keys:
            raise PlanError(f"entries {keys[entry.owner_key].text!r} and {entry.text!r} collide on owner key {entry.owner_key}")
        keys[entry.owner_key] = entry
    return entries


# --- existing planes and quota --------------------------------------------------

def plane_stack_prefix(project, env):
    return f"{project}-{env}-plane-"


def discover_planes(aws, project, env):
    """R12: stacks named <project>-<env>-plane-* AND tagged with the deployment.

    Returns {ownerKey: (stackName, status)}.
    """
    prefix = plane_stack_prefix(project, env)
    deployment = f"{project}-{env}"
    summaries = aws.call("cloudformation", "ListStacks").get("StackSummaries", [])
    planes = {}
    for summary in summaries:
        name = summary.get("StackName", "")
        status = summary.get("StackStatus", "")
        if not name.startswith(prefix) or status == "DELETE_COMPLETE":
            continue
        try:
            stacks = aws.call("cloudformation", "DescribeStacks", StackName=name).get("Stacks", [])
        except AwsError as exc:
            if exc.not_found:
                continue
            raise
        if not stacks:
            continue
        tags = {t.get("Key"): t.get("Value") for t in stacks[0].get("Tags", [])}
        if tags.get("sch:deployment") != deployment:
            continue
        key = tags.get("sch:owner-key", "")
        if not OWNER_KEY.match(key) or name != prefix + key:
            continue
        planes[key] = (name, stacks[0].get("StackStatus", status))
    return planes


def runtime_quota(aws):
    """R6: the Service Quotas value when readable, else the documented 100."""
    for operation in ("GetServiceQuota", "GetAWSDefaultServiceQuota"):
        try:
            quota = aws.call("service-quotas", operation, ServiceCode="bedrock-agentcore",
                             QuotaCode=RUNTIME_QUOTA_CODE)
        except AwsError:
            continue
        value = (quota.get("Quota") or {}).get("Value")
        if isinstance(value, (int, float)) and value > 0:
            return int(value), "service quotas"
    return DEFAULT_RUNTIME_QUOTA, "documented default"


def existing_runtimes(aws):
    try:
        runtimes = aws.call("bedrock-agentcore-control", "ListAgentRuntimes").get("agentRuntimes", [])
    except AwsError as exc:
        raise PlanError(f"cannot count the AgentCore runtimes of the region for the quota check: {exc}")
    return [r.get("agentRuntimeName", "") for r in runtimes]


# --- plan ---------------------------------------------------------------------

def telegram_configured(env):
    return bool(env.get("TELEGRAM_BOT_TOKEN") or env.get("TELEGRAM_CHAT_ID")
                or env.get("ENABLE_TELEGRAM_INTERACTION", "false") == "true")


def check_switches(env):
    """R2, R6 and R44 (Telegram binding); returns the raw allow-list."""
    raw = env.get("ISOLATED_PRINCIPALS", "")
    principal = env.get("TELEGRAM_PRINCIPAL", "").strip()
    if not raw.strip():
        if principal:
            raise PlanError(
                "TELEGRAM_PRINCIPAL is set but ISOLATED_PRINCIPALS is empty: Telegram binds to a"
                " listed principal only with isolation on (per-principal-isolation R44); unset it")
        return ""
    if env.get("ENABLE_WORKSPACE_REGISTRY", "false") != "true":
        raise PlanError("ISOLATED_PRINCIPALS requires ENABLE_WORKSPACE_REGISTRY=true")
    if telegram_configured(env) and not principal:
        raise PlanError(
            "Telegram on an isolated stack binds to one listed principal (per-principal-isolation"
            " R44): set TELEGRAM_PRINCIPAL to one ISOLATED_PRINCIPALS entry, or unset"
            " TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID and ENABLE_TELEGRAM_INTERACTION")
    if principal and not telegram_configured(env):
        raise PlanError("TELEGRAM_PRINCIPAL requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
    return raw


def telegram_entry(env, entries):
    """R44: the listed entry TELEGRAM_PRINCIPAL names, or None without Telegram.

    Matched on the entry text after the same parsing as ISOLATED_PRINCIPALS,
    so `user: alice` and `user:alice` are the same entry; the match happens
    before any AWS call."""
    principal = env.get("TELEGRAM_PRINCIPAL", "").strip()
    if not principal or not telegram_configured(env):
        return None
    parsed = parse_entries(principal)
    if len(parsed) != 1:
        raise PlanError(f"TELEGRAM_PRINCIPAL must name exactly one entry, got {principal!r}")
    for entry in entries:
        if entry.text == parsed[0].text:
            return entry
    listed = ", ".join(entry.text for entry in entries)
    raise PlanError(
        f"TELEGRAM_PRINCIPAL {parsed[0].text!r} is not in ISOLATED_PRINCIPALS ({listed});"
        " Telegram binds to a listed principal (per-principal-isolation R44)")


def build_plan(env, aws, warn=print):
    project = env.get("PROJECT_NAME", "sch")
    environment = env.get("ENVIRONMENT", "dev")
    raw = check_switches(env)
    parsed = parse_entries(raw) if raw else []
    bound = telegram_entry(env, parsed)
    entries = resolve_entries(parsed, aws) if raw else []
    for entry in entries:
        if entry.warning:
            warn(f"deploy: WARNING — {entry.warning}")
    existing = discover_planes(aws, project, environment)
    listed = {entry.owner_key: entry for entry in entries}
    orphans = {key: value for key, value in existing.items() if key not in listed}
    if entries:
        names = existing_runtimes(aws)
        to_create = sum(1 for key in listed
                        if key not in existing or existing[key][1] == "ROLLBACK_COMPLETE")
        shared_missing = f"{project}_{environment}_runtime" not in names
        needed = len(names) + to_create - len(orphans) + (1 if shared_missing else 0)
        quota, source = runtime_quota(aws)
        if needed > quota:
            raise PlanError(
                f"AgentCore runtime quota: {needed} runtimes needed ({len(names)} existing,"
                f" {to_create} planes to create, {len(orphans)} to delete"
                f"{', 1 shared runtime to create' if shared_missing else ''}) but the quota is"
                f" {quota} ({source}); request an increase or list fewer principals")
    lines = []
    for entry in entries:
        stack = plane_stack_prefix(project, environment) + entry.owner_key
        status = existing.get(entry.owner_key, (stack, "NEW"))[1]
        lines.append("\t".join(["PLANE", entry.text, entry.kind, entry.owner_key,
                                entry.pattern, stack, status,
                                "true" if entry is bound else "false"]))
    for key, (stack, status) in sorted(orphans.items()):
        lines.append("\t".join(["ORPHAN", stack, key, status]))
    return lines


# --- managed-policy size (X9) -------------------------------------------------------

def policy_size_errors(env):
    errors = []
    entries = [e.strip() for e in env.get("RUNTIME_BEDROCK_MODEL_ALLOWLIST", "").split(",") if e.strip()]
    estimate = BASE_POLICY_BUDGET + sum(2 * len(e) + ALLOWLIST_ENTRY_OVERHEAD for e in entries)
    if estimate > MANAGED_POLICY_LIMIT:
        errors.append(
            f"RUNTIME_BEDROCK_MODEL_ALLOWLIST is too long for the base managed policy: about"
            f" {estimate} of {MANAGED_POLICY_LIMIT} characters; use wildcard entries"
            " (for example *anthropic.claude-sonnet*) instead of many exact IDs")
    extra = env.get("RUNTIME_EXTRA_POLICY_JSON", "")
    if extra:
        try:
            size = len(re.sub(r"\s+", "", json.dumps(json.loads(extra))))
        except ValueError:
            errors.append("RUNTIME_EXTRA_POLICY_JSON is not valid JSON")
        else:
            if size > MANAGED_POLICY_LIMIT:
                errors.append(
                    f"RUNTIME_EXTRA_POLICY_JSON has {size} characters without whitespace; a"
                    f" managed policy holds at most {MANAGED_POLICY_LIMIT}")
    return errors


def main(argv=None, env=None):
    env = dict(os.environ if env is None else env)
    parser = argparse.ArgumentParser(prog="isolation_plan.py")
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--out", required=True)
    sub.add_parser("policy-size")
    args = parser.parse_args(argv)
    if args.command == "policy-size":
        errors = policy_size_errors(env)
        for error in errors:
            print(f"deploy: {error}", file=sys.stderr)
        return 2 if errors else 0
    aws = Aws(env.get("REGION", "eu-west-1"))
    try:
        lines = build_plan(env, aws, warn=lambda msg: print(msg, file=sys.stderr))
    except (PlanError, AwsError) as exc:
        print(f"deploy: isolation preflight failed: {exc}", file=sys.stderr)
        print("  no stack has been changed", file=sys.stderr)
        return 2
    with open(args.out, "w") as fh:
        fh.write("".join(line + "\n" for line in lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
