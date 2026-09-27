---
id: TASK-20
title: Per-user workspace isolation with runtimes provisioned at deploy time
status: To Do
assignee: []
created_date: '2026-09-27 10:12'
updated_date: '2026-09-27 10:13'
labels:
  - security
dependencies: []
references:
  - docs/specs/security/iam-workspace-registry.md
  - docs/specs/security/owner-scoped-workspace-storage.md
  - docs/specs/security/iam-workspace-control-api.md
  - docs/workspaces.md
  - infra/agent_runtime.yaml
  - infra/deploy.sh
  - infra/workspace_registry_handler.py
  - image/app/main.py
  - >-
    https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/resource-based-policies.html
  - >-
    https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_CreateAgentRuntime.html
  - >-
    https://docs.aws.amazon.com/service-authorization/latest/reference/list_bedrock-agentcore.html
  - >-
    https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_variables.html
  - >-
    https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_elements_principal.html
priority: high
type: feature
ordinal: 12000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
## Goal

Give every SCH user in one AWS account a workspace that other users, and their agents, cannot join or read. The feature is requested by the community and is an opt-in, deploy-time switch for registry-enabled stacks. The operator lists the allowed principals at deploy time; `infra/deploy.sh` creates one isolated plane per principal with CloudFormation. At request time nothing creates or changes IAM or AgentCore resources.

## Current state on dev

- Registry mode separates workspace names per caller (`ownerId` = sha256 of the caller ARN), but every workspace runs on one shared runtime whose execution role can read every checkpoint. `docs/workspaces.md` states that this is not a data-plane boundary; `SECURITY.md` promises per-user isolation, which is not enforced.
- AgentCore authorizes `InvokeAgentRuntime*` on the runtime ARN, not on the session: anyone allowed to invoke the runtime who knows a session ID can join it. The only per-principal boundary AWS offers here is a separate runtime per principal, locked by a deny resource policy, with an execution role limited to that principal's storage, plus a bucket policy.
- A first attempt (TASK-8) created those resources from the registry Lambda at request time. It was reviewed, found not deployable and rolled back; dev does not contain it. This task replaces it.

## Decided by the maintainer (2026-09-27)

- Per-user segregation is a required feature.
- Per-user runtimes, and their roles and policies, are created at deploy time, not by the registry at request time.
- The request-time provisioning of TASK-8 is not reused.

## Defaults to apply

The maintainer may change these before giving the GO. Record the final values in the decision record.

1. **Principals.** An explicit allow-list in a deploy-time switch (suggested name `ISOLATED_PRINCIPALS`). Empty or unset means the feature is off, an inert default as in decision-4; setting it requires `ENABLE_WORKSPACE_REGISTRY=true`. Entry forms:
   - `user:<iam-user-name>`, bound to the user's unique ID (`aws:userid` = `AIDA...`);
   - `sso:<permission-set-name>/<identity-center-username>`, bound to `<role-unique-id>:<username>` of the permission-set role (Identity Center sets that session name);
   - `role:<role-name>`, where every session of the role is one owner (automation).
   `infra/deploy.sh` resolves entries with IAM read calls and fails before changing any stack on an unknown or ambiguous entry.
2. **Owner identity.** Derived from the bound identity (unique IDs), never from the session ARN. A new session name (for example `botocore-session-<epoch>`) stays the same owner, a re-created IAM user with the same name is a new owner, and a caller-chosen role session name never becomes a separate owner. With isolation off the registry keeps today's derivation.
3. **Planes.** One CloudFormation stack per principal from a new template (for example `infra/user_plane.yaml`), deployed by `infra/deploy.sh` after the runtime stack. Each stack is updated on every deploy (image digest and runtime configuration) and deleted when the principal leaves the list or by `sch destroy`. `Fn::ForEach` in the runtime stack is the alternative: choose after research and record why.
4. **Runtime lock.** Resource policies on each user runtime and on its DEFAULT endpoint deny every data-plane action (all `InvokeAgentRuntime*` variants, `StopRuntimeSession`, `GetAgentCard`) to everyone except the owner, conditioned on `aws:userid`, plus `aws:PrincipalArn` for the registry role, which may only stop sessions. The policies are deny-only, so no principal ARN has to exist or carry the right path. When isolation is on, the shared runtime denies user data-plane actions. Every SCH user, the operator included, must then be listed, and registry-off mode is not available on that stack.
5. **Execution role per principal.**
   - It gets the shared role's permissions from one source of truth in the repository, for example customer managed policies attached to both roles. Nothing is copied at run time.
   - A CloudFormation-managed permissions boundary restricts only SCH-owned shared resources. The owner's storage prefixes stay readable and writable. Other principals' prefixes, the registry table, the Telegram tables and other SCH runtime log groups are denied.
   - Everything else the shared role allows keeps working: `ReadOnlyAccess`, build-source uploads, data-bucket writes.
   - Role names contain `BedrockAgentCore`, so the deploy-principal `iam:PassRole` scope documented in `docs/getting-started.md` still applies.
6. **Operator reads of the owner's checkpoints** (`sch status`, `sch list --remote-check`, `sch dashboard`) go through a per-principal access role. The plane stack creates it, its trust is restricted to the owner with `aws:userid`, and it is read-only on the owner's prefixes. A bucket policy denies checkpoint objects to everyone else. The CLI refreshes these credentials before they expire.
7. **Storage layout.**
   - Per-owner prefixes for checkpoints, generations, writer claims, task status and build sources.
   - They must not collide with registry-off workspace names, which match `[A-Za-z0-9][A-Za-z0-9_-]{0,127}`. A separator such as `o.<16 hex>`, or a top-level `owners/` tree, is unambiguous.
   - The existing lifecycle rules (candidate generations, build sources) keep applying, and the registry-off layout is unchanged.
8. **Shim.**
   - The owner prefix comes from the user runtime's environment (for example `SCH_OWNER_PREFIX`, set by the plane stack), never from invoke payloads or marker files. A payload that disagrees with it is rejected.
   - Nothing is written into the workspace root before the bootstrap restore completes.
   - Runtimes without the variable behave exactly as today.
9. **Telegram with isolation on.** Not available to user runtimes: no bot token, chat ID or table names in their environment, and no Telegram session policy on their roles. It is documented as a single-operator feature; per-owner Telegram is a follow-up.
10. **Migration.** None. With isolation on, owners start with empty namespaces. Existing registry records and checkpoints are left untouched, and the docs say so; this is the same approach as the OpenCode 2 fresh installation.
11. **Removing a principal.** The next deploy deletes its plane. Its storage is retained (bucket retention), and the docs explain how to purge it.
12. **Logs.** Task prompts are no longer written to the runtime logs. Today the shim logs the headless argv, which ends with the prompt.

## Lessons from the rolled-back attempt (do not repeat)

- **IAM actions.** AgentCore actions use the `bedrock-agentcore:` prefix; `bedrock-agentcore-control` is only the SDK client name. CreateAgentRuntime also needs `bedrock-agentcore:CreateAgentRuntimeEndpoint`, `bedrock-agentcore:TagResource` when tags are passed, and `iam:PassRole`. `s3:HeadObject` is not an IAM action.
- **API constraints.**
  - The CreateAgentRuntime `clientToken` must be 33 to 256 characters of `[a-zA-Z0-9-]`.
  - Runtime names match `[a-zA-Z][a-zA-Z0-9_]{0,47}`.
  - The endpoint ARN is `<runtime-arn>/runtime-endpoint/DEFAULT`.
- **Resource policies.** AgentCore resource policies cover data-plane actions only, their `Resource` must be the exact ARN, and the runtime and endpoint policies are both evaluated. A rejected policy must fail the deploy, never leave a runtime unlocked.
- **Role ARNs.** For a role session, `aws:PrincipalArn` is the role ARN with its path, while STS session ARNs carry no path, and every IAM Identity Center role lives under `/aws-reserved/sso.amazonaws.com/`. Bind people with `aws:userid`, not with role ARNs rebuilt from session ARNs.
- **Boundary administrators.** Principals with AgentCore control-plane write (for example `BedrockAgentCoreFullAccess`), IAM write, or Lambda or CloudFormation deploy rights can remove any lock. They administer the boundary, and the docs must say so.
- **Manifests.** Checkpoint manifests store absolute artifact keys, so any copy or move must rewrite them.
- **Restore.** The L2 restore promotes only into an empty root: no invocation may write into the workspace root before the bootstrap finishes.
- **Runtime updates.** A runtime version update supersedes running microVMs and resets session storage. Update user runtimes at deploy time only, never from `sch status` or other observation commands.
- **Boundary scope.** A boundary that caps every S3 write breaks `sch-build-image` (`builds/`) and the data-bucket capabilities.
- **Shared state.** State outside checkpoints (registry table, Telegram tables, runtime logs, build sources) must be covered too, not only checkpoint objects.
- **Evidence.** Unit tests that compare policy shapes prove nothing. Validate policies with IAM Access Analyzer, evaluate them with the IAM policy simulator, check every AWS request the code builds with botocore's `ParamValidator`, and finish with a live check using two principals.

## Delivery slices

Keep all suites green and commit at the end of each slice. If time runs out, stop at a slice boundary and record progress in the implementation notes.

1. **Design.** A decision record (`backlog decision create`), a normative spec under `docs/specs/security/` (identities, layout, planes, threat model, residual risks), and the implementation plan in this task.
2. **Storage and shim.** Owner prefix from the environment, per-owner layout, no early writes, prompts out of the logs, a per-owner `sch-build-image` key, the new layout in the watchdog listing; image-side tests.
3. **Infrastructure.**
   - The plane template.
   - Runtime-template changes: managed policies, bucket policy, shared-runtime lock, least-privilege registry role.
   - `infra/deploy.sh`: the switch, entry resolution, a preflight that includes the AgentCore runtime quota (100 per account by default), per-principal stacks, image and configuration updates.
   - `sch destroy`.
   - Template tests, plus read-only Access Analyzer and simulator scripts.
4. **Registry and CLI.**
   - Callers mapped to owners by bound identity.
   - HTTP 403 for unlisted callers, with a message that names the identity to add.
   - Plane fields in responses, and no fallback to the shared runtime.
   - Access-role reads with refresh, plus the `sch dashboard` fixes.
   - Registry-off unchanged; tests.
5. **Tooling and docs.** `bin/verify-*.sh` usable on isolation stacks, a new `bin/verify-isolation.sh`, the guides and `SECURITY.md`, a journal entry and the masterplan.

## Constraints for unattended execution

- **Headless run.** The task runs with `sch task` and nobody answers questions. Apply the defaults above and record every assumption in the implementation notes.
- **Read-only AWS.** AWS access from the microVM is read-only. Never try to deploy, or to create, change or delete AWS resources.
  - Allowed: `access-analyzer:ValidatePolicy`, `iam:SimulatePrincipalPolicy`, `iam:SimulateCustomPolicy`, and describe, get and list calls.
  - Never print or commit account IDs; use `111122223333` in fixtures.
- **Do not disturb the live session.** Importing `image/app/main.py` deletes `/tmp/sch-telegram-enabled`, and `invoke()` rewrites `/run/sch/provider-keys.env` and `/home/sch/.sch-workspace.json`. Run image-side tests with `SCH_TELEGRAM_ENABLED_MARKER`, `SCH_PROVIDER_KEYS_FILE`, `SCH_GIT_CREDENTIALS_FILE`, `SCH_CHECKPOINT_TMP_DIR` and `SCH_WORKSPACE_ROOT` pointing into a temporary directory, and patch the hard-coded roots before calling shim functions.
- **Bootstrap.** `uv venv .venv && uv pip install --python .venv/bin/python -e ".[dev]"`, then `cd tunnel && npm ci`.
- **Git.**
  - Create `feat/per-user-runtime-isolation` from `dev`, use conventional commits, and commit after each slice.
  - Never push or force-push.
  - Do not commit `uv.lock`, `.venv/`, or the TASK-10 file under `.backlog/tasks/`, which is intentionally untracked.
- **Project rules.** Follow `AGENTS.md` (masterplan, normative specs, journal via `backlog doc create`, decisions via `backlog decision create`) and `docs/coding-standards.md`.
- **Out of scope.** Repository-token handling in `image/scripts/init-workspace.sh`, OAuth inbound authorization, cross-account isolation.
- **When finished,** leave this task In Progress with the last acceptance criterion unchecked: the live check is operator-side.

## Reference material

- **The rolled-back attempt and its review,** if the branch exists in the workspace: `git show docs/task-8-isolation-review:<path>`.
  - `26e44ae` is TASK-8 as received.
  - The review is `.backlog/brainstorming/2026-09-27.IsolationReview/review.md`; its `repro/validate_policies.py` and `repro/simulate_owner_role.py` are reusable patterns for read-only verification.
  - Ideas worth reusing: CLI adoption of a registry-returned runtime ARN, assume-role reads of owner checkpoints, nested watchdog listing, purge of both layouts.
  - Not reusable: request-time provisioning (`infra/workspace_isolation.py`).
- **Code.** `infra/agent_runtime.yaml`, `infra/deploy.sh`, `infra/workspace_registry_handler.py`, `infra/task_watchdog_handler.py`, `cli/sch/workspace_registry.py`, `cli/sch/config.py`, `cli/sch/harness.py`, `cli/sch/runtime.py`, `cli/sch/commands/` (`status.py`, `list.py`, `stop.py`, `delete.py`, `destroy.py`), `cli/sch/dashboard.py`, `cli/sch/awsteardown.py`, `image/app/main.py`, `image/scripts/sch-build-image.sh`, `bin/verify-*.sh`.
- **Specs and guides.** `docs/specs/security/` (`iam-workspace-control-api.md`, `iam-workspace-registry.md`, `owner-scoped-workspace-storage.md`, `workspace-registry.md`), `docs/workspaces.md`, `docs/deploy.md`, `docs/security.md`, `SECURITY.md`, `docs/getting-started.md`.

## Launch

Only after the maintainer's GO, on a workspace whose `dev` branch contains this task:

`sch task <workspace> "Carry out Backlog task TASK-20 end to end, unattended, as its description says."`
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 A decision record and a normative spec under docs/specs/security/ describe the bound identities, the deploy-time planes, the storage layout, the threat model (who administers the boundary) and the residual risks
- [ ] #2 With the allow-list unset, a deploy creates no new resource and the registry, CLI and shim behave as on dev; the existing suites, including the default-rendering tests, pass
- [ ] #3 With the allow-list set, infra/deploy.sh resolves every entry to its bound identity, refuses unknown or ambiguous entries before changing any stack, and keeps exactly one plane per listed principal (created, updated on every deploy, deleted when the entry is removed)
- [ ] #4 No request-time component (registry Lambda, CLI, shim) can create or change IAM roles or policies, AgentCore runtimes or resource policies; the registry role keeps only its table, checkpoint purge and StopRuntimeSession permissions
- [ ] #5 Every new or changed policy document validates with IAM Access Analyzer without ERROR findings, and a committed read-only script reproduces the check
- [ ] #6 IAM policy simulator evidence (committed script, output with the account redacted) shows that an owner's execution role reads and writes only its own storage, including through ReadOnlyAccess, still uploads build sources and writes to the data bucket, and is denied other owners' storage, the registry table, the Telegram tables and other SCH runtime log groups
- [ ] #7 Simulator evidence shows that each user runtime's resource policy denies every data-plane action to another IAM user, to another Identity Center user of the same permission set and to an unlisted role session, all holding bedrock-agentcore:* on *, and allows the owner (IAM user and Identity Center cases)
- [ ] #8 The registry maps callers by bound identity: an unlisted caller gets HTTP 403 and no record is created, two Identity Center users of one permission set are different owners, a new session name keeps the owner, and the isolation-off derivation is unchanged; unit tests cover each case
- [ ] #9 The shim takes the owner prefix only from the runtime environment, rejects a payload that disagrees, never writes into the workspace root before the restore completes, and no longer logs task prompts; tests cover s3 and session restores with concurrent invokes, and registry-off behavior
- [ ] #10 User runtimes receive image and configuration changes during deploy only, no sch command updates a runtime, and sch destroy removes every plane
- [ ] #11 Every sch command uses the owner's runtime without falling back to the shared runtime, owner checkpoint reads go through the access role with credential refresh (sch dashboard keeps working past expiry), and registry-off sch status output is unchanged
- [ ] #12 The bin/verify-*.sh scripts run against an isolation-enabled stack as a listed user, and bin/verify-isolation.sh checks two principals end to end: join denied even with a known session ID, cross-owner checkpoint reads denied both from the CLI and from inside the agent, own workflow works, unlisted caller refused
- [ ] #13 docs/workspaces.md, docs/deploy.md, docs/security.md and SECURITY.md explain how to enable the feature, how to add and remove principals, the caller permissions, Telegram and ReadOnlyAccess behavior, the AgentCore runtime quota and the residual risks, and claim only what the tests and the simulator prove
- [ ] #14 Operator-side live check passes: a test stack deployed with two IAM users (and one Identity Center user when available) runs bin/verify-isolation.sh successfully; the executing agent leaves this criterion unchecked
<!-- AC:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [ ] #1 cli, infra and tunnel suites, the image-side tests (with sandboxed paths) and bin/verify-docs.sh pass
- [ ] #2 Journal entry and masterplan updated as AGENTS.md requires; implementation notes list every assumption and the verification results
- [ ] #3 Work committed on feat/per-user-runtime-isolation, never pushed
<!-- DOD:END -->
