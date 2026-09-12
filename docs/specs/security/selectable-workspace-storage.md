# Selectable Workspace Storage

> Domain: [Security](../README.md) · Status: Implemented · Source: rationalized from openspec specs (2026-08-28)

## Purpose

Specify how the storage backend (`s3` or `session`) is selected per workspace, persisted, propagated end-to-end, and kept immutable, together with the monotonic `sessionEpoch` that fences stale writers.

## Scope

In scope:

- `--storage` selection at workspace creation and its persistence in the local index or central registry
- Operational root per backend inside the microVM
- Backend and epoch propagation and validation across CLI, runtime payload, markers, manifest, registry, and diagnostics
- Epoch-based rejection of stale writers across session rotation

Out of scope:

- Registry API mechanics (see [iam-workspace-control-api](iam-workspace-control-api.md))
- Identity-scoped checkpoint prefixes (see [owner-scoped-workspace-storage](owner-scoped-workspace-storage.md))

## Requirements

### Immutable backend selection

- **R1.** SCH SHALL accept `--storage s3|session` when a workspace is created, persist the choice in the local index or central registry, and reuse it automatically on subsequent invocations. In the absence of the flag, a new workspace SHALL use `s3`. A legacy workspace record missing the field SHALL be reconciled to `session` without changing the session ID (or harness or existing data). A subsequent request with a divergent backend MUST be rejected without mutating the workspace: SCH stops before provisioning and indicates that an explicit migration is required.

### Backend-dependent operational root

- **R2.** The runtime SHALL use `/home/sch/workspace` as the operational root for the `s3` backend and `/mnt/workspace` for the `session` backend. All consumers of the worktree and of the harness state MUST use the resolved root: bootstrap, sync worker, task cwd, remote server, shell autostart, config and checkpoint. An `s3` workspace's local-to-remote bootstrap sync SHALL apply files under `/home/sch/workspace/repo` and never under `/mnt/workspace/repo`; a `session` workspace continues to read and write under `/mnt/workspace`.

### End-to-end propagation and epoch fencing

- **R3.** CLI, runtime payload, markers, manifest, registry and diagnostics SHALL carry the storage backend and a monotonic per-workspace `sessionEpoch`. The runtime MUST reject a payload that diverges from the backend already selected in the microVM or from an already-active epoch, without changing the root or starting a workload. Rotating the runtime session ID SHALL increment the epoch while preserving backend and harness; a microVM with a previous epoch attempting to publish after rotation SHALL be rejected by the S3 claim and the conditional writes.

## Behavior

- `sch task my-workspace` (no `--storage`) persists `storage=s3`; the microVM uses its local workdir, with durability via S3 checkpoints.
- `sch task my-workspace --storage session` persists `storage=session`; the workspace uses the managed `/mnt/workspace` mount.
- Opening an `s3` workspace with `--storage session` fails before provisioning with an explicit migration-required message; the workspace is unchanged.
- Live workspace information (`sch info`/`sch status`) includes the backend and the effective operational root.
- `sch reset-session` / runtime-version rotation: new session ID, `sessionEpoch + 1`, backend and harness preserved; the old session's L1 state is no longer addressed (for `session` it is not deleted until AgentCore retention expires).

## Invariants

- **I1.** A workspace's storage backend is immutable after creation; only an explicit migration can change it, and no command performs one implicitly.
- **I2.** The operational root is a pure function of the backend, and every worktree/harness-state consumer derives it from that backend, never from a hardcoded path.
- **I3.** `sessionEpoch` never decreases for a given workspace; at most one epoch is active for writes at any time.
- **I4.** A registry record's storage and epoch, once present, are immutable through resolution and are reconciled (`session`, epoch `1`) only when absent.

## Cross-references

- [iam-workspace-control-api](iam-workspace-control-api.md) — resolve/rotate guards that enforce R1 and R3 server-side
- [iam-workspace-registry](iam-workspace-registry.md) — registry-backed resolution that carries backend and epoch to the CLI
- [owner-scoped-workspace-storage](owner-scoped-workspace-storage.md) — identity-scoped S3 prefixes the `s3` backend writes to
- [workspace-registry](workspace-registry.md) — registry record fields and legacy reconciliation
- [MANIFESTO](../../../MANIFESTO.md)
- Code: `cli/sch/harness.py` (`resolve_harness` validation and rotation), `cli/sch/workspace.py` (`validate_storage_state`, `validate_epoch_state`), `infra/workspace_registry_handler.py` (`_reconcile_storage`, `_rotate`), `infra/agent_runtime.yaml` (`DEFAULT_STORAGE: s3`)
