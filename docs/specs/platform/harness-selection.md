# Harness selection

> Domain: [Platform](../README.md) · Status: Implemented · Source: rationalized from openspec specs (2026-08-28)

## Purpose

Persisted per-workspace selection of the coding agent harness (`opencode`, `claude`, or `pi`), chosen via a `--harness` flag on the `sch` wrapper and propagated on all interactive (`sch shell`/`sch open`/`sch run`) and headless (`sch task`) paths, so that L2 restore, orphan reconciliation, and microVM restarts automatically restore the right harness without re-specifying it.

## Scope

In scope:
- The `--harness` flag, persistence in the workspace's local index file, rejection of divergent selections, upgrade reconcile of legacy workspaces, propagation to the shim payload and the mount marker, mutual exclusivity per workspace, consistent default between client and central registry.

Out of scope:
- How the selected harness is dispatched inside the microVM (ENV bridge, readiness markers: [runtime-image](runtime-image.md)).
- The documented reset procedure for changing harness on an existing workspace.
- Raw `agentcore exec` invocations that bypass the `sch` wrapper (outside the multi-harness contract).

## Requirements

### Selection and persistence

**R1.** The `sch` wrapper SHALL expose a `--harness <opencode|claude|pi>` flag on the `shell`, `open`, `run`, and `task` commands. On first selection for a workspace the value SHALL be persisted alongside the `runtimeSessionId` in the workspace's local index file (`~/.config/sch/workspaces/<ws>`); subsequent invocations SHALL reuse the persisted value without requiring `--harness`.

**R2.** Omitting `--harness` on a workspace with no persisted value SHALL select the default `opencode`.

**R3.** The wrapper SHALL reject with an explicit error (naming both the persisted and the requested value) any invocation passing `--harness <x>` where `<x>` differs from the persisted value. The rejection MUST be non-destructive: no local or remote state is modified and the runtime session is not touched. A congruent redundant flag (matches the persisted value) SHALL be accepted.

**R4.** The local index file SHALL contain both the `runtimeSessionId` (existing contract) and the `harness` field.

### Upgrade reconcile of legacy workspaces

**R5.** Workspaces whose local index predates the `harness` field SHALL be reconciled to `harness=opencode` on first access after deploying the new version of `sch`. The reconciliation SHALL be an additive write of the `harness=opencode` field to the existing index file, without touching the `runtimeSessionId`.

**R6.** The reconciliation SHALL NOT be applied when the operator explicitly passes `--harness claude` on that first invocation: the explicit value is persisted instead (explicit upgrade handoff).

### Propagation

**R7.** `sch` SHALL propagate the `harness` value in the payload of shim-bound invocations (`noop` warm-up, `task`, `mark-interactive`, `checkpoint`), and the shim SHALL persist it in the workspace identity marker on the mount.

**R8.** A restarted microVM (idle timeout, `MaxLifetime`, version update) MUST find the persisted harness and apply it without operator re-specification: the `init-workspace.sh` seed, the L2 restore, and the wrapper dispatcher all SHALL use the persisted harness, never the image default.

**R9.** A session opened without the `sch` wrapper (e.g. raw `agentcore exec`) SHALL NOT trigger the default-harness logic: this capability is opt-in via the wrapper and the raw path remains outside the multi-harness contract.

### Mutual exclusivity

**R10.** A workspace is exactly one of `opencode`, `claude`, and `pi`. The shim SHALL reject any `task` action whose `harness` differs from the one persisted in the workspace marker on the mount, returning an explicit error and starting no thread of any harness.

**R11.** An interactive (TUI) invocation on a harness different from the persisted one SHALL be rejected client-side by the `sch` wrapper. No path in the system SHALL start two harnesses on the same session.

**R12.** Orphan reconciliation (see `workspace-checkpointing`) SHALL use the persisted harness to reconstruct the correct argv of the interrupted task when computing the `interrupted` state: after a restart with `state=running`, the task is marked `interrupted` and a subsequent submission is accepted only for the persisted harness.

### Consistent default between client and registry

**R13.** The default harness applied to new workspaces SHALL be identical in every component that materializes one: the `sch` wrapper (client) and the optional central registry MUST use the same default value, so the resulting harness of a new workspace does not depend on which component completed registration first.

## Behavior

- `sch shell brand-new-ws` (no flag): persists `harness=opencode`, opens the OpenCode TUI.
- `sch shell myws --harness claude` where `myws` has `harness=opencode` persisted: explicit error, no shell opened, no state modified.
- `sch shell myws --harness opencode` where `harness=opencode` is persisted: accepted (redundant flag).
- `sch shell oldws` on a legacy index without `harness`: additive write of `harness=opencode`, `runtimeSessionId` untouched.
- `sch shell oldws --harness claude` on a legacy index: `harness=claude` persisted (explicit handoff), Claude Code TUI opened.
- `sch task` with a divergent harness: shim returns an explicit error and starts no thread.

## Invariants

- **I1.** At any time, a workspace has at most one persisted harness value, and every running harness on that workspace's session equals it.
- **I2.** A harness-changing invocation never mutates state (local index, registry, runtime session, mount marker).
- **I3.** After any microVM restart, the active harness on a workspace is the persisted one (never the image default).
- **I4.** Client default and registry default are the same value (`opencode`).

## Cross-references

- [runtime-image](runtime-image.md) — dispatcher and harness-specific seeding driven by the persisted value.
- [workspace-checkpointing](../workspace-lifecycle/workspace-checkpointing.md) — L2 restore and orphan reconciliation using the persisted harness. Headless task lifecycle (built on the shim `task` action) lives in the `headless-task-execution` capability.
- [MANIFESTO](../../../MANIFESTO.md)
- Code: `cli/sch`, `image/app/main.py` (shim marker and `task`/`mark-interactive` actions), `image/scripts/init-workspace.sh`, `bin/verify-multi-harness.sh`
