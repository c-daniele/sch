# IAM Workspace Control API

> Domain: [Security](../README.md) · Status: Implemented · Source: rationalized from openspec specs (2026-08-28)

## Purpose

Define the IAM-authenticated control-plane contract for resolving, listing, rotating, and deleting owner-scoped workspaces. All registry operations flow through this API so that ownership is established by AWS IAM, never by client input.

## Scope

In scope:

- API Gateway endpoint authentication (`AWS_IAM`) and caller identity derivation
- Resolve-or-create, owner-filtered listing, session rotation, single and bulk deletion operations
- Immutability and conditional-write guards enforced at the API boundary
- HTTP error contract (status codes and when they apply)

Out of scope:

- CLI wiring and local-mode fallback (see [iam-workspace-registry](iam-workspace-registry.md))
- Infrastructure provisioning of the table, Lambda, and API (see [workspace-registry](workspace-registry.md))
- Storage identity derivation and S3 layout (see [owner-scoped-workspace-storage](owner-scoped-workspace-storage.md))
- Storage backend selection and session epochs (see [selectable-workspace-storage](selectable-workspace-storage.md))

## Requirements

### Authentication and identity

- **R1.** The system SHALL expose workspace resolution, listing, session rotation, and single/bulk deletion through an API Gateway endpoint authenticated with AWS IAM (`AuthorizationType: AWS_IAM`).
- **R2.** The API SHALL derive the caller identity solely from the verified API Gateway request context (`requestContext.identity.userArn`). It MUST NOT accept an owner identifier from the client, and the stored owner key SHALL be a hash of the caller ARN so principal details never become DynamoDB keys or runtime path components.
- **R3.** A request without valid AWS IAM authentication SHALL be rejected without reading or writing any workspace record.

### Operations

- **R4.** The API SHALL provide a resolve-or-create operation accepting a logical workspace name and optional harness and storage backend for a new workspace, and returning the owner-scoped workspace record. Creation of an absent record SHALL be guarded by a conditional write so concurrent first resolutions produce exactly one persisted record, and every concurrent response SHALL identify that same record.
- **R5.** When an owner resolves an existing workspace while requesting a different harness or storage backend, the API SHALL reject the request (409) and preserve the persisted values.
- **R6.** The list operation SHALL query only the authenticated caller's workspace partition. A principal with no records SHALL receive a successful response with an empty collection.
- **R7.** Session rotation SHALL atomically replace the runtime session ID of an existing record only, and SHALL reject the request (404) for an unknown workspace and (409) for a record in deletion state.
- **R8.** Single and bulk deletion SHALL act only on records selected by the authenticated principal; an unknown or not-owned workspace SHALL return not found without creating a record. Deletion state-machine semantics are specified in [iam-workspace-registry](iam-workspace-registry.md).
- **R9.** A request with an invalid workspace name, a non-JSON or non-object body, or an out-of-enum harness/storage value SHALL be rejected (400) without mutating any record.
- **R10.** An unknown operation SHALL return not found (404); an internal failure SHALL return a server error (500) without partial visible mutation of the addressed record.

## Behavior

Endpoints (all `AWS_IAM`; clients sign SigV4 for `execute-api`):

| Method | Path | Effect |
| --- | --- | --- |
| POST | `/workspaces/{name}/resolve` | Resolve-or-create; `201` with `created=true` or `200` with `created=false` |
| GET | `/workspaces` | Owner-filtered list |
| POST | `/workspaces/{name}/rotate-session` | New runtime session ID, epoch incremented |
| DELETE | `/workspaces/{name}` | Owner-scoped single deletion |
| DELETE | `/workspaces` | Owner-scoped bulk deletion with per-workspace results |

Error contract: `400` invalid name/body/enum value; `404` unknown workspace or operation; `409` record being deleted, or immutable harness/storage divergence; `500` control-plane or incomplete-deletion failure. The response body always carries an `error` message the CLI surfaces verbatim.

## Invariants

- **I1.** No API operation accepts or trusts a client-supplied owner identity.
- **I2.** Every workspace record is uniquely keyed by (hashed owner, logical workspace name).
- **I3.** The harness and storage backend of an existing record cannot be changed through any API operation.
- **I4.** Concurrent identical resolve-or-create calls result in exactly one persisted record.

## Cross-references

- [iam-workspace-registry](iam-workspace-registry.md) — registry-enabled CLI behavior and deletion state machine
- [workspace-registry](workspace-registry.md) — implemented control plane (table, Lambda, API Gateway)
- [owner-scoped-workspace-storage](owner-scoped-workspace-storage.md) — workspace identity and S3 prefix rules
- [selectable-workspace-storage](selectable-workspace-storage.md) — storage backend and sessionEpoch semantics
- [MANIFESTO](../../../MANIFESTO.md)
- Code: `infra/workspace_registry_handler.py` (handler), `infra/agent_runtime.yaml` (`WorkspaceRegistryApi`, `WorkspaceRegistry*Method`), `cli/sch/workspace_registry.py` (client), `infra/test_workspace_registry_handler.py` (handler tests)
