# Owner-Scoped Workspace Storage

> Domain: [Security](../README.md) · Status: Implemented · Source: rationalized from openspec specs (2026-08-28)

## Purpose

Ensure registry-enabled workspace runtime markers and checkpoint storage use stable, owner-scoped identities so that identical logical workspace names belonging to different principals never share remote state.

## Scope

In scope:

- The owner-scoped workspace identity: derivation, determinism, and path safety
- Use of the identity in runtime markers and checkpoint S3 key paths
- Non-adoption of legacy checkpoint prefixes

Out of scope:

- Registry API and CLI behavior (see [iam-workspace-registry](iam-workspace-registry.md))
- Storage backend selection and operational roots (see [selectable-workspace-storage](selectable-workspace-storage.md))

## Requirements

- **R1.** For registry-enabled commands, the client SHALL send the registry-provided owner-scoped workspace identity to the runtime in place of the user-visible logical workspace name. The identity SHALL be safe for runtime marker and checkpoint S3 key paths: alphanumeric or `-`/`_` only, starting alphanumeric, at most 128 characters. The client SHALL reject a registry response whose identity violates this pattern.
- **R2.** The identity SHALL be deterministic per owner and logical workspace: the registry SHALL return the same identity whenever the same owner resolves the same logical workspace record, from any client machine. The implemented derivation is `ws-` + lowercased base32 of `sha256(ownerId + "\0" + logicalWorkspace)`, truncated to 40 characters, where `ownerId` is the hashed caller ARN defined in [iam-workspace-control-api](iam-workspace-control-api.md) (R2).
- **R3.** The system SHALL NOT automatically map a registry-created owner-scoped workspace identity to a legacy checkpoint prefix derived only from its logical workspace name. When an owner creates a registry-enabled workspace with the name of a legacy local workspace, the runtime SHALL use the owner-scoped checkpoint prefix and SHALL NOT restore the legacy prefix implicitly.
- **R4.** Owner-scoped deletion SHALL purge and verify absent all SCH-controlled S3 versions under exactly the identity-derived prefixes `checkpoints/{identity}/`, `checkpoint-generations/{identity}/`, and `workspace-writers/{identity}.json` (deletion state machine: [iam-workspace-registry](iam-workspace-registry.md), R7–R8).

## Behavior

- Two principals each create a workspace named `my-workspace`: their runtime markers and checkpoint object prefixes are distinct (`checkpoints/<identity-a>/...` vs `checkpoints/<identity-b>/...`), and neither sees the other's checkpoints, history, or writer claim.
- An owner resolving a previously created workspace from a different client machine (fresh local index) gets the same workspace identity as prior registry-enabled operations used.
- Deleting a workspace removes only the S3 objects under that identity's three prefixes; the same logical name owned by another principal keeps its record, session, and prefixes unchanged.

## Invariants

- **I1.** Distinct (owner, logical workspace) pairs always map to distinct workspace identities, hence distinct runtime marker and checkpoint prefixes.
- **I2.** The same (owner, logical workspace) pair always maps to the same workspace identity.
- **I3.** A workspace identity is always valid as an S3 key path component and never encodes the raw logical name or principal ARN.

## Cross-references

- [iam-workspace-control-api](iam-workspace-control-api.md) — caller identity and owner-key hashing
- [iam-workspace-registry](iam-workspace-registry.md) — resolution and deletion flows that carry the identity
- [workspace-registry](workspace-registry.md) — implemented identity computation and purge
- [selectable-workspace-storage](selectable-workspace-storage.md) — backend roots the identity-scoped state lives under
- [MANIFESTO](../../../MANIFESTO.md)
- Code: `infra/workspace_registry_handler.py` (`_owner_id`, `_workspace_identity`, `_purge`), `cli/sch/workspace_registry.py` (`IDENTITY_RE`, `RegistryWorkspace`), `cli/sch/deletion.py` (prefix list), `infra/test_workspace_registry_handler.py`
