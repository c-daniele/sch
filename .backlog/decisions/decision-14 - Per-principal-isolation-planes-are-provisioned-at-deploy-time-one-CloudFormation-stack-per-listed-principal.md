---
id: decision-14
title: >-
  Per-principal isolation planes are provisioned at deploy time, one
  CloudFormation stack per listed principal
date: '2026-09-27 15:13'
status: accepted
---
## Context

Registry mode gives every caller its own workspace names, but all workspaces run on one
shared AgentCore runtime whose execution role reads every checkpoint. AgentCore authorizes
`InvokeAgentRuntime*` on the runtime ARN, not on the session, so any principal allowed to
invoke the runtime who learns a session ID can join that session. `SECURITY.md` promised
per-user isolation that was not enforced. The community asked for real per-user isolation.

A first attempt (TASK-8) created runtimes, roles and resource policies from the registry
Lambda at request time. It was reviewed, found not deployable and rolled back. The maintainer
decided on 2026-09-27 that per-user segregation is required, that the per-user resources are
created at deploy time, and that TASK-8 is not reused (TASK-20).

## Decision

Isolation is an opt-in deploy-time switch, `ISOLATED_PRINCIPALS`, that requires the
workspace registry. The final defaults of TASK-20 are:

1. **Principals.** An explicit allow-list with the entry forms `user:<iam-user>`,
   `sso:<permission-set>/<username>` and `role:<role>`, resolved by `infra/deploy.sh` with IAM
   reads; unknown or ambiguous entries fail the deploy before any stack changes. `sso:`
   usernames are accepted unverified with a warning, because they cannot be checked without
   `identitystore` access (maintainer, final).
2. **Owner identity.** Derived from the bound identity (unique IDs, plus the Identity Center
   username), never from the session ARN. Isolation off keeps today's derivation.
3. **Planes.** One CloudFormation stack per principal from `infra/user_plane.yaml`, deployed
   after the runtime stack, updated on every deploy and deleted when the principal leaves
   the list. `Fn::ForEach` in the runtime stack is rejected (maintainer, final): with one
   stack per principal a failure on one principal does not block or roll back the others,
   the runtime stack stays far from the 500-resource limit, and removing a principal is a
   stack deletion.
4. **Runtime lock.** Deny-only `AWS::BedrockAgentCore::ResourcePolicy` resources on each user
   runtime and its DEFAULT endpoint, conditioned on `aws:userid`, with a stop-only exception
   for the registry role. With isolation on, the shared runtime denies all user data-plane
   actions, so every user, the operator included, must be listed.
5. **Execution role.** The shared role's permissions move into customer managed policies
   attached to both the shared role and every plane role (one definition in the template).
   A permissions boundary denies SCH-owned shared resources (registry and Telegram tables,
   SCH log groups, SCH runtime and Lambda configuration, plane parameters, changes to SCH
   identities) and nothing else.
6. **Access role.** Per principal, trusted only by the owner through `aws:userid`, read-only
   on the owner's checkpoint trees, used by `sch status`, `sch list --remote-check` and
   `sch dashboard`.
7. **Storage layout.** An owner segment `o.<16 hex>` right after each top-level prefix
   (`checkpoints/o.<k>/<ws>/…`, `checkpoint-generations/o.<k>/…`, `workspace-writers/o.<k>/…`,
   `builds/o.<k>/…`). It cannot collide with registry-off names, which never contain `.`,
   and the existing lifecycle rules keep applying unchanged.
8. **Shim.** The owner prefix comes only from `SCH_OWNER_PREFIX` in the runtime environment;
   a disagreeing payload is rejected; nothing is written into the workspace root before the
   bootstrap restore; runtimes without the variable behave as today.
9. **Telegram.** Refused with isolation on; it stays a single-operator feature. *Superseded on
   2026-10-02 by [decision-17](decision-17%20-%20Telegram-on-an-isolated-stack-binds-to-exactly-one-listed-principal.md):
   Telegram stays single-operator but binds to one listed principal (`TELEGRAM_PRINCIPAL`).*
10. **Migration.** None; owners start with empty namespaces.
11. **Removing a principal.** The next deploy deletes its plane; its storage is retained.
12. **Logs.** Task prompts are no longer written to the runtime logs, in every mode.

Choices made while writing the spec (TASK-20.1), within those defaults:

- **Cross-owner S3 denial uses one constant bucket policy** keyed on the plane roles' name
  pattern and on the role tag `sch-owner` through the policy variable
  `${aws:PrincipalTag/sch-owner}`, instead of one statement per owner. Per-owner statements
  would hit the 20 KB bucket-policy limit at about two dozen principals and would force a
  runtime-stack change for every added principal. Plane role trusts never allow
  `sts:TagSession`, so a session tag cannot override the role tag.
- **The registry finds a caller's plane through one SSM parameter per plane**
  (`/<project>/<env>/planes/<ownerKey>`) created by the plane stack, after the locks. The
  registry role gains read-only `ssm:GetParameter` on those parameters; the runtime ARN of a
  plane is not deterministic, and the registry Lambda is deployed before the planes.
- **The owner key is the first 16 hex characters of `sha256("<kind>:<boundId>")`**, and the
  registry `ownerId` is the full hash, so isolated owners get a namespace distinct from the
  hashed caller ARNs of today's records.
- **Owner trees are denied to every non-SCH principal, deletes included.** A removed
  owner's retained storage is purged by the owner before removal (`sch delete --all`), by a
  boundary administrator, or by `sch destroy`.

## Consequences

- Isolation holds against listed users, their agents and other account principals.
  Boundary administrators (IAM write, AgentCore control-plane write, bucket-policy write,
  CloudFormation or Lambda deploy rights, plane-parameter or registry-table write) can
  remove it; the guides must say so.
- Each listed principal costs one AgentCore runtime against the account quota (100 by
  default); the deploy checks it first.
- The shared role's policy documents become customer managed policies in every mode. Its
  effective permissions are unchanged, but the "byte-identical role at defaults" wording of
  runtime-capability-tuning R1 and I1 no longer holds and is amended when TASK-20.3 lands.
  A managed policy holds 6,144 characters, less than the 10,240 of inline role policies, which
  caps the Bedrock allow-list length.
- With isolation on, registry-off use and the shared runtime are unavailable on that stack,
  and existing records and checkpoints are not migrated. Telegram, refused here, binds to one
  listed principal since decision-17.
- Residual risks are listed in the spec: ReadOnlyAccess configuration reads, the plane
  creation window, unverified `sso:` usernames, simulator limits, shared image rebuild,
  shared data bucket, `role:` entries, the registry mapping cache, the managed-policy size.
- Spec: [`docs/specs/security/per-principal-isolation.md`](../../docs/specs/security/per-principal-isolation.md).
  Tasks: TASK-20 and TASK-20.1 to TASK-20.5.
