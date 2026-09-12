# Workspace Registry

> Domain: [Security](../README.md) · Status: Implemented · Source: rationalized from openspec change `add-central-workspace-registry` + implemented code (2026-08-28)

## Purpose

Specify the implemented central workspace registry: an optional Lambda + API Gateway control plane over a single DynamoDB table that is the source of truth for workspace existence, harness binding, storage backend, ownership, and runtime session mapping when `SCH_WORKSPACE_REGISTRY_URL` is configured.

## Scope

In scope:

- Provisioned infrastructure: DynamoDB table, Lambda handler, API Gateway REST API, IAM role
- Owner derivation from the verified caller ARN and its hashing
- Record schema, resolve/rotate/delete semantics, and conditional-write guards as implemented
- The delta between the original proposal and the implemented design

Out of scope:

- CLI-side resolution and fallback rules (see [iam-workspace-registry](iam-workspace-registry.md))
- Authentication contract and error codes (see [iam-workspace-control-api](iam-workspace-control-api.md))
- Workspace identity → S3 prefix rules (see [owner-scoped-workspace-storage](owner-scoped-workspace-storage.md))

## Requirements

### Infrastructure

- **R1.** The registry stack SHALL consist of a DynamoDB table (partition key `ownerId`, sort key `logicalWorkspace`, pay-per-request billing, point-in-time recovery and encryption at rest enabled), a Python 3.12 Lambda function, and a regional API Gateway REST API whose methods all use `AWS_IAM` authorization. The stack SHALL be created only when the `EnableWorkspaceRegistry` CloudFormation condition is true.
- **R2.** The Lambda execution role SHALL be least-privilege: DynamoDB item actions on the registry table only; `s3:ListBucketVersions`/`s3:DeleteObjectVersion` on the checkpoint bucket only; `bedrock-agentcore:StopRuntimeSession`; and CloudWatch Logs. Clients SHALL NOT require DynamoDB permissions — only `execute-api:Invoke` on the registry API.
- **R3.** The Lambda environment SHALL pin `DEFAULT_HARNESS` (opencode) and `DEFAULT_STORAGE` (s3), which MUST stay equal to the CLI defaults so a workspace's effective harness and backend never depend on which component materialized it first.

### Owner derivation and record schema

- **R4.** The handler SHALL derive the owner from `requestContext.identity.userArn` of the API Gateway event only, SHALL fail the request if it is absent, and SHALL store only `sha256(callerArn)` as `ownerId`. Principal details MUST NOT appear in table keys, records, or runtime path components.
- **R5.** A record SHALL contain `ownerId`, `logicalWorkspace`, `runtimeSessionId` (`sch-registry-<uuid4>`), `harness`, `workspaceIdentity`, `storage`, `sessionEpoch`, `createdAt`, `updatedAt`, and `schemaVersion`. The public record returned to clients SHALL exclude timestamps and SHALL expose `deletionState` only when it is `deleting`; the list operation SHALL filter deleting records out entirely.
- **R6.** The handler SHALL validate the logical workspace name against `^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$`, the harness against the enum (`opencode`, `claude`, `pi` — kept in sync with the CLI's accepted values), and storage against (`s3`, `session`); violations SHALL be rejected with 400.

### Operations as implemented

- **R7.** Resolve: read the record; if it is `deleting`, return 409; if present, reconcile missing storage/epoch fields via an `if_not_exists` update (legacy records become `session`/epoch 1), enforce harness and storage immutability (409 on divergence), and return the record with `created=false`. If absent, create via `put_item` with `ConditionExpression attribute_not_exists(ownerId) AND attribute_not_exists(logicalWorkspace)`; on a conditional-check failure re-read and return the winner's record with `created=false` — never overwrite.
- **R8.** Rotate: a single conditional update (`attribute_exists(ownerId) AND attribute_not_exists(deletionState)`) that sets a fresh `sch-registry-<uuid4>` session ID and `sessionEpoch = if_not_exists(sessionEpoch, 0) + 1`; conditional-check failure maps to 404 (unknown) or 409 (deleting) by re-reading the item.
- **R9.** Delete: read the item (404 if absent); transition to `deletionState=deleting` with `ConditionExpression attribute_not_exists(deletionState)`; stop the runtime session (tolerating not-found/conflict); purge and verify the identity-derived S3 prefixes (paginated `list_object_versions` including delete markers, batched `delete_objects`, then a verification pass that raises if anything remains); finally delete the item with `ConditionExpression deletionState = :deleting`. Any failure after entering deletion state SHALL return 500 with the public record so a retry resumes the same identity — the item and its metadata are never recreated or re-identified.
- **R10.** Bulk delete: query the caller's partition only, sort by workspace name, and apply R9 to each target independently, collecting per-workspace `deleted`/`failed` results and aggregate counts (HTTP 200 only if nothing failed, else 500). Records created after the snapshot are out of scope; a failing target keeps its recoverable record.

### Client contract

- **R11.** The CLI client (`SCH_WORKSPACE_REGISTRY_URL`) SHALL sign SigV4 requests for `execute-api` using credentials from `aws configure export-credentials`, SHALL apply a request timeout, and SHALL strictly validate responses: required string fields present, workspace identity matching the path-safe pattern, storage within the enum, and `sessionEpoch` a non-negative integer. An invalid or non-2xx response SHALL raise an error carrying the registry's `error` message.

## Behavior

- `POST /workspaces/{name}/resolve` → `201 {"workspace": {...}, "created": true}` on first resolution for that owner, `200 ... "created": false` afterwards, `409` while deleting or on harness/storage divergence.
- `GET /workspaces` → `{"workspaces": [...]}` for the caller only, deleting records omitted.
- `POST /workspaces/{name}/rotate-session` → new session ID, epoch+1, harness/storage/identity preserved.
- `DELETE /workspaces/{name}` → `{"workspace": {...}, "deleted": true}` after stop + verified purge; `{"error": "workspace deletion incomplete", ...}` with 500 on interruption (retryable).
- `DELETE /workspaces` → `{"results": [{"workspace", "status"}...], "deleted": n, "failed": m}`.
- The client mirrors every resolved/rotated record into its local index as a cache; the registry remains authoritative.

## Delta from the original proposal

The original proposal (`openspec/changes/add-central-workspace-registry/proposal.md`, git tag `pre-openspec-retirement`) envisioned a directly accessed DynamoDB table; the implementation differs as follows. Where this spec is silent, the proposal text does not apply.

1. **Control plane, not direct table access.** The proposal had `bin/sch` reading/writing DynamoDB with operator-side least-privilege table permissions and `infra/registry.yaml`. Implemented: a Lambda + API Gateway (`AWS_IAM`) control plane in `infra/agent_runtime.yaml`; clients sign SigV4 `execute-api` requests and need no DynamoDB grants.
2. **Server-side identity, not client STS.** The proposal bound ownership via client-side STS `GetCallerIdentity`. Implemented: the owner is the caller ARN from the verified API Gateway request context (R4), stored hashed; the client never sends an owner.
3. **No offline fallback.** The proposal made the local directory a cache with offline fallback and explicit refresh. Implemented: the local index is a written-through cache only; when the registry is configured, a control-plane error fails the command before any AgentCore call — no silent cached-session fallback.
4. **No automatic migration.** The proposal imported existing local index entries into the registry on first use. Implemented: legacy local-index mode is preserved only when `SCH_WORKSPACE_REGISTRY_URL` is unset; there is no automatic import.
5. **No fleet view.** The proposal included `sch list --all` for a fleet view. Implemented: listing is owner-scoped only; no cross-owner listing exists.
6. **Delegation of deletion.** The proposal called the registry a purely operator-side concern with runtime image unchanged. Implemented: the registry also performs deletion — it stops the AgentCore runtime session and purges/verifies the owner-scoped S3 prefixes before removing the record, with a resumable `deleting` state.
7. **Extended record.** The proposal's record carried status and last-checkpoint metadata. Implemented: records carry harness, storage backend, `sessionEpoch`, and a `schemaVersion`; last-checkpoint metadata is not stored.

## Invariants

- **I1.** `ownerId` is always the SHA-256 hash of the verified caller ARN; no request path can set it.
- **I2.** Creation, rotation, and deletion are all conditional writes; no operation overwrites an existing record's identity or silently creates over a deleting record.
- **I3.** The client never needs or uses DynamoDB access; the only authorized writer is the registry Lambda.
- **I4.** After a deletion failure, a retry targets the original `runtimeSessionId` and `workspaceIdentity`; no new identity or session ID is allocated during cleanup.

## Cross-references

- [iam-workspace-control-api](iam-workspace-control-api.md) — API contract this implementation satisfies
- [iam-workspace-registry](iam-workspace-registry.md) — CLI-side rules (resolution, fallback, legacy mode)
- [owner-scoped-workspace-storage](owner-scoped-workspace-storage.md) — identity derivation and purged prefixes
- [selectable-workspace-storage](selectable-workspace-storage.md) — storage/sessionEpoch fields in records
- [MANIFESTO](../../../MANIFESTO.md)
- Code: `cli/sch/workspace_registry.py`, `infra/workspace_registry_handler.py`, `infra/agent_runtime.yaml` (`WorkspaceRegistryTable`, `WorkspaceRegistryRole`, `WorkspaceRegistryFunction`, `WorkspaceRegistryApi`), `cli/tests/test_workspace_registry.py`, `infra/test_workspace_registry_handler.py`, `bin/verify-iam-workspace-registry.sh`
