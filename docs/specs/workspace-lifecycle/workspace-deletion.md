# Workspace deletion

> Domain: [Workspace lifecycle](../README.md) · Status: Implemented · Source: rationalized from openspec specs (2026-08-28)

## Purpose

Define safe, irreversible deletion of one or all SCH workspaces and their SCH-managed
state: explicit confirmation, mandatory quiescence, verified purge of the SCH-controlled
remote state, ordered finalization, idempotent retries, and clean recreation.

## Scope

In scope:
- `sch delete <workspace>` and `sch delete --all` semantics, confirmation, and exit codes.
- Scope snapshot and targets of bulk deletion (local mode and registry mode).
- Quiescence before purge, complete purge of SCH-controlled S3 state, verification.
- Ordered remote finalization, local cleanup boundaries, idempotency and retry.
- Clean recreation of a deleted workspace's name; `session` backend retention caveat.

Out of scope:
- Checkpoint content and lifecycle-based expiry (non-deletion) — see [workspace-checkpointing](workspace-checkpointing.md) and [workspace-persistence](workspace-persistence.md).
- Registry data model and record semantics beyond the deletion interaction — see [iam-workspace-registry](../security/iam-workspace-registry.md).

## Requirements

### Explicit, confirmed deletion

**R1.** SCH SHALL expose `sch delete <workspace> [--yes]` and `sch delete --all [--yes]`
as irreversible operations separate from `stop` and `reset-session`; `<workspace>` and
`--all` MUST be mutually exclusive (both or neither is a usage error without any
mutation).

**R2.** For a single workspace, without `--yes`, the CLI MUST show which classes of data
will be deleted and require typing the exact workspace name. For `--all`, without
`--yes`, it MUST show the scope and the list of targets and require the exact phrase
`DELETE ALL WORKSPACES`. A missing or mismatched confirmation MUST terminate with exit
code 1 without local or remote mutations (including without stopping the session).

**R3.** With `--yes`, SCH SHALL proceed without reading stdin while keeping the same
checks and the same fail-closed semantics.

### Bulk deletion scope and result

**R4.** In local mode, `sch delete --all` SHALL acquire, before the confirmation, a
snapshot of the workspaces indexed on the current machine and of the still-incomplete
local deletion intents. With the registry enabled, it SHALL acquire, through a dedicated
bulk operation, all the usable or being-deleted records belonging solely to the
authenticated IAM principal. Workspaces created after the snapshot SHALL NOT be part of
the operation. An empty scope SHALL terminate successfully (exit code 0) without a
destructive prompt.

**R5.** The bulk deletion SHALL apply to each target the same quiescence, verified purge,
and finalization workflow as the single deletion. A failure on one target MUST NOT
prevent the attempt on the other targets. At the end, SCH SHALL emit a deterministic
per-workspace summary with outcome `deleted` or `failed`, return exit code 1 if at least
one target fails, and preserve, for the failed targets only, the state required for
retry. Repeating `sch delete --all` after a partial failure SHALL include the incomplete
deletions in the snapshot and idempotently resume the remaining targets.

### Quiescence before purge

**R6.** SCH MUST prevent new resolutions of the workspace, when the registry is enabled,
and MUST successfully stop the current `runtimeSessionId` before deleting the persistent
state. If the runtime cannot be stopped or rendered inactive, the purge MUST NOT begin;
an already absent or stopped session SHALL be considered quiescent. The stop is performed
without performing a new checkpoint; the purge begins only after the stop has succeeded.
On a failed stop, SCH SHALL report the error, preserve the remote and local state
required for retry, and MUST NOT delete the checkpoints. A client resolving or opening a
workspace marked as being deleted SHALL be rejected by the registry without creating a
new session or modifying the record.

### Verified remote purge

**R7.** For each workspace, SCH SHALL delete from the checkpoint bucket all object
versions and all delete markers under `checkpoints/<identity>/` and
`checkpoint-generations/<identity>/`, plus all versions of the key
`workspace-writers/<identity>.json`, where `identity` is the local name or the
owner-scoped identity returned by the registry. The command MUST verify that no version
or delete marker remains in the three scopes before considering the purge successful. A
workspace whose three scopes contain no objects or versions SHALL be considered
successfully purged without error.

**R8.** On a partial S3 error, SCH SHALL exit with an error, MUST NOT remove the final
mapping, and SHALL allow the deletion to be repeated idempotently.

### Ordered finalization and local cleanup

**R9.** SCH SHALL delete the registry record, if configured, and the local state only
after the verified remote purge. The local cleanup SHALL include the workspace index and
status, sync bindings and baselines, and the default ACP mirror managed by SCH; it MUST
NOT delete the source directory associated via `--sync`, an explicit ACP mirror outside
the managed root, local repositories, Git branches, or other data not owned by SCH. The
complete operation and each of its retries SHALL be idempotent.

### Clean recreation

**R10.** After a completed deletion, reusing the same name SHALL create a new workspace
without adopting the deleted workspace's checkpoints, writer claims, session IDs,
harness, or storage (a new session identity is assigned; no data of the deleted
workspace is restored).

**R11.** For backend `session`, SCH SHALL state explicitly that stopping and removing the
mapping make the old managed session storage unreachable through SCH, but do not
guarantee its physical deletion ahead of the AgentCore retention. The outcome SHALL
distinguish the complete purge of the SCH-controlled data from the residual retention of
the AWS managed session storage.

## Behavior

- `sch delete my-workspace` followed by typing exactly `my-workspace` → irreversible
  deletion starts; any other input or refusal → exit code 1, nothing stopped or deleted.
- `sch delete my-workspace --yes` (automation) → proceeds without reading stdin, same
  checks and fail-closed semantics.
- `sch delete --all` → scope and target list shown; typing `DELETE ALL WORKSPACES` starts
  the deletion of all workspaces in the displayed scope. Empty scope → "no targets",
  exit code 0, no prompt.
- Partial bulk failure → all targets attempted, successful ones finalized, failed ones
  kept for retry, exit code 1; a retry resumes only the remaining targets.
- Active microVM → runtime stopped (without a new checkpoint), purge only after the stop
  succeeded; AgentCore refusing/failing the stop → error, retry state preserved,
  checkpoints intact.
- Workspace with current versions, historical versions, and delete markers in the managed
  prefixes → all purged and final verification finds nothing belonging to the workspace.
- A workspace created after the bulk snapshot → not deleted by that invocation; a
  subsequent `sch delete --all` is required.
- Deletion completed → registry record and managed local artifacts removed; `sch list`
  no longer shows the workspace. A workspace bound via `--sync` → binding and baseline
  removed, the user's directory, files, and Git history remain intact.

## Invariants

**I1.** Deletion is irreversible and never runs without either the exact interactive
confirmation or `--yes`; a mismatched confirmation produces no mutation of any kind.

**I2.** No purge begins before the current runtime session is stopped or proven absent.

**I3.** The registry record and local state are deleted only after a verified remote
purge; ordering is never inverted.

**I4.** Data not owned by SCH (sync source directories, external ACP mirrors, local
repositories, Git history) is never deleted.

**I5.** The purge is considered successful only when no object version or delete marker
remains in the three S3 scopes.

**I6.** The complete operation, bulk or single, and every retry are idempotent; retries
skip already-deleted elements.

**I7.** A deleted workspace's name can be reused only for a fresh workspace with a new
session identity and no adopted state.

## Cross-references

- What the purged prefixes contain and lifecycle-based retention: [workspace-checkpointing](workspace-checkpointing.md).
- Survival guarantees that deletion terminates: [workspace-persistence](workspace-persistence.md).
- Rationalized from openspec/specs/workspace-deletion (git tag `pre-openspec-retirement`).
- Registry interaction: [iam-workspace-registry](../security/iam-workspace-registry.md).
- [MANIFESTO](../../../MANIFESTO.md).
- Code: `cli/sch/commands/delete.py` (CLI), `infra/workspace_registry_handler.py` (registry records), end-to-end check in `bin/verify-workspace-deletion.sh`.
