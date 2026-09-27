---
id: TASK-20.3
title: 'Isolation: per-principal plane stacks and deploy'
status: In Progress
assignee: []
created_date: '2026-09-27 14:50'
updated_date: '2026-09-27 16:13'
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
- [ ] #1 With the allow-list unset, a deploy creates no new resource; the default-rendering tests pass
- [ ] #2 With the allow-list set, infra/deploy.sh resolves every entry to its bound identity, refuses unknown or ambiguous entries before changing any stack, warns on unverified sso usernames, checks the AgentCore runtime quota, and keeps exactly one plane stack per listed principal (created, updated on every deploy, deleted when the entry is removed)
- [ ] #3 Runtime and DEFAULT endpoint locks are AWS::BedrockAgentCore::ResourcePolicy resources in the plane stack; the shared runtime denies user data-plane actions when isolation is on
- [ ] #4 The registry role keeps only its table, read-only access to the plane-mapping parameters, checkpoint purge and StopRuntimeSession on the deployment runtimes, and no role used at request time can create or change IAM, AgentCore runtimes, resource policies or plane parameters
- [ ] #5 Every new or changed policy document validates with IAM Access Analyzer without ERROR findings, and a committed read-only script reproduces the check
- [ ] #6 Committed simulator script and redacted output show that an owner execution role reads and writes only its own storage (including through ReadOnlyAccess), still uploads build sources and writes to the data bucket, and is denied other owners storage, the registry table, the Telegram tables and other SCH runtime log groups
- [ ] #7 Simulator evidence shows each user runtime and endpoint policy denies every data-plane action to another IAM user, another Identity Center user of the same permission set and an unlisted role session, all holding bedrock-agentcore:* on *, and allows the owner (IAM user and Identity Center cases); the simulator limits are recorded next to the evidence
- [ ] #8 User runtimes receive image and configuration changes during deploy only, and sch destroy removes every plane stack before the runtime stack
<!-- AC:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [ ] #1 Relevant suites pass (cli, infra, tunnel, image-side tests with sandboxed paths) and bin/verify-docs.sh when docs changed
- [ ] #2 Implementation notes list every assumption and the verification results; a journal entry is created and MASTERPLAN.md updated as AGENTS.md requires
- [ ] #3 Work committed on feat/task-20 with conventional commits, never pushed
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
