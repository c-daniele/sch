# Workspace persistence

> Domain: [Workspace lifecycle](../README.md) · Status: Implemented · Source: rationalized from openspec specs (2026-08-28)

## Purpose

Guarantee survival of workspaces and harness state across stop/resume cycles, whether the
backend is `session` (managed session storage) or `s3` (S3 checkpoints). Defines the
durability contract (loss window), the end-to-end verification procedure, and the
operator-facing documentation of session storage limits.

## Scope

In scope:
- Worktree and Git metadata persistence across stop/resume, idle timeout, and microVM recreation.
- Persistence and recovery of harness state (OpenCode database, Claude Code JSONL transcripts, user config).
- Durability semantics for total session storage loss (L2 layer): loss window and harness restoration.
- A reproducible, parameterizable end-to-end verification procedure.
- Operator documentation of session storage (Preview) limits with mitigation status.

Out of scope:
- Checkpoint mechanics, artifact format, manifest publication, retention, and writer fencing — see [workspace-checkpointing](workspace-checkpointing.md).
- Deletion of workspaces or checkpoint data — see [workspace-deletion](workspace-deletion.md).
- Selection or switching of storage backends — see [selectable-workspace-storage](../security/selectable-workspace-storage.md).
- Harness binding resolution and immutability — see [harness-selection](../platform/harness-selection.md).

## Requirements

### Durability across cycles

**R1.** The worktree SHALL survive stop/resume cycles according to the persisted backend.
For `session`, `/mnt/workspace/repo` SHALL be preserved by the managed session storage.
For `s3`, `/home/sch/workspace/repo` SHALL be rebuilt from the last published S3 manifest
when the microVM is recreated; if the microVM is still alive, it SHALL be reused directly.

**R2.** Harness state SHALL be checkpointed and restored consistently with the backend,
keeping project paths stable for that backend. The OpenCode database (sessions, messages,
visible tool calls) and the Claude JSONL transcript SHALL be preserved. At restart, the
history MUST be available to the harness before readiness, without promising
chain-of-thought not recorded by the harness.

**R3.** For backend `s3`, the workspace (worktree, harness state, user config) SHALL
survive total loss of session storage or recreation of the microVM through the L2
durability layer (S3 checkpoints). The maximum loss window SHALL be the checkpoint
interval (default ~60 seconds of work) for scenarios not gracefully stopped; for explicit
stops (`sch stop`) the loss SHALL be zero (synchronous final checkpoint). The `session`
backend keeps its own managed session storage durability semantics.

**R4.** The L2 restore SHALL restore the harness persisted in the checkpoint manifest, so
that a recreated microVM starts the correct harness without re-specification. For legacy
manifests without a `harness` field, the restore SHALL apply `upgrade_reconcile`
treating the restore as harness=opencode, and SHALL proceed without loss.

### Verification

**R5.** The repository SHALL provide a reproducible verification procedure (script or
documented runbook) that validates end-to-end persistence for both harnesses: creation of
known state → explicit stop → resume → assertion of the presence of the worktree, the
harness state, and the harness sessions. The procedure SHALL be parameterizable by
harness (`--harness opencode|claude`) and SHOULD cover both paths in a single
end-to-end run. Each check SHALL report an explicit pass/fail outcome naming the check
and the harness.

### Limits documentation

**R6.** The repository documentation SHALL keep up to date the list of observed limits of
the session storage (Preview) and of the L2 durability for both harnesses, each with its
current mitigation status:
- expiry after 14 days of inactivity (mitigated by L2 restore for both harnesses);
- storage reset at a runtime version update (mitigated by L2 restore);
- at most 10 shells per runtime;
- empirically observed size/quota limits;
- the `fcntl`/async-restore hazard, applied to both harnesses;
- the cumulative growth of Claude Code JSONL transcripts as a distinct budget factor
  (mitigated by the same application-timeout mechanism, `SCH_TASK_TIMEOUT_S`).

**R7.** The documentation MUST NOT indicate a manual pre-deploy backup as necessary,
except for the last deploy that introduces the L2 mechanism itself (including the deploy
that introduces the Claude state replica).

## Behavior

- `session` backend, explicit stop: user modifies files in `/mnt/workspace/repo`, runs
  `sch stop <workspace>`, reopens a shell on the same session → modified files, Git
  state, and uncommitted changes are present and identical to before the stop.
- `session` backend, idle timeout: the microVM is stopped by the idle timeout; reopening
  a shell finds the worktree present and intact as before the stop.
- `s3` backend resume: reopening an `s3` workspace after the microVM has terminated
  restores worktree, Git metadata, and uncommitted changes from the last checkpoint.
- Harness resume (both backends): relaunching `opencode` (or `claude --resume
  <session-id>`) from the operational root finds the previous session with full history;
  harness config modified in the operational root (opencode) or `~/.claude` mirror
  (claude) is still present and used by the harness TUI.
- Edge cases: a missing file, session, or config at resume makes the verification
  procedure fail, indicating which check and which harness did not pass. A workspace
  recreated after session storage loss starts the harness persisted in the manifest;
  a deploy with a new image version restores state identical to the moment of the stop.

## Invariants

**I1.** After an explicit `sch stop` and subsequent resume on the same backend, the
worktree, Git state, harness sessions, and config are identical to the pre-stop state
(zero loss).

**I2.** For non-graceful termination of an `s3` workspace, data loss SHALL NOT exceed
one checkpoint interval (default ~60 seconds).

**I3.** A restored workspace always starts the harness recorded in the checkpoint
manifest (or opencode for legacy manifests without the field), never the image default.

**I4.** Harness history is available to the harness before the workspace is declared
ready.

## Cross-references

- Checkpoint timing, artifact coverage, restore mechanics, and fencing: [workspace-checkpointing](workspace-checkpointing.md).
- Removal of checkpoints and workspace state: [workspace-deletion](workspace-deletion.md).
- Rationalized from openspec/specs/workspace-persistence (git tag `pre-openspec-retirement`).
- Storage backends: [selectable-workspace-storage](../security/selectable-workspace-storage.md); harness rules: [harness-selection](../platform/harness-selection.md).
- [MANIFESTO](../../../MANIFESTO.md).
- Code: workspace seed and restore orchestration in `image/scripts/init-workspace.sh`; CLI in `cli/sch`; end-to-end check in `bin/verify-persistence.sh`.
