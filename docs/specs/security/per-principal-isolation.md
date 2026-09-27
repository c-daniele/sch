# Per-Principal Workspace Isolation

> Domain: [Security](../README.md) · Status: Partially verified (TASK-20; design TASK-20.1; runtime side R26–R35 by TASK-20.2; templates, deploy and teardown R1–R6, R10–R25, R27, R39, R44–R48 by TASK-20.3; registry and CLI R7–R9, R36–R38, R40–R43 by TASK-20.4; verify scripts and guides by TASK-20.5. Every requirement is implemented and covered by unit tests, Access Analyzer and simulator evidence; not yet checked live: a deploy with planes (CloudFormation acceptance of the locks), the joint runtime and endpoint lock evaluation (R16, R17, X4), the principal-ID field of R36 and the 403 text of R37, the bucket policy against real callers (R24), all covered by `bin/verify-isolation.sh`) · Decision: [decision-14](../../../.backlog/decisions/decision-14%20-%20Per-principal-isolation-planes-are-provisioned-at-deploy-time-one-CloudFormation-stack-per-listed-principal.md)

## Purpose

Give every listed SCH user of one AWS account a workspace plane that other users, and
the agents those users run, cannot join or read. The operator lists the principals at
deploy time. `infra/deploy.sh` then creates one isolated plane per principal: an
AgentCore runtime locked to that principal, an execution role limited to that principal's
storage, and a read-only access role for the principal's own CLI. At request time nothing
creates or changes IAM or AgentCore resources.

Why a runtime per principal. AgentCore authorizes `InvokeAgentRuntime*` on the runtime ARN,
not on the runtime session. On a shared runtime, anyone allowed to invoke it who learns a
session ID can join that session (trace: user B runs
`aws bedrock-agentcore invoke-agent-runtime --agent-runtime-arn <shared> --runtime-session-id <A's id>`
and gets A's microVM). The only per-principal boundary AWS offers is a separate runtime per
principal whose resource policy denies everyone else, plus storage that only that runtime's
role can reach.

## Scope

In scope:

- The deploy-time switch, its entry forms and how entries are resolved to bound identities
- The owner key derived from a bound identity, and the per-owner storage layout
- The plane stack (`infra/user_plane.yaml`): runtime, resource-policy locks, execution role,
  permissions boundary, access role, plane-mapping parameter
- Changes to the runtime stack (`infra/agent_runtime.yaml`) when isolation is on: shared
  managed policies, bucket policy, shared-runtime lock, registry role
- Registry, shim and CLI behavior with isolation on
- Threat model, residual risks and the evidence each slice must produce

Out of scope:

- Isolation across AWS accounts, OAuth inbound authorization
- Repository-token handling in `image/scripts/init-workspace.sh`
- Per-owner Telegram (a follow-up; Telegram is refused with isolation on, R44)
- Migration of existing registry records or checkpoints (none, R47)
- Behavior with isolation off: unchanged, see [owner-scoped-workspace-storage](owner-scoped-workspace-storage.md),
  [workspace-registry](workspace-registry.md) and [iam-workspace-registry](iam-workspace-registry.md)

## Requirements

### Switch and entries

- **R1.** Isolation SHALL be controlled by the deploy-time switch `ISOLATED_PRINCIPALS`, a
  comma-separated list of entries (whitespace around entries ignored). Empty or unset means
  isolation is off: the deploy creates no resource of this spec and the rendered templates
  keep their isolation-off shape ([decision-4](../../../.backlog/decisions/decision-4%20-%20Optional-features-are-deploy-time-switches-with-inert-defaults.md)).
- **R2.** A non-empty `ISOLATED_PRINCIPALS` SHALL require `ENABLE_WORKSPACE_REGISTRY=true`;
  `infra/deploy.sh` SHALL fail before changing any stack otherwise.
- **R3.** The accepted entry forms SHALL be exactly:

  | Entry | Resolved with | Bound identity (`boundId`) | `aws:userid` pattern |
  | --- | --- | --- | --- |
  | `user:<iam-user-name>` | `iam:GetUser` | the user's unique ID (`AIDA…`) | `AIDA…` |
  | `sso:<permission-set>/<username>` | `iam:ListRoles` with path prefix `/aws-reserved/sso.amazonaws.com/` | `<role-unique-id>:<username>` (`AROA…:<username>`) | `AROA…:<username>` |
  | `role:<role-name>` | `iam:GetRole` | the role's unique ID (`AROA…`) | `AROA…:*` |

  For `sso:`, the permission-set role is the single role under
  `/aws-reserved/sso.amazonaws.com/` (any region sub-path) whose name matches
  `AWSReservedSSO_<permission-set>_<16 hex>` exactly. Identity Center sets the role session
  name to the username, so the caller's `aws:userid` is `AROA…:<username>`.
- **R4.** `infra/deploy.sh` SHALL resolve every entry with IAM read calls only and SHALL fail
  before changing any stack when an entry is malformed, unknown, or ambiguous. Ambiguous
  means: an `sso:` permission set matching zero or several roles; a `role:` entry naming a
  role under `/aws-reserved/sso.amazonaws.com/` (use `sso:`); two entries resolving to the
  same bound identity; an `sso:` entry and a `role:` entry resolving to the same role.
- **R5.** The Identity Center username of an `sso:` entry cannot be verified without
  `identitystore` access. `infra/deploy.sh` SHALL accept it unverified, keep its case as
  written, and print a warning naming the entry. This is residual risk X3.
- **R6.** Before changing any stack, `infra/deploy.sh` SHALL check the AgentCore runtime quota:
  existing runtimes in the region, plus planes to create, minus planes to delete, plus the
  shared runtime when it does not exist yet, MUST NOT exceed the account quota: the
  Service Quotas value of `Total Agents per Account` (service `bedrock-agentcore`, code
  `L-F4575653`) when readable, first as applied then as the AWS default, else the
  documented default of 100. It SHALL also refuse
  `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` and `ENABLE_TELEGRAM_INTERACTION=true` together
  with isolation (R44).

### Owner identity

- **R7.** The owner of a request SHALL be derived from the bound identity, never from the
  session ARN. The owner string is `<kind>:<boundId>` with `kind` in `user`, `sso`, `role`.
  `ownerId = sha256(owner string)` as 64 lowercase hex; `ownerKey` = the first 16 hex
  characters of `ownerId`; `ownerPrefix = "o." + ownerKey`.
- **R8.** Consequences that implementations MUST preserve: a new session name of the same IAM
  user or `role:` role (for example `botocore-session-<epoch>`) is the same owner; two
  Identity Center users of one permission set are two owners; a re-created IAM user with the
  same name is a new owner (new unique ID, so a new plane and an empty namespace); a
  caller-chosen role session name never becomes a separate owner of a `role:` entry.
- **R9.** With isolation off the registry SHALL keep today's derivation
  (`sha256(callerArn)`, [workspace-registry](workspace-registry.md) R4).

### Planes

- **R10.** Each listed principal SHALL get one CloudFormation stack named
  `<project>-<env>-plane-<ownerKey>`, created from `infra/user_plane.yaml`, tagged
  `sch:deployment=<project>-<env>` and `sch:owner-key=<ownerKey>`. `infra/deploy.sh` SHALL
  deploy the plane stacks after the runtime stack, one stack per principal, and a failed
  plane SHALL NOT block or roll back the others; the deploy exits non-zero after trying all.
- **R11.** Every deploy SHALL update every plane stack with the image reference (digest when
  pinned, as in [decision-9](../../../.backlog/decisions/decision-9%20-%20Reference-the-runtime-image-by-digest-every-image-building-deploy-is-a-new-runtime-version.md))
  and the runtime configuration of the shared runtime (idle timeout, max lifetime, Pi default
  model, heap and build-job caps, capability policies, ReadOnlyAccess toggle, image rebuild).
  User runtimes SHALL change only during a deploy, never from `sch` observation commands.
- **R12.** A plane stack whose owner key is no longer listed (orphan) SHALL be deleted by the
  next deploy. `infra/deploy.sh` finds plane stacks by the name prefix `<project>-<env>-plane-`
  AND the `sch:deployment` tag. With `ISOLATED_PRINCIPALS` empty, every plane stack of the
  deployment is an orphan.
- **R13.** A plane stack SHALL contain exactly: the user runtime, two
  `AWS::BedrockAgentCore::ResourcePolicy` locks (R16), the execution role and its permissions
  boundary (R18–R21), the access role (R22–R23), and the plane-mapping parameter (R14).
  It SHALL NOT contain custom resources, and `infra/deploy.sh` SHALL NOT call
  `put-resource-policy`.
- **R14.** The plane-mapping parameter SHALL be an `AWS::SSM::Parameter` named
  `/<project>/<env>/planes/<ownerKey>` (type String) whose value is the JSON
  `{"schemaVersion": 1, "ownerKey", "ownerPrefix", "runtimeArn", "accessRoleArn"}`. It holds
  no secret and no bound identity. It SHALL depend on both locks (R16), so the registry
  never hands out a runtime ARN before the runtime is locked.
- **R15.** Resource names SHALL be deterministic: runtime `<project>_<env>_o_<ownerKey>`,
  execution role `<project>-<env>-o-<ownerKey>-BedrockAgentCore` (the name contains
  `BedrockAgentCore`, so the deploy principal's `iam:PassRole` scope documented in
  `docs/getting-started.md` still applies), access role `<project>-<env>-o-<ownerKey>-access`,
  boundary `<project>-<env>-o-<ownerKey>-boundary`. With `ProjectName` matching
  `^[a-z][a-z0-9]{2,7}$` and a 3-letter environment these fit the runtime (48) and role (64)
  name limits. All plane roles have path `/` and the tag `sch-owner=<ownerKey>`.

### Runtime locks

- **R16.** Each user runtime and its DEFAULT endpoint (`<runtime-arn>/runtime-endpoint/DEFAULT`)
  SHALL carry a deny-only resource policy whose `Resource` is that exact ARN:
  1. `DenyNonOwnerDataPlane`: `Deny`, `Principal: "*"`, actions
     `bedrock-agentcore:InvokeAgentRuntime`, `InvokeAgentRuntimeForUser`,
     `InvokeAgentRuntimeCommand`, `InvokeAgentRuntimeCommandShell`,
     `InvokeAgentRuntimeWithWebSocketStream`, `InvokeAgentRuntimeWithWebSocketStreamForUser`,
     `GetAgentCard`, condition `StringNotLike aws:userid <owner pattern>`.
  2. `DenyNonOwnerStop`: `Deny`, `Principal: "*"`, action `bedrock-agentcore:StopRuntimeSession`,
     conditions `StringNotLike aws:userid <owner pattern>` AND
     `ArnNotEquals aws:PrincipalArn <registry role ARN>`.

  The policies contain no `Allow` and name no principal ARN, so no principal has to exist
  or carry a particular path. Access still needs the caller's own identity policy.
- **R17.** With isolation on, the shared runtime and its DEFAULT endpoint SHALL carry, in the
  runtime stack, a resource policy that denies the R16 data-plane actions to everyone
  (`Principal: "*"`, no condition) and denies `StopRuntimeSession` to everyone except the
  registry role. Every SCH user, the operator included, must therefore be listed, and
  registry-off use of that stack is not available.

### Execution role and boundary

- **R18.** The shared runtime role and every plane execution role SHALL get their SCH
  permissions from the same customer managed policies defined once in
  `infra/agent_runtime.yaml`: the base execution policy (ECR pull, runtime logs, metrics,
  Bedrock and bedrock-mantle grants, checkpoint read/write), each enabled capability policy,
  the escape-hatch policy, and the image-rebuild session policy. The runtime stack SHALL
  output their ARNs (`SharedRuntimePolicyArns`). `ReadOnlyAccess` SHALL be attached to plane
  roles exactly when it is attached to the shared role. The Telegram session policy SHALL
  NOT be attached to plane roles. Nothing is copied at run time.
- **R19.** The plane execution role trust SHALL be the shared role's trust
  (`bedrock-agentcore.amazonaws.com`, `aws:SourceAccount`, `aws:SourceArn` in the account) and
  SHALL NOT allow `sts:TagSession`.
- **R20.** Each plane execution role SHALL have a CloudFormation-managed permissions boundary
  that allows `*` and denies only SCH-owned shared resources of the deployment:
  - `dynamodb:*` on `table/<project>-<env>-*` and its sub-resources (registry and Telegram tables);
  - log reads, i.e. every read-level CloudWatch Logs action that accepts a log-group or
    log-stream resource (service reference checked 2026-09-27): `logs:FilterLogEvents`,
    `GetDataProtectionPolicy`, `GetLogEvents`, `GetLogGroupFields`, `GetLogRecord`,
    `GetQueryResults`, `GetTransformer`, `StartLiveTail`, `StartQuery`, `Unmask`, on
    `/aws/bedrock-agentcore/runtimes/<project>_<env>_*`,
    `/aws/bedrock/agentcore/<project>-<env>-*` and `/aws/lambda/<project>-<env>-*`, the
    owner's own runtime log group included. List-level actions (`DescribeLogStreams`,
    `DescribeMetricFilters`, `DescribeSubscriptionFilters`, `ListTagsLogGroup`) return no
    log content and stay allowed;
  - configuration reads `bedrock-agentcore:GetAgentRuntime`, `GetAgentRuntimeEndpoint`,
    `GetResourcePolicy` on `runtime/<project>_<env>_*` and its endpoints
    (`ListAgentRuntimeVersions` and `ListAgentRuntimeEndpoints` accept no resource and
    return no configuration, so they are not listed); `lambda:Get*` on
    `function:<project>-<env>-*`; `lambda:ListFunctions` (not resource-scoped, it returns
    environment variables); `ssm:GetParameter*` on `parameter/<project>/<env>/*`;
  - IAM and STS changes to SCH identities: `iam:Tag*`, `iam:Untag*`, `iam:Put*`,
    `iam:Attach*`, `iam:Detach*`, `iam:Delete*`, `iam:Update*` on `role/<project>-<env>-*`
    and `policy/<project>-<env>-*`, and `sts:AssumeRole` on `role/<project>-<env>-*`.

  The boundary SHALL NOT restrict S3 (cross-owner storage denial is the bucket policy's job,
  R24), so build-source uploads, data-bucket writes and every other grant of the shared role
  keep working.
- **R21.** Each plane runtime environment SHALL equal the shared runtime's environment minus
  every `SCH_TELEGRAM_*` variable, plus `SCH_OWNER_PREFIX=<ownerPrefix>`.

### Access role

- **R22.** Each plane SHALL have an access role whose trust policy allows only
  `sts:AssumeRole` (never `sts:TagSession`) from the account root principal with the
  condition `StringLike aws:userid <owner pattern>`, and whose only permissions are
  `s3:GetObject` on `checkpoints/<ownerPrefix>/*` and `workspace-writers/<ownerPrefix>/*`
  and `s3:ListBucket` on the checkpoint bucket with `s3:prefix` limited to those two trees.
  Maximum session duration: 1 hour.
- **R23.** The owner's CLI SHALL read its checkpoint objects (task status, manifests, writer
  claims, checkpoint listing) only through the access role (R40). Callers need
  `sts:AssumeRole` on their access role in their own identity policy.

### Bucket policy

- **R24.** With isolation on, the checkpoint bucket SHALL carry a bucket policy, defined once
  in the runtime stack and independent of the number of principals, with three deny
  statements. `<plane roles>` is `arn:aws:iam::<account>:role/<project>-<env>-o-*`; `<tag>` is
  `${aws:PrincipalTag/sch-owner}`; the owner trees are `checkpoints/o.*`,
  `checkpoint-generations/o.*`, `workspace-writers/o.*` and `builds/o.*`.
  1. `DenyOwnerTreesToOthers`: `Deny` `s3:*` on the owner trees unless
     `aws:PrincipalArn` is ArnLike `<plane roles>` or equals the registry role, the task
     watchdog role or the image-rebuild build role.
  2. `DenyForeignObjectsToPlaneRoles`: `Deny` `s3:*` for `aws:PrincipalArn` ArnLike
     `<plane roles>` with `NotResource` = the bucket ARN and the four trees with the owner
     segment `o.<tag>`.
  3. `DenyForeignListingToPlaneRoles`: `Deny` `s3:ListBucket` and `s3:ListBucketVersions`
     for `<plane roles>` when `s3:prefix` is StringNotLike every `<tree>/o.<tag>/*`.
     (`s3:ListBucketMultipartUploads` does not support `s3:prefix`; Access Analyzer reports
     an ERROR if it is added here.)

  A plane role without the tag matches no `NotResource` entry and no prefix, so it is denied
  (fail closed). Per-owner statements are not used: they would grow with every principal
  and hit the 20 KB bucket-policy limit around two dozen principals.
- **R25.** The trust policies of plane roles SHALL NOT allow `sts:TagSession`, because a
  session tag `sch-owner` would override the role tag that R24 relies on.

### Storage layout

- **R26.** With `SCH_OWNER_PREFIX` set, every SCH object SHALL live under the owner segment:

  | Object | Isolation off (unchanged) | Isolation on |
  | --- | --- | --- |
  | Checkpoint objects, manifest | `checkpoints/<ws>/<name>` | `checkpoints/o.<k>/<ws>/<name>` |
  | Task status | `checkpoints/<ws>/task-status.json` | `checkpoints/o.<k>/<ws>/task-status.json` |
  | Generations | `checkpoint-generations/<ws>/<gen>/<name>` | `checkpoint-generations/o.<k>/<ws>/<gen>/<name>` |
  | Writer claim | `workspace-writers/<ws>.json` | `workspace-writers/o.<k>/<ws>.json` |
  | Build source | `builds/<scope>/source.zip` | `builds/o.<k>/<scope>/source.zip` |

  `<ws>` is the registry workspace identity and `<k>` the owner key. Workspace names match
  `[A-Za-z0-9][A-Za-z0-9_-]{0,127}` and never contain `.`, so an `o.<16 hex>` segment
  never collides with a registry-off workspace.
- **R27.** The bucket lifecycle rules SHALL keep their current prefixes
  (`checkpoint-generations/` with the `active=candidate` tag filter, `builds/`,
  non-current version expiry), which cover both layouts.
- **R28.** Checkpoint manifests store absolute artifact keys. Keys written under the new
  layout are owner-prefixed from the start; nothing is copied or moved between layouts.

### Shim

- **R29.** The shim SHALL take the owner prefix only from `SCH_OWNER_PREFIX` in the runtime
  environment, validated against `^o\.[0-9a-f]{16}$`. An invalid value SHALL make every
  invocation fail with an explicit error, before any S3 access or workspace write. The
  prefix SHALL NOT come from invoke payloads or marker files.
- **R30.** An invoke payload carrying `owner_prefix` that differs from the environment value
  SHALL be rejected (`{"status": "rejected", "error": …}`) before any other effect. A payload
  without it is accepted.
- **R31.** With the prefix set, nothing SHALL be written into the workspace root
  (`SCH_WORKSPACE_ROOT`) before the bootstrap restore has completed, because the L2 restore
  promotes only into an empty root. Concurrent invocations arriving during the bootstrap
  wait for it or answer without writing (how each action does so:
  [workspace-checkpointing](../workspace-lifecycle/workspace-checkpointing.md) R10a).
- **R32.** The shim SHALL NOT write task prompts to the runtime logs, in every mode: the
  headless argv is logged with the prompt replaced by a length marker (`<prompt: N chars>`).
- **R33.** `sch-build-image` SHALL upload build sources to `builds/<ownerPrefix>/<scope>/source.zip`
  when `SCH_OWNER_PREFIX` is set, and to `builds/<scope>/source.zip` otherwise.
- **R34.** A runtime without `SCH_OWNER_PREFIX` SHALL behave exactly as before this spec,
  except for R32.

### Task watchdog

- **R35.** The task watchdog SHALL list both layouts: under `checkpoints/`, a common prefix
  matching `^o\.[0-9a-f]{16}/$` is an owner tree whose children are workspaces; any other
  common prefix is a workspace. Its IAM stays Get/Put on `checkpoints/*/task-status.json`
  and List on `checkpoints/`.

### Registry

- **R36.** With isolation on (`ISOLATION_ENABLED=true` in the registry Lambda environment),
  the registry SHALL map the caller from the verified request context: the principal ID
  (`requestContext.identity.user`, the caller's `aws:userid`) and the account
  (`requestContext.identity.accountId`, which MUST equal the deployment account, the account
  of the registry function's own ARN; a mismatch is a `403`). A principal
  ID without `:` gives the candidate owner string `user:<id>`; `AROA…:<name>` gives
  `sso:AROA…:<name>` then `role:AROA…`. The first candidate whose plane-mapping parameter
  exists is the owner; R4 guarantees at most one exists. The exact field carrying the
  principal ID is verified live by the parent task's check (unverified).
- **R37.** A caller with no plane SHALL get HTTP 403 and no record SHALL be read or created.
  The error names the identity to add, derived from the caller ARN: `user:<name>` for an IAM
  user, `sso:<permission-set>/<session-name>` for an assumed `AWSReservedSSO_<ps>_<hex>` role,
  `role:<name>` for any other assumed role.
- **R38.** With isolation on, records SHALL be keyed by `ownerId` (R7), SHALL store the
  immutable `ownerPrefix`, and every response carrying records SHALL include
  `"isolation": true` and, per record, `plane: {runtimeArn, accessRoleArn, ownerPrefix}`.
  Every successful response (list, resolve, rotate, delete, bulk delete) SHALL also carry the
  caller's `plane` at top level, so an owner without records still learns its plane.
  Deletion SHALL stop the session on the plane runtime and purge the owner-prefixed keys of
  R26 for the stored `ownerPrefix`, build sources `builds/<ownerPrefix>/<identity>/` included
  (resumable as in [iam-workspace-registry](iam-workspace-registry.md) R8). A malformed
  plane-mapping parameter, or an SSM error other than not-found, fails the request (`500`)
  before any table access.
- **R39.** The registry role SHALL keep only: its table; `ssm:GetParameter` on
  `parameter/<project>/<env>/planes/*`; checkpoint purge
  (`s3:ListBucketVersions`, `s3:DeleteObjectVersion`); `bedrock-agentcore:StopRuntimeSession`
  on `runtime/<project>_<env>_*`; its own logs. No role used at request time (registry,
  watchdog, webhook, execution or access roles) SHALL be able to create or change IAM,
  AgentCore runtimes, resource policies or plane parameters. Plane lookups MAY be cached for
  at most 60 seconds.

### CLI

- **R40.** With a registry response carrying `"isolation": true`, every `sch` command SHALL
  use the record's `plane.runtimeArn` for AgentCore calls, send `owner_prefix` in invoke
  payloads, and read owner checkpoint objects through `plane.accessRoleArn` with credentials
  refreshed before they expire (a long-running `sch dashboard` keeps working past the first
  expiry). A missing or malformed `plane` SHALL fail the command; there is no fallback to the
  shared runtime ARN, `SCH_RUNTIME_ARN` or the cached runtime ARN. A command that needs the
  runtime before any registry response (for example `sch info`) SHALL ask the registry first
  (`GET /workspaces`) and fail if it cannot. Access-role credentials come from
  `sts:AssumeRole` (one-hour sessions), live only in memory, reach `aws s3api` subprocesses
  through their environment (never argv or disk), and are renewed five minutes before they
  expire. Owner reads use the owner-segment keys of R26: `checkpoints/<ownerPrefix>/<ws>/`
  (task status, manifest) and `workspace-writers/<ownerPrefix>/` (writer claims).
- **R41.** The CLI SHALL validate `plane` strictly: `runtimeArn` is a
  `bedrock-agentcore` runtime ARN whose runtime name is `<project>_<env>_o_<ownerKey>`,
  `accessRoleArn` an IAM role ARN, `ownerPrefix` matches `^o\.[0-9a-f]{16}$`, and the
  three agree on the owner key.
- **R42.** No `sch` command SHALL create or update a runtime, IAM role or resource policy.
  The runtime-version pin ([runtime-provisioning](../platform/runtime-provisioning.md) R15d)
  reads the version of the plane runtime.
- **R43.** With isolation off, `sch` output and behavior SHALL be unchanged.

### Telegram, removal, migration, teardown

- **R44.** Telegram notifications and interaction SHALL NOT be available with isolation on:
  no bot token, chat ID or table name reaches a user runtime, no Telegram session policy is
  attached to a plane role, and `infra/deploy.sh` refuses the combination (R6). Telegram
  remains a single-operator feature of isolation-off stacks.
- **R45.** Removing a principal from `ISOLATED_PRINCIPALS` deletes its plane on the next deploy
  (R12). Its storage SHALL be retained (the bucket has `DeletionPolicy: Retain` and the
  owner trees are not touched).
- **R46.** Retained storage of a removed owner is reachable only by the registry and SCH
  service roles and by boundary administrators (R24). The documented purge is: before
  removal, the owner runs `sch delete --all`; after removal, a boundary administrator
  purges `*/o.<ownerKey>/` with the bucket policy temporarily removed, or `sch destroy`
  purges the whole bucket.
- **R47.** There SHALL be no migration: with isolation on, owners start with empty namespaces
  and existing registry records and checkpoints are left untouched.
- **R48.** `sch destroy` SHALL delete every plane stack of the deployment (R12 discovery)
  before the runtime stack, and purge both layouts when it empties the bucket. The bucket
  policy disappears with the runtime stack; `sch destroy` also drops any remaining policy
  of the checkpoint bucket before purging it. A failure to list the plane stacks stops the
  teardown before anything is deleted.

## Behavior

### Deploy flow with isolation on

1. Validate switches (R2, R6 Telegram) and the managed-policy sizes (X9), resolve every
   entry (R3–R5), compute owner keys (R7), compute planes to create, update and delete,
   check the runtime quota (R6) (`infra/isolation_plan.py`, IAM, CloudFormation, AgentCore
   and Service Quotas reads only). Any failure stops here; no stack has changed.
2. Bootstrap stack and image build, as today.
3. Delete orphan plane stacks (R12) and wait. They go before the runtime stack update
   because turning isolation off removes the bucket policy that confines plane roles; a
   removed principal's runtime must be gone first. Storage is retained (R45).
4. Runtime stack with `IsolationEnabled=true`: shared managed policies, bucket policy (R24),
   shared-runtime lock (R17), registry with `ISOLATION_ENABLED=true` and its plane-parameter
   read (R39).
5. One `aws cloudformation deploy` per listed principal on `<project>-<env>-plane-<ownerKey>`
   with the owner pattern and the configuration read back from the runtime stack as deployed
   (its image tag and digest, lifecycle, Pi model, heap and build-job caps, `ReadOnlyAccess`
   toggle, `SharedRuntimePolicyArns`, image-rebuild project, registry role), so the shared
   and user runtimes cannot drift. A plane left in `ROLLBACK_COMPLETE` by an earlier failed
   creation is deleted and created again.
6. Print one line per entry: entry, owner key, runtime ARN, stack status. Exit non-zero if any
   plane operation failed.

With `ISOLATED_PRINCIPALS` empty, step 1 only looks for plane stacks of the deployment; if
that lookup fails, the deploy warns and continues (nothing of this spec is created).

Example: `ISOLATED_PRINCIPALS="user:alice, sso:Developers/bob@example.com"` on project `sch`,
environment `dev`, account `111122223333` gives two stacks, for example
`sch-dev-plane-3f9c0e2a7b1d4c65` and `sch-dev-plane-a04be91c22d7f310`, runtimes
`sch_dev_o_3f9c0e2a7b1d4c65-<suffix>` and `sch_dev_o_a04be91c22d7f310-<suffix>`, and a warning
that the Identity Center username `bob@example.com` was not verified.

### Runtime lock policy (plane runtime, IAM user owner)

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DenyNonOwnerDataPlane",
      "Effect": "Deny",
      "Principal": "*",
      "Action": [
        "bedrock-agentcore:InvokeAgentRuntime",
        "bedrock-agentcore:InvokeAgentRuntimeForUser",
        "bedrock-agentcore:InvokeAgentRuntimeCommand",
        "bedrock-agentcore:InvokeAgentRuntimeCommandShell",
        "bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStream",
        "bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStreamForUser",
        "bedrock-agentcore:GetAgentCard"
      ],
      "Resource": "arn:aws:bedrock-agentcore:eu-west-1:111122223333:runtime/sch_dev_o_3f9c0e2a7b1d4c65-AbCdEf1234",
      "Condition": {"StringNotLike": {"aws:userid": "AIDAEXAMPLEALICE0001"}}
    },
    {
      "Sid": "DenyNonOwnerStop",
      "Effect": "Deny",
      "Principal": "*",
      "Action": "bedrock-agentcore:StopRuntimeSession",
      "Resource": "arn:aws:bedrock-agentcore:eu-west-1:111122223333:runtime/sch_dev_o_3f9c0e2a7b1d4c65-AbCdEf1234",
      "Condition": {
        "StringNotLike": {"aws:userid": "AIDAEXAMPLEALICE0001"},
        "ArnNotEquals": {"aws:PrincipalArn": "arn:aws:iam::111122223333:role/sch-dev-workspace-registry-role"}
      }
    }
  ]
}
```

The endpoint policy is identical with `Resource` set to
`…:runtime/sch_dev_o_3f9c0e2a7b1d4c65-AbCdEf1234/runtime-endpoint/DEFAULT`. For an `sso:` owner
the pattern is `AROAEXAMPLEDEVS00001:bob@example.com`; for a `role:` owner `AROAEXAMPLEBOTS00001:*`.

### Who can do what (trace)

| Caller | Own plane invoke | Other plane invoke | Own checkpoints | Other checkpoints |
| --- | --- | --- | --- | --- |
| Alice (listed IAM user) | allowed (identity policy + no deny) | denied by R16 | via access role | denied: access role trust (R22), bucket policy (R24.1) |
| Alice's agent (plane execution role) | not granted | denied by R16 | read/write | denied by R24.2/R24.3 |
| Bob, second user of the same permission set | own plane only | denied: `aws:userid` differs in the username part | via own access role | denied |
| Unlisted role session with `bedrock-agentcore:*` and `s3:*` on `*` | registry 403 (R37) | denied by R16 | none | denied by R24.1 |
| Registry role | never invokes | `StopRuntimeSession` only | purge | purge on the owner's request only |

### Registry mapping

`POST /workspaces/my-ws/resolve` by `arn:aws:sts::111122223333:assumed-role/AWSReservedSSO_Developers_0123456789abcdef/bob@example.com`
with principal ID `AROAEXAMPLEDEVS00001:bob@example.com`: candidate
`sso:AROAEXAMPLEDEVS00001:bob@example.com` → `ownerKey` → parameter
`/sch/dev/planes/<ownerKey>` found → record keyed by `ownerId`, response
`{"isolation": true, "workspace": {…, "plane": {"runtimeArn": …, "accessRoleArn": …, "ownerPrefix": "o.<ownerKey>"}}, "created": true}`.
The same call by an unlisted user `carol` answers
`403 {"error": "caller arn:aws:iam::111122223333:user/carol is not an isolated principal of this deployment; add user:carol to ISOLATED_PRINCIPALS and redeploy"}`.

### Threat model

- **Assets.** Each owner's live sessions (shell, agent, provider keys staged in the microVM),
  checkpoints and generations (source code, harness state), writer claims, task status and
  prompts, build sources, registry records; SCH configuration secrets (Telegram token and
  webhook secret in runtime and Lambda environments).
- **Listed users and their agents** are untrusted with respect to each other. A user's agent
  runs with the plane execution role and may be prompt-injected; it holds everything the
  shared role holds, `ReadOnlyAccess` included, capped by the boundary (R20) and the bucket
  policy (R24).
- **Other principals of the account** without boundary-administrator rights are untrusted:
  they cannot join a plane (R16, R17) or read owner trees (R24.1), whatever their identity
  policies allow.
- **Boundary administrators are trusted and can remove any lock.** They are principals with
  any of: IAM write; AgentCore control-plane write (`UpdateAgentRuntime`,
  `DeleteAgentRuntime`, `PutResourcePolicy`, `DeleteResourcePolicy`, as in
  `BedrockAgentCoreFullAccess`); `s3:PutBucketPolicy` or `s3:DeleteBucketPolicy` on the
  checkpoint bucket; CloudFormation deploy rights with `iam:PassRole`; Lambda code or
  configuration updates of SCH functions; `ssm:PutParameter` on the plane parameters (it
  could redirect a user's CLI to another runtime, which would then receive that user's
  provider keys); DynamoDB write on the registry table. Listed users SHOULD NOT hold these
  rights; the guides say so.
- **AWS itself** (AgentCore, S3, IAM evaluation) is trusted.

### Residual risks

- **X1. ReadOnlyAccess configuration reads.** The boundary denies SCH runtime and Lambda
  configuration, SCH log groups, SCH tables and plane parameters (R20). Still readable by an
  agent: names and ARNs of SCH resources; IAM role trust policies and plane stack parameters,
  which reveal the listed identities (unique IDs, Identity Center usernames); CloudFormation
  templates; the shared image in ECR; the shared image-rebuild CodeBuild logs; key names,
  sizes and timestamps of the checkpoint bucket for principals outside the plane roles that
  hold `s3:ListBucket` (object content stays denied); key names of in-progress multipart
  uploads for any principal holding `s3:ListBucketMultipartUploads`. None of these is a
  secret or workspace content.
- **X2. Plane creation window.** A user runtime exists for a short time before CloudFormation
  attaches its resource policies. During that window only identity policies apply. The ARN
  is not published yet (the plane parameter is created only after both locks, R14), so
  the risk is low. A rejected policy rolls the stack back and the runtime is deleted.
- **X3. Unverified `sso:` usernames.** A mistyped username binds the plane to a different
  Identity Center user, or to nobody. `aws:userid` comparisons are case-sensitive; the
  username must be written as Identity Center reports it.
- **X4. Simulator limits.** The IAM policy simulator evaluates one resource policy per call,
  so it does not model the joint runtime and endpoint evaluation; each policy is simulated
  separately. It fills `aws:userid` from the caller, so Identity Center callers are simulated
  in custom mode with explicit context entries. Only the operator-side live check
  (`bin/verify-isolation.sh`, two principals) covers the joint evaluation.
- **X5. Shared image rebuild.** With `ENABLE_SESSION_IMAGE_REBUILD=true`, every owner can start
  the one CodeBuild project, whose role pushes the image all planes run on and reads every
  `builds/` source (the existing risk R1 of that feature now spans owners). Enable it only
  when every listed principal is trusted with the image.
- **X6. Shared data bucket.** `RUNTIME_DATA_BUCKET_ARN` is operator-owned and shared: every
  owner's agent reads the whole data bucket and writes under `transcribe/` and `textract/`.
- **X7. `role:` entries.** Every principal that can assume the role is that one owner.
- **X8. Mapping cache.** After a principal is removed, the registry may return the deleted
  plane for up to 60 seconds (R39); calls then fail on the missing runtime.
- **X9. Managed-policy size.** A customer managed policy holds at most 6,144 characters where
  the inline role policy held 10,240, which caps the Bedrock allow-list length (about 20
  exact model IDs; wildcard entries cover many models each); the deploy fails on an oversized base
  policy (conservative estimate) or escape-hatch policy before changing any stack, rather
  than truncating it.

### Evidence each slice produces

- Policy documents: IAM Access Analyzer `ValidatePolicy` without ERROR findings, reproduced
  by a committed read-only script.
- Execution role, boundary and bucket policy: `iam:SimulateCustomPolicy` runs (identity
  policy + boundary + bucket policy) showing own-tree read/write, build-source upload,
  data-bucket write, and denial of other owner trees, the registry table, the Telegram tables
  and other SCH log groups, with redacted output committed.
- Runtime locks: simulator runs per policy (runtime, endpoint) showing denial for another IAM
  user, another Identity Center user of the same permission set and an unlisted role
  session, each holding `bedrock-agentcore:*` on `*`, and no denial for the owner; the X4
  limits recorded next to the output.
- AWS requests built by new code: botocore `ParamValidator` in tests.
- Live: `bin/verify-isolation.sh` with two principals, operator-side (parent task).
- Design-time check (TASK-20.1, placeholder account): the R16 lock and R24 bucket policy
  shapes shown in this spec validate with Access Analyzer without findings, and custom-mode
  simulation gives the expected decisions (own tree allowed, other tree, flat layout,
  untagged plane role and other-prefix listing denied; owner invoke allowed, same
  permission-set user, other IAM user and registry invoke denied, registry stop allowed).
  This is not the TASK-20.3 evidence, which runs on the rendered templates.

## Invariants

- **I1.** With `ISOLATED_PRINCIPALS` empty, no resource of this spec exists and no request path
  changes.
- **I2.** Every listed principal has exactly one plane, and every plane belongs to exactly one
  listed principal after a successful deploy.
- **I3.** No request-time component creates or changes IAM, AgentCore runtimes, resource
  policies or plane parameters.
- **I4.** The owner of a request is a function of the bound identity only; a caller cannot
  choose it.
- **I5.** A user runtime answers data-plane requests only from its owner, plus
  `StopRuntimeSession` from the registry role.
- **I6.** Owner trees (`*/o.<k>/`) are readable and writable only by that owner's execution
  role, read by that owner's access role, and reachable by the registry, watchdog and
  image-rebuild service roles within their identity policies.
- **I7.** The owner prefix a runtime writes under comes only from its deploy-time environment.
- **I8.** A command with isolation on never falls back to the shared runtime.

## Cross-references

- [owner-scoped-workspace-storage](owner-scoped-workspace-storage.md): workspace identity, isolation-off layout
- [workspace-registry](workspace-registry.md), [iam-workspace-control-api](iam-workspace-control-api.md),
  [iam-workspace-registry](iam-workspace-registry.md): registry contract this spec extends
- [runtime-capability-tuning](runtime-capability-tuning.md): the capability policies shared with plane roles
- [runtime-provisioning](../platform/runtime-provisioning.md): runtime stack, versions, R15d
- [session-image-rebuild](../platform/session-image-rebuild.md): build sources, CodeBuild role
- [headless-task-execution](../access-surfaces/headless-task-execution.md): task status and the watchdog
- [decision-4](../../../.backlog/decisions/decision-4%20-%20Optional-features-are-deploy-time-switches-with-inert-defaults.md),
  [decision-9](../../../.backlog/decisions/decision-9%20-%20Reference-the-runtime-image-by-digest-every-image-building-deploy-is-a-new-runtime-version.md),
  [decision-14](../../../.backlog/decisions/decision-14%20-%20Per-principal-isolation-planes-are-provisioned-at-deploy-time-one-CloudFormation-stack-per-listed-principal.md)
- AWS: [AgentCore resource-based policies](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/resource-based-policies.html),
  [AgentCore actions](https://docs.aws.amazon.com/service-authorization/latest/reference/list_bedrock-agentcore.html),
  [`AWS::BedrockAgentCore::ResourcePolicy`](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-bedrockagentcore-resourcepolicy.html),
  [IAM policy variables](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_variables.html)
- Code implementing R26–R35 (TASK-20.2): `image/app/main.py` (`OWNER_PREFIX`, `_owner_rejection`,
  `_s3_key`/`_generation_key`/`_writer_claim_key`, `_redact_argv`), `image/scripts/sch-build-image.sh`,
  `infra/task_watchdog_handler.py` (`list_workspaces`); tests `image/app/test_owner_prefix.py`,
  `image/app/test_sch_build_image.py`, `infra/test_task_watchdog_handler.py`
- Code implementing R1–R6, R10–R25, R39, R44–R48 (TASK-20.3): `infra/user_plane.yaml`,
  `infra/agent_runtime.yaml` (`IsolationEnabled`, `AgentRuntimeBasePolicy` and the other
  managed policies, `CheckpointBucketPolicy`, `SharedRuntimeLock`, `SharedRuntimeEndpointLock`,
  `SharedRuntimePolicyArns`), `infra/isolation_plan.py`, `infra/deploy.sh` (preflight, isolation
  plane helpers), `cli/sch/awsteardown.py` (`plane_stacks`), `cli/sch/commands/destroy.py`;
  tests `infra/test_isolation_templates.py`, `infra/test_isolation_plan.py`,
  `infra/test_isolation_deploy.py`, `cli/tests/test_destroy.py`; evidence scripts
  `infra/validate_isolation_policies.py`, `infra/simulate_isolation.py` (renderer
  `infra/cfn_render.py`) and their reports under `docs/history/`
  ([Access Analyzer](../../history/isolation-evidence-access-analyzer.md),
  [simulator](../../history/isolation-evidence-simulator.md))
- Code implementing R7–R9, R36–R38, R40–R43 (TASK-20.4): `infra/workspace_registry_handler.py`
  (`_isolated_owner`, `_lookup_plane`, `_suggested_entry`, `_purge_prefixes`), `cli/sch/plane.py`
  (`parse_plane`, `adopt`, `active_plane`, `access_env`), `cli/sch/workspace_registry.py`,
  `cli/sch/config.py` (`runtime_arn`), `cli/sch/runtime.py` (`_inject_owner_prefix`),
  `cli/sch/commands/` (`status.py`, `list.py`, `info.py`), `cli/sch/dashboard.py`; tests
  `infra/test_workspace_registry_isolation.py`, `cli/tests/test_isolation_cli.py`
- Verification tooling (TASK-20.5): `bin/verify-isolation.sh` (the live two-principal check),
  `bin/lib/verify-target.sh` and `cli/sch/verify_support.py` (plane runtime, registry session,
  owner-segment keys and access-role reads for every `bin/verify-*.sh`); tests
  `cli/tests/test_verify_support.py`, `cli/tests/test_verify_scripts.py` (the scripts against
  a fake world that enforces these rules)
- Backlog: TASK-20 and subtasks TASK-20.1 to TASK-20.5
