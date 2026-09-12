# IAM Workspace Registry

> Domain: [Security](../README.md) · Status: Implemented · Source: rationalized from openspec specs (2026-08-28)

## Purpose

Specify owner-scoped workspace resolution semantics and the registry-backed behavior of `sch` workspace commands: every registry-enabled logical workspace lives in a namespace derived from the authenticated IAM principal, and the CLI resolves, rotates, lists, and deletes workspaces through the registry instead of treating the local index as source of truth.

## Scope

In scope:

- Client-side resolution semantics (owner scoping, repeatability, atomic creation)
- Registry-backed CLI workspace commands and their failure behavior
- Owner-scoped deletion, recoverable deletion state, and bulk deletion
- Owner-scoped listing and explicit session rotation
- Legacy local-mode compatibility when no registry endpoint is configured

Out of scope:

- API authentication contract and endpoint definitions (see [iam-workspace-control-api](iam-workspace-control-api.md))
- Control-plane infrastructure and implementation (see [workspace-registry](workspace-registry.md))
- Workspace identity derivation and S3 prefix layout (see [owner-scoped-workspace-storage](owner-scoped-workspace-storage.md))
- Storage backend selection (see [selectable-workspace-storage](selectable-workspace-storage.md))

## Requirements

### Resolution

- **R1.** The system SHALL resolve every registry-enabled logical workspace in a namespace derived from the IAM principal authenticated by the control plane (identity derivation: [iam-workspace-control-api](iam-workspace-control-api.md), R2). The client SHALL NOT supply, select, or override the owner namespace.
- **R2.** Two distinct IAM principals resolving the same logical name SHALL get two distinct workspace records with distinct runtime session IDs and workspace identities. A principal re-resolving an existing workspace without a reset SHALL get its persisted runtime session ID and harness.
- **R3.** An absent owner-scoped workspace SHALL be created atomically, with a random valid AgentCore runtime session ID, a persisted harness, and a deterministic owner-scoped workspace identity (creation guard: [iam-workspace-control-api](iam-workspace-control-api.md), R4).

### Registry-backed CLI commands

- **R4.** When `SCH_WORKSPACE_REGISTRY_URL` is configured, `sch` SHALL resolve workspace metadata through the registry before every workspace-scoped command that opens, invokes, attaches to, checks, stops or resets a workspace. The CLI SHALL use the returned runtime session ID for its existing direct AgentCore operations, and SHALL mirror the returned record into its local index as a cache.
- **R5.** Deletion SHALL use the registry's owner-scoped deletion operation so it cannot create an absent record and so deletion state can block concurrent resolution. This applies to single deletion and to bulk deletion: `sch delete --all` SHALL invoke the owner-scoped bulk deletion operation without deriving targets from its local cache.
- **R6.** When a registry-enabled workspace command cannot resolve or delete its workspace due to a control-plane error, the CLI SHALL fail before invoking AgentCore and SHALL NOT silently use a cached session ID.

### Deletion

- **R7.** The registry SHALL expose an IAM-authenticated, owner-scoped deletion operation. It MUST act only on the record selected by the authenticated principal and logical workspace name, transition that record to a deletion state before remote cleanup, and remove the record only after the runtime is quiescent and all SCH-controlled S3 versions have been purged and verified absent (purged prefixes: [owner-scoped-workspace-storage](owner-scoped-workspace-storage.md), R4).
- **R8.** Registry deletion SHALL be idempotent and resumable. A failure after entering deletion state MUST preserve enough immutable target metadata to retry safely; normal resolve/list behavior MUST NOT expose the record as usable; and a later delete request SHALL continue cleanup against the original workspace identity rather than allocating a new identity or session ID.
- **R9.** The registry SHALL expose a bulk deletion operation for the authenticated owner. It SHALL snapshot all usable and already-deleting records for that owner, transition each usable target to the recoverable deletion state, attempt every target independently, and return a deterministic per-workspace result plus aggregate counts. It MUST NOT inspect or mutate records belonging to another owner, and records created after the snapshot SHALL be outside the operation.

### Listing and rotation

- **R10.** The system SHALL list only workspace records owned by the authenticated IAM principal; `sch list` output SHALL exclude workspace records owned by every other principal, and SHALL omit records being deleted.
- **R11.** The system SHALL allow the authenticated owner to rotate the runtime session ID of one of its workspace records while retaining its logical workspace name, harness and owner-scoped identity. `sch reset-session` SHALL request a registry session rotation and persist the returned mapping in its local cache.

### Legacy compatibility

- **R12.** When `SCH_WORKSPACE_REGISTRY_URL` is not configured, `sch` SHALL retain the existing local workspace-index behavior: read or create the local workspace index as before.

## Behavior

- `sch task my-workspace --harness opencode` with the registry configured resolves the workspace through the registry first; two operators running the same command get isolated sessions and checkpoint prefixes (verified end-to-end by `bin/verify-iam-workspace-registry.sh` with two IAM profiles).
- `sch delete my-workspace` invokes the registry deletion endpoint without resolving-or-creating; a record being deleted answers with an explicit conflict (409) to resolution attempts and is omitted from `sch list`.
- `sch delete --all` snapshots the owner's usable and deleting records; a partial failure continues with the remaining targets and reports the failed one, which keeps its recoverable record for retry. An owner with no records gets success with zero counts.
- `sch reset-session my-workspace` shows the current mapping, requires confirmation, then rotates via the registry and persists the new session ID.
- Any registry error (network, 4xx, 5xx, malformed response) aborts the command with the registry's error message before any AgentCore call.

## Invariants

- **I1.** The client never sends an owner identifier; ownership follows only from the signed IAM request.
- **I2.** A registry-enabled command never falls back to a cached session ID when the registry is unreachable or rejects the request.
- **I3.** A registry deletion request never creates a workspace record.
- **I4.** A record in deletion state is never returned as usable by resolve or list.
- **I5.** With `SCH_WORKSPACE_REGISTRY_URL` unset, no registry code path runs.

## Cross-references

- [iam-workspace-control-api](iam-workspace-control-api.md) — API contract, authentication, guards
- [workspace-registry](workspace-registry.md) — implemented control plane and delta from the original proposal
- [owner-scoped-workspace-storage](owner-scoped-workspace-storage.md) — identity and checkpoint prefix rules
- [selectable-workspace-storage](selectable-workspace-storage.md) — storage backend carried in registry records
- [MANIFESTO](../../../MANIFESTO.md)
- Code: `cli/sch/workspace_registry.py` (SigV4 client), `cli/sch/harness.py` (`resolve_harness` registry path), `cli/sch/commands/delete.py`, `cli/sch/commands/reset_session.py`, `cli/sch/commands/stop.py`, `cli/sch/commands/list.py`, `cli/tests/test_workspace_registry.py`, `bin/verify-iam-workspace-registry.sh`
