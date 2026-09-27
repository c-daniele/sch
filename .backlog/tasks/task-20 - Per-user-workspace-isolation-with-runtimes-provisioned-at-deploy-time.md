---
id: TASK-20
title: Per-user workspace isolation with runtimes provisioned at deploy time
status: To Do
assignee: []
created_date: '2026-09-27 10:12'
updated_date: '2026-09-27 14:50'
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

## Current state on the branch

- Registry mode separates workspace names per caller (`ownerId` = sha256 of the caller ARN), but every workspace runs on one shared runtime whose execution role can read every checkpoint. `docs/workspaces.md` states that this is not a data-plane boundary; `SECURITY.md` promises per-user isolation, which is not enforced.
- AgentCore authorizes `InvokeAgentRuntime*` on the runtime ARN, not on the session: anyone allowed to invoke the runtime who knows a session ID can join it. The only per-principal boundary AWS offers here is a separate runtime per principal, locked by a deny resource policy, with an execution role limited to that principal's storage, plus a bucket policy.
- A first attempt (TASK-8) created those resources from the registry Lambda at request time. It was reviewed, found not deployable and rolled back; this repository does not contain it. This task replaces it.

## Decided by the maintainer (2026-09-27)

- Per-user segregation is a required feature.
- Per-user runtimes, and their roles and policies, are created at deploy time, not by the registry at request time.
- The request-time provisioning of TASK-8 is not reused.
- Decided 2026-09-27 (second session): the work runs headless in a microVM, split into subtasks TASK-20.1 to TASK-20.5 (one per delivery slice), all on the existing branch `feat/task-20`. Planes are one CloudFormation stack per principal (default 3 is final; `Fn::ForEach` is rejected). `sso:` usernames are accepted unverified (default 1 is final).

## Defaults to apply

The maintainer may change these before giving the GO. Record the final values in the decision record.

1. **Principals.** An explicit allow-list in a deploy-time switch (suggested name `ISOLATED_PRINCIPALS`). Empty or unset means the feature is off, an inert default as in decision-4; setting it requires `ENABLE_WORKSPACE_REGISTRY=true`. Entry forms:
   - `user:<iam-user-name>`, bound to the user's unique ID (`aws:userid` = `AIDA...`);
   - `sso:<permission-set-name>/<identity-center-username>`, bound to `<role-unique-id>:<username>` of the permission-set role (Identity Center sets that session name);
   - `role:<role-name>`, where every session of the role is one owner (automation).
   `infra/deploy.sh` resolves entries with IAM read calls and fails before changing any stack on an unknown or ambiguous entry.
   For `sso:` entries only the permission-set role can be resolved with IAM reads (under `/aws-reserved/sso.amazonaws.com/`); the Identity Center username cannot be verified without `identitystore` access. The deploy accepts it unverified, prints a warning, and the spec lists this as a residual risk.
2. **Owner identity.** Derived from the bound identity (unique IDs), never from the session ARN. A new session name (for example `botocore-session-<epoch>`) stays the same owner, a re-created IAM user with the same name is a new owner, and a caller-chosen role session name never becomes a separate owner. With isolation off the registry keeps today's derivation.
3. **Planes.** One CloudFormation stack per principal from a new template (for example `infra/user_plane.yaml`), deployed by `infra/deploy.sh` after the runtime stack. Each stack is updated on every deploy (image digest and runtime configuration) and deleted when the principal leaves the list or by `sch destroy`. `infra/deploy.sh` finds the plane stacks of a deployment by name prefix and tag, so it can delete orphans.
   - Why one stack per principal rather than `Fn::ForEach` in the runtime stack: a failure on one principal does not block or roll back the others, the runtime stack stays far from the 500-resource limit, and removing a principal is a stack deletion.
   - The runtime lock uses `AWS::BedrockAgentCore::ResourcePolicy` (a public CloudFormation type, properties `ResourceArn` and `Policy`, verified 2026-09-27) on the runtime and on its DEFAULT endpoint, inside the plane stack. A rejected policy rolls the stack back. No custom resource, and no `put-resource-policy` call from `deploy.sh`.
4. **Runtime lock.** `AWS::BedrockAgentCore::ResourcePolicy` resources on each user runtime and on its DEFAULT endpoint deny every data-plane action (all `InvokeAgentRuntime*` variants, `StopRuntimeSession`, `GetAgentCard`) to everyone except the owner, conditioned on `aws:userid`, plus `aws:PrincipalArn` for the registry role, which may only stop sessions. The policies are deny-only, so no principal ARN has to exist or carry the right path. When isolation is on, the shared runtime denies user data-plane actions. Every SCH user, the operator included, must then be listed, and registry-off mode is not available on that stack.
5. **Execution role per principal.**
   - It gets the shared role's permissions from one source of truth in the repository, for example customer managed policies attached to both roles. Nothing is copied at run time.
   - A CloudFormation-managed permissions boundary restricts only SCH-owned shared resources. The owner's storage prefixes stay readable and writable. Other principals' prefixes, the registry table, the Telegram tables and other SCH runtime log groups are denied.
   - Everything else the shared role allows keeps working: `ReadOnlyAccess`, build-source uploads, data-bucket writes.
   - `ReadOnlyAccess` also reads the configuration of other runtimes and Lambdas, including environment variables (for example the Telegram bot token on the shared runtime through `bedrock-agentcore:GetAgentRuntime`, or `lambda:GetFunctionConfiguration`). The boundary denies those reads on SCH resources, or the spec lists what remains as a residual risk.
   - Cross-owner S3 denial belongs in the bucket policy, conditioned on each owner's execution and access roles. The boundary keeps a simple shape instead of per-owner `NotResource` lists.
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
  - The endpoint ARN is `<runtime-arn>/runtime-endpoint/DEFAULT` (verified on the live dev runtime; the AWS guide's `/endpoint/<id>` example does not match).
- **Resource policies.** AgentCore resource policies cover data-plane actions only, their `Resource` must be the exact ARN, and the runtime and endpoint policies are both evaluated. A rejected policy must fail the deploy, never leave a runtime unlocked.
- **Role ARNs.** For a role session, `aws:PrincipalArn` is the role ARN with its path, while STS session ARNs carry no path, and every IAM Identity Center role lives under `/aws-reserved/sso.amazonaws.com/`. Bind people with `aws:userid`, not with role ARNs rebuilt from session ARNs.
- **Boundary administrators.** Principals with AgentCore control-plane write (for example `BedrockAgentCoreFullAccess`), IAM write, or Lambda or CloudFormation deploy rights can remove any lock. They administer the boundary, and the docs must say so.
- **Manifests.** Checkpoint manifests store absolute artifact keys, so any copy or move must rewrite them.
- **Restore.** The L2 restore promotes only into an empty root: no invocation may write into the workspace root before the bootstrap finishes.
- **Runtime updates.** A runtime version update supersedes running microVMs and resets session storage. Update user runtimes at deploy time only, never from `sch status` or other observation commands.
- **Boundary scope.** A boundary that caps every S3 write breaks `sch-build-image` (`builds/`) and the data-bucket capabilities.
- **Shared state.** State outside checkpoints (registry table, Telegram tables, runtime logs, build sources) must be covered too, not only checkpoint objects.
- **Evidence.** Unit tests that compare policy shapes prove nothing. Validate policies with IAM Access Analyzer, evaluate them with the IAM policy simulator, check every AWS request the code builds with botocore's `ParamValidator`, and finish with a live check using two principals.
- **Simulator limits.** The simulator evaluates one resource policy per call, so it does not model the joint runtime and endpoint evaluation: simulate each policy separately. It fills `aws:userid` from the caller, so Identity Center callers are simulated in custom mode with explicit context entries. Record these limits next to the evidence; only the live check covers the joint evaluation.
- **Plane creation window.** The runtime exists briefly before its resource policies are attached. Its ARN is not published yet, so the risk is low; the spec states it.

## Delivery slices

Each slice is a subtask with its own acceptance criteria: TASK-20.1 (design), TASK-20.2 (storage and shim), TASK-20.3 (infrastructure), TASK-20.4 (registry and CLI), TASK-20.5 (tooling and docs). Each depends on the previous one and runs as its own headless `sch task`. Keep all suites green and commit at the end of each subtask. This parent task holds the shared context and the operator-side live check.

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

- **Headless run.** Each subtask runs with `sch task` in a microVM and nobody answers questions. Apply the defaults above and record every assumption in the subtask's implementation notes. A subtask whose predecessor is not Done stops and says so.
- **Read-only AWS.** AWS access from the microVM is read-only. Never try to deploy, or to create, change or delete AWS resources.
  - Allowed: `access-analyzer:ValidatePolicy`, `iam:SimulatePrincipalPolicy`, `iam:SimulateCustomPolicy`, and describe, get and list calls.
  - Never print or commit account IDs; use `111122223333` in fixtures.
- **Do not disturb the live session.** Importing `image/app/main.py` deletes `/tmp/sch-telegram-enabled`, and `invoke()` rewrites `/run/sch/provider-keys.env` and `/home/sch/.sch-workspace.json`. Run image-side tests with `SCH_TELEGRAM_ENABLED_MARKER`, `SCH_PROVIDER_KEYS_FILE`, `SCH_GIT_CREDENTIALS_FILE`, `SCH_CHECKPOINT_TMP_DIR` and `SCH_WORKSPACE_ROOT` pointing into a temporary directory, and patch the hard-coded roots before calling shim functions.
- **Bootstrap.** `uv venv .venv && uv pip install --python .venv/bin/python -e ".[dev]"`, then `cd tunnel && npm ci`.
- **Git.**
  - Work on the existing branch `feat/task-20` (there is no `dev` branch). Use conventional commits and commit at the end of each subtask.
  - Never push or force-push.
  - Do not commit `uv.lock` or `.venv/`.
- **Project rules.** Follow `AGENTS.md` (masterplan, normative specs, journal via `backlog doc create`, decisions via `backlog decision create`) and `docs/coding-standards.md`.
- **Out of scope.** Repository-token handling in `image/scripts/init-workspace.sh`, OAuth inbound authorization, cross-account isolation.
- **When finished,** each subtask is set Done. The parent stays In Progress with the live-check criterion unchecked: it is operator-side.

## Reference material

- **The rolled-back attempt and its review are not in this repository** (no `docs/task-8-isolation-review` branch, no commit `26e44ae`, no `IsolationReview` brainstorming folder; checked 2026-09-27). Verification scripts are written from scratch.
  - Ideas worth keeping from that attempt: CLI adoption of a registry-returned runtime ARN, assume-role reads of owner checkpoints, nested watchdog listing, purge of both layouts.
- **Code.** `infra/agent_runtime.yaml`, `infra/deploy.sh`, `infra/workspace_registry_handler.py`, `infra/task_watchdog_handler.py`, `cli/sch/workspace_registry.py`, `cli/sch/config.py`, `cli/sch/harness.py`, `cli/sch/runtime.py`, `cli/sch/commands/` (`status.py`, `list.py`, `stop.py`, `delete.py`, `destroy.py`), `cli/sch/dashboard.py`, `cli/sch/awsteardown.py`, `image/app/main.py`, `image/scripts/sch-build-image.sh`, `bin/verify-*.sh`.
- **Specs and guides.** `docs/specs/security/` (`iam-workspace-control-api.md`, `iam-workspace-registry.md`, `owner-scoped-workspace-storage.md`, `workspace-registry.md`), `docs/workspaces.md`, `docs/deploy.md`, `docs/security.md`, `SECURITY.md`, `docs/getting-started.md`.

## Launch

Only after the maintainer's GO, on a workspace whose `feat/task-20` branch contains this task, one subtask at a time and in order:

`sch task <workspace> "Carry out Backlog task TASK-20.N end to end, unattended, as its description and the parent TASK-20 say."`
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Subtasks TASK-20.1 to TASK-20.5 are Done; their acceptance criteria together cover the feature
- [ ] #2 Operator-side live check passes: a test stack deployed with two IAM users (and one Identity Center user when available) runs bin/verify-isolation.sh successfully; the executing agent leaves this criterion unchecked
<!-- AC:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [ ] #1 cli, infra and tunnel suites, the image-side tests (with sandboxed paths) and bin/verify-docs.sh pass
- [ ] #2 Journal entry and masterplan updated as AGENTS.md requires; implementation notes list every assumption and the verification results
- [ ] #3 Work committed on feat/task-20, never pushed
<!-- DOD:END -->
