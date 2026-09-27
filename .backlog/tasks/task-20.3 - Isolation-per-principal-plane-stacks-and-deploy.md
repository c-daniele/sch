---
id: TASK-20.3
title: 'Isolation: per-principal plane stacks and deploy'
status: Done
assignee: []
created_date: '2026-09-27 14:50'
updated_date: '2026-09-27 16:40'
labels:
  - security
dependencies:
  - TASK-20.2
parent_task_id: TASK-20
priority: high
type: feature
ordinal: 17000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Slice 3 of TASK-20. The plane template, runtime-template changes (shared managed policies, bucket policy, shared-runtime lock, least-privilege registry role), infra/deploy.sh (switch, entry resolution, quota preflight, per-principal stacks, image and configuration updates, orphan deletion), sch destroy, template tests and read-only Access Analyzer and simulator scripts. Context, decisions, defaults, lessons and constraints live in the parent TASK-20; read it first and follow it. Headless run: apply the defaults, record every assumption in the implementation notes, and stop if the dependency is not Done. Work on `feat/task-20`, commit at the end, never push.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 With the allow-list unset, a deploy creates no new resource; the default-rendering tests pass
- [x] #2 With the allow-list set, infra/deploy.sh resolves every entry to its bound identity, refuses unknown or ambiguous entries before changing any stack, warns on unverified sso usernames, checks the AgentCore runtime quota, and keeps exactly one plane stack per listed principal (created, updated on every deploy, deleted when the entry is removed)
- [x] #3 Runtime and DEFAULT endpoint locks are AWS::BedrockAgentCore::ResourcePolicy resources in the plane stack; the shared runtime denies user data-plane actions when isolation is on
- [x] #4 The registry role keeps only its table, read-only access to the plane-mapping parameters, checkpoint purge and StopRuntimeSession on the deployment runtimes, and no role used at request time can create or change IAM, AgentCore runtimes, resource policies or plane parameters
- [x] #5 Every new or changed policy document validates with IAM Access Analyzer without ERROR findings, and a committed read-only script reproduces the check
- [x] #6 Committed simulator script and redacted output show that an owner execution role reads and writes only its own storage (including through ReadOnlyAccess), still uploads build sources and writes to the data bucket, and is denied other owners storage, the registry table, the Telegram tables and other SCH runtime log groups
- [x] #7 Simulator evidence shows each user runtime and endpoint policy denies every data-plane action to another IAM user, another Identity Center user of the same permission set and an unlisted role session, all holding bedrock-agentcore:* on *, and allows the owner (IAM user and Identity Center cases); the simulator limits are recorded next to the evidence
- [x] #8 User runtimes receive image and configuration changes during deploy only, and sch destroy removes every plane stack before the runtime stack
<!-- AC:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [x] #1 Relevant suites pass (cli, infra, tunnel, image-side tests with sandboxed paths) and bin/verify-docs.sh when docs changed
- [x] #2 Implementation notes list every assumption and the verification results; a journal entry is created and MASTERPLAN.md updated as AGENTS.md requires
- [x] #3 Work committed on feat/task-20 with conventional commits, never pushed
<!-- DOD:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
Spec: docs/specs/security/per-principal-isolation.md (R1-R6, R10-R25, R27, R39, R44-R48). Decision: decision-14. Check first that TASK-20.2 is Done.

1. Runtime template (infra/agent_runtime.yaml).
   - New parameter IsolationEnabled (default 'false'); condition IsolationOn = WorkspaceRegistryEnabled AND IsolationEnabled.
   - Move the base inline policy of AgentRuntimeRole into AWS::IAM::ManagedPolicy AgentRuntimeBasePolicy; turn the capability, escape-hatch and image-rebuild session policies into AWS::IAM::ManagedPolicy (same statements, same conditions, Roles: [AgentRuntimeRole]). TelegramInteractionSessionPolicy stays an inline AWS::IAM::Policy. Output SharedRuntimePolicyArns (comma list of the policies that exist) and WorkspaceRegistryRoleArn.
   - Under IsolationOn: CheckpointBucketPolicy with the three statements of R24 (plane-role pattern role/<p>-<e>-o-*, exemptions registry, watchdog and image-rebuild roles when they exist); shared-runtime lock with two AWS::BedrockAgentCore::ResourcePolicy (runtime ARN and <arn>/runtime-endpoint/DEFAULT) per R17.
   - Registry role (R39): StopRuntimeSession scoped to runtime/<p>_<e>_*; under IsolationOn add ssm:GetParameter on parameter/<p>/<e>/planes/*; registry env ISOLATION_ENABLED and PLANE_PARAMETER_PREFIX.
   - Amend runtime-capability-tuning R1/I1/R3/R10 and runtime-provisioning R8 in the same change (planned-change notes become the requirement text).
2. Plane template infra/user_plane.yaml (R13-R16, R18-R23). Parameters: ProjectName, Environment, OwnerKey, OwnerKind, OwnerUserIdPattern, PrincipalEntry, image reference (ContainerUri or digest parts, ApplicationVersion), runtime configuration (idle timeout, max lifetime, PiDefaultModel, NodeHeapMb, BuildJobs), CheckpointBucket, SharedPolicyArns (CommaDelimitedList), AwsApiRead, ImageRebuildProject, RegistryRoleArn. Resources: boundary ManagedPolicy, execution role (trust as shared, no TagSession, boundary, managed policies, tag sch-owner), runtime (env of R21), two ResourcePolicy locks, access role (trust account root + StringLike aws:userid, no TagSession, read-only inline policy, tag sch-owner, MaxSessionDuration 3600), SSM parameter DependsOn both locks. Outputs RuntimeArn, AccessRoleArn, OwnerPrefix, ExecutionRoleArn. Verify every logs read action name in R20 against the service authorization reference and record the list.
3. infra/deploy.sh.
   - Parse ISOLATED_PRINCIPALS; R2 and R6 Telegram refusal; resolve entries with iam get-user, get-role, list-roles --path-prefix /aws-reserved/sso.amazonaws.com/ (R3, R4); sso warning (R5); owner key sha256("<kind>:<boundId>")[:16] (R7), computed in python3 for portability; duplicates and ambiguity checks.
   - Discover existing plane stacks (list-stacks + describe-stacks tags, R12); quota preflight with service-quotas get-service-quota (fallback 100) and bedrock-agentcore-control list-agent-runtimes (R6). All of this before the bootstrap stack.
   - Pass IsolationEnabled to the runtime stack; then one cloudformation deploy per plane with --no-fail-on-empty-changeset, continuing on failure; then delete orphans and wait; per-entry summary; exit non-zero on any failure. The smoke-test hint prints plane ARNs instead of the shared runtime.
   - Check managed-policy size before deploying (X9).
4. sch destroy and teardown (R48): cli/sch/awsteardown.py and cli/sch/commands/destroy.py delete plane stacks (prefix + tag) before the runtime stack; the bucket purge covers both layouts. sch deploy forwards ISOLATED_PRINCIPALS.
5. Tests (infra/test_*.py): default rendering unchanged except the managed-policy move (resource list compared to today); IsolationOn rendering; plane template rendering per entry kind; deploy.sh entry resolution with a stubbed aws CLI (unknown, ambiguous, duplicate, sso warning, quota, Telegram refusal, orphan deletion); botocore ParamValidator on every request shape built by new Python code.
6. Evidence scripts (read-only, AWS allowed actions only): infra/validate_isolation_policies.py (access-analyzer ValidatePolicy for every rendered policy: identity, boundary, trust, bucket and AgentCore resource policies) and infra/simulate_isolation.py (iam simulate-custom-policy for the R24 and R16 cases of spec "Evidence each slice produces", Identity Center callers in custom mode with explicit aws:userid context). Commit redacted outputs under docs/history/ or the task notes; account IDs replaced with 111122223333. Record the X4 simulator limits next to the output.
7. Suites (cli, infra, tunnel, image-side), bin/verify-docs.sh, journal, masterplan, commit on feat/task-20.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Assumptions (headless run, defaults of TASK-20 applied):
- AC #1 "creates no new resource": with the allow-list unset the only new resource is AgentRuntimeBasePolicy, the former inline base policy moved into a customer managed policy (decision-14 makes the move apply in every mode). Its document is the old inline document unchanged; the fixture test now proves effective-permission identity instead of byte identity. No bucket policy, lock, SSM parameter or plane exists without isolation, and the registry environment and role are unchanged apart from the least-privilege scoping below.
- Converted policies get new logical IDs (...ManagedPolicy): CloudFormation cannot change the type of an existing logical resource. Managed policy names keep the former inline names. One-time transition on the first deploy of this template: the role update drops the inline policy before the new managed policy is attached, so running sessions may see a few seconds of AccessDenied; deploy the upgrade with no session running.
- The escape-hatch managed policy takes the raw JSON string parameter as PolicyDocument, as the inline policy did (CloudFormation Json properties accept a JSON string; not verified live).
- Registry role scoping applies in every mode (R39): StopRuntimeSession on runtime/<p>_<e>_* and logs on its own /aws/lambda log group only (was '*' and the whole account's logs).
- Bucket-policy exemptions: registry role always; watchdog and image-rebuild roles only when those features exist (Fn::If in the ArnNotLike list).
- Lock and plane-parameter JSON is built with Fn::Sub strings (ResourcePolicy.Policy is a String). The owner pattern parameter is restricted to ^A[A-Z0-9]{15,127}(:(\*|[A-Za-z0-9._@+=,-]{1,64}))?$ so a username can neither break the JSON nor inject IAM wildcards; deploy.sh applies the same username set.
- R20 logs actions: verified against the service reference JSON (servicereference.us-east-1.amazonaws.com, 2026-09-27). All read-level actions that accept log-group/log-stream resources are denied: FilterLogEvents, GetDataProtectionPolicy, GetLogEvents, GetLogGroupFields, GetLogRecord, GetQueryResults, GetTransformer, StartLiveTail, StartQuery, Unmask. List-level ones (DescribeLogStreams etc.) stay allowed (no content; DescribeLogStreams is also in the base policy). ListAgentRuntimeVersions/ListAgentRuntimeEndpoints were dropped from the R20 deny: they accept no resource and return no configuration. Spec amended.
- Quota (R6): Service Quotas 'Total Agents per Account' (bedrock-agentcore, L-F4575653); applied value, then the AWS default, else 100. The live account read 100 applied; the AWS default listing showed 1000. Recorded in the spec.
- Deploy order deviation from the spec draft: orphan planes are deleted before the runtime stack update (not after the plane deploys), so turning isolation off never leaves a removed principal's runtime running without the bucket policy. Spec deploy flow updated.
- Plane configuration is read back from the runtime stack as deployed (parameters and outputs) instead of being recomputed, so -s deploys and parameters set outside deploy.sh stay in sync (R11). A plane in ROLLBACK_COMPLETE is deleted and recreated.
- With ISOLATED_PRINCIPALS empty the preflight only lists plane stacks (orphans); a failed lookup warns and continues. python3 is required only with isolation on (it is used for the policy-size check whenever present).
- X9 check: conservative estimate of the base policy (budget 2300 + two ARNs per allow-list entry; a test keeps the estimate >= the rendered size and within 600 characters of it) and the exact compact size of the escape hatch.
- sch destroy: plane listing failure stops the teardown before any deletion; the checkpoint bucket policy is dropped (best effort) before the purge in case the runtime stack was deleted by hand.
- Evidence scripts use boto3 (dev extra) and the rendered templates with the placeholder account 111122223333, so no real account data is written. The simulator run reads the real AWS managed ReadOnlyAccess document (v190) with iam:GetPolicyVersion.
- Guides (docs/deploy.md, docs/workspaces.md, SECURITY.md) are TASK-20.5 scope; only deploy.sh's own header documents ISOLATED_PRINCIPALS now. No user-facing doc examples were written (AGENTS.md: propose, do not write).

Verification:
- infra suite 194 OK (new: test_isolation_templates 26, test_isolation_plan 26, test_isolation_deploy 9; test_runtime_tuning updated for the managed policies); cli suite 600 OK (new destroy and deploy-forwarding tests); tunnel npm test pass; image-side 534 tests with sandboxed paths: only the 7 known TASK-22 failures, live-session files untouched; bin/verify-docs.sh passes; bash -n on all scripts.
- botocore ParamValidator on every request the preflight builds (stubbed aws CLI), and the CLI spelling of each operation checked against botocore xform_name.
- Access Analyzer (infra/validate_isolation_policies.py, report docs/history/isolation-evidence-access-analyzer.md): 37 documents, 0 ERROR; boundary SECURITY_WARNING/WARNING for Allow * (inherent to a boundary, explained in the report), one pre-existing SUGGESTION.
- Simulator (infra/simulate_isolation.py, report docs/history/isolation-evidence-simulator.md): 222 cases, 0 unexpected; every denial except the access-role write is an explicitDeny, and each boundary denial has an allowed control case outside SCH. Simulator limits (X4 plus the GetLogEvents log-stream ARN quirk) recorded in the report.
- Live read-only preflight against the account: role entry resolved, unknown user and unknown permission set refused, quota read (100).
- Not done here (operator-side, parent TASK-20): an actual deploy; CloudFormation acceptance of AWS::BedrockAgentCore::ResourcePolicy and the string escape-hatch document; the joint runtime+endpoint evaluation.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Infrastructure slice of TASK-20. infra/agent_runtime.yaml: IsolationEnabled switch (effective only with the registry); base, capability, escape-hatch and image-rebuild session policies are customer managed policies (new logical IDs, same documents) exported as SharedRuntimePolicyArns; with isolation on a constant three-statement checkpoint bucket policy (R24) and deny-only AWS::BedrockAgentCore::ResourcePolicy locks on the shared runtime and its DEFAULT endpoint (R17); registry role scoped to its table, runtime/<p>_<e>_* StopRuntimeSession, its own logs and (isolation on) read-only plane parameters, plus ISOLATION_ENABLED/PLANE_PARAMETER_PREFIX. New infra/user_plane.yaml: boundary, execution role, locked user runtime (shared env minus Telegram plus SCH_OWNER_PREFIX), runtime and endpoint locks, owner-only read-only access role, plane-mapping parameter after both locks. New infra/isolation_plan.py preflight (entry resolution with IAM reads, owner keys, duplicate/ambiguity checks, sso warning, Telegram and registry checks, runtime quota, plane discovery by prefix and tag, managed-policy size) wired into deploy.sh before any stack; deploy.sh deletes orphan planes before the runtime stack, deploys one plane stack per principal after it (configuration read back from the runtime stack, failures isolated, non-zero exit) and prints a per-entry summary. sch destroy deletes plane stacks before the runtime stack and drops the bucket policy before the purge. Evidence: Access Analyzer 0 ERROR on 37 rendered documents and 222 simulator cases as expected, both reproducible by committed read-only scripts with redacted reports under docs/history/. Specs amended (per-principal-isolation, runtime-capability-tuning R1/R3/R10/I1, runtime-provisioning R8, workspace-registry R2, installation R14). All suites green (image-side: only the 7 known TASK-22 failures). Live deploy and joint lock evaluation stay operator-side (TASK-20).
<!-- SECTION:FINAL_SUMMARY:END -->
