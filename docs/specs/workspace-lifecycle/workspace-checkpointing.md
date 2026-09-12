# Workspace checkpointing

> Domain: [Workspace lifecycle](../README.md) · Status: Implemented · Source: rationalized from openspec specs (2026-08-28)

## Purpose

L2 durability of the workspace through periodic and forced synchronous checkpointing to
S3, automatic restore onto the operational root, checkpoint identity tied to the
workspace (not the `runtimeSessionId`), retention via bucket versioning, and writer
fencing. Also defines the headless-task state as an S3 object sibling of the checkpoint,
readable offline by `sch status`, and orphan reconciliation at shim restart.

## Scope

In scope:
- Periodic and forced (synchronous) checkpoint content, ordering, and upload semantics.
- Automatic restore from S3 on empty session storage, and restore-always-yields semantics.
- Checkpoint identity, key layout, manifest fields, and harness propagation.
- Retention via bucket versioning; fenced publication across microVMs.
- Headless-task state sibling object, `sch status` offline reads, and orphan reconciliation.
- Verification of the full checkpoint → loss → restore cycle.

Out of scope:
- The durability contract (loss window, zero-loss on explicit stop) and backend-level survival guarantees — see [workspace-persistence](workspace-persistence.md).
- Deletion and purge of checkpoint objects — see [workspace-deletion](workspace-deletion.md).
- Headless task execution semantics beyond the persisted state — see [headless-task-execution](../access-surfaces/headless-task-execution.md).
- Harness binding rules — see [harness-selection](../platform/harness-selection.md).

## Requirements

### Periodic checkpoint of the full state

**R1.** For workspaces with backend `s3`, the shim SHALL perform a periodic checkpoint
(default 60 seconds, configurable) directly from the microVM's local root to S3,
including the worktree and Git metadata, OpenCode state with a consistent SQLite backup,
configuration and, for Claude, the JSONL transcripts. Modified objects SHALL be uploaded
under generation-scoped keys and the authoritative manifest SHALL be published last, only
after successful uploads; unchanged artifacts MAY reuse references from the previous
generation. For backend `session`, the shim SHALL keep the existing compatible checkpoint
behavior. If nothing has changed, the cycle MUST NOT upload new artifacts.

**R2.** The checkpoint archive SHALL include uncommitted changes and untracked files in
full. The OpenCode database copy SHALL be a consistent snapshot (produced by the SQLite
backup API) even when taken while OpenCode is writing; it is consistent regardless of
the state of the other artifacts.

**R3.** Harness state replicas SHALL be per-harness: the `/mnt/workspace/state/claude`
replica (Claude JSONL) SHALL be included, if modified, with read-after-write
verification, only on `harness=claude` workspaces; the Pi state replica
(`/mnt/workspace/state/pi`: JSONL sessions, settings, trust, seeded extensions) SHALL be
included as a dedicated manifest artifact only on `harness=pi` workspaces. Each replica
SHALL be absent on the other harnesses' workspaces.

### Synchronous forced checkpoints

**R4.** The shim's `checkpoint` action SHALL execute the whole checkpoint sequence
synchronously and forcibly (db → mount, artifacts → S3, manifest updated even if change
detection finds no differences), and `sch stop` SHALL invoke it before
`StopRuntimeSession`.

**R5.** The completion of a headless task (terminal states
`succeeded`/`failed`/`timed-out`/`interrupted`) SHALL be a forced synchronous checkpoint
trigger, semantically on par with `sch stop`: no terminal state MAY produce unattended
data. The forced checkpoint SHALL be executed before invoking `complete_async_task` and
before returning `Healthy`. Partial changes of a failed or timed-out run SHALL be
persisted too.

**R6.** A checkpoint failure MUST NOT block the stop nor the task completion: it SHALL be
reported as an explicit warning on `sch stop` and recorded in the task's persisted state
(`checkpoint_status=failed`). The task SHALL still be completed via
`complete_async_task` (ping → `Healthy`); the session MUST NOT be left in `HealthyBusy`.

### Automatic restore

**R7.** At the startup of an `s3` workspace, the shim SHALL download the authoritative
manifest and restore worktree, Git metadata, state, config, and harness history into
staging on the same filesystem, validate every required artifact, and promote the result
before readiness. For `session`, restore SHALL keep the current L1 precedence and operate
only when the session storage is empty, using staging inside the mount.

**R8.** Restore SHALL be fail-closed: unreadable or malformed manifests, partial
restores, or divergent backends MUST result in no readiness and no checkpoint loop, with
explicit diagnostics exposed. A transient S3 error while reading an existing manifest
MUST NOT be interpreted as an absent checkpoint and MUST NOT cause initialization or
publication of an empty workspace.

**R9.** Restore MUST NOT overwrite an active workdir: if the same microVM already
contains the populated local root, no restore is performed. Pre-existing content on the
operational root (detected at startup classification) SHALL proceed with the unchanged
resume flow. An `s3` workspace starting without an existing checkpoint SHALL proceed with
a fresh seed of the selected harness, without errors.

**R10.** Restore of a harness state replica (claude, pi) SHALL complete before the
readiness marker, so session history is resumable at the first harness startup. If a
user already has an interactive shell open during the restore, the wrapper dispatcher
SHALL wait for the readiness marker of the selected harness (within the configured wait
window) and start only after the restore is verified.

### Checkpoint identity

**R11.** The S3 key of the checkpoints SHALL be derived from the workspace name
(`checkpoints/<workspace>/`), stable across `runtimeSessionId` rotation. The manifest
SHALL include the `harness` and `storage` fields, valued from the workspace marker, so
that restarts with an empty operational root restore the persisted harness and backend.

**R12.** `sch` SHALL propagate workspace name, harness, storage, and `sessionEpoch` in
the invocation payloads (warm-up `noop`, `task`, `mark-interactive`, `checkpoint`); the
shim SHALL persist them in the operational root's marker and MAY derive the workspace
name from the sessionId prefix (`sch-<workspace>-<uuid>`) as a fallback. In the absence
of a workspace name, the L2 restore MUST NOT be attempted and the absence SHALL be
recorded in the boot state; the Phase 0 flow (fresh seed of the image's default harness)
SHALL proceed.

### Retention and fencing

**R13.** Checkpoints SHALL use per-workspace manifests and generation-scoped artifacts
with bucket versioning enabled. Non-current generations SHALL expire automatically
through a lifecycle policy with configurable retention (default 30 days). The runtime
MUST NOT perform application-level deletions of checkpoint objects.

**R14.** Manifest and task status SHALL be attributed to the current writer via
`sessionEpoch` and token, and updated with conditional S3 writes based on ETag. A writer
with an earlier epoch MUST NOT be able to overwrite data published by a later epoch.

### Headless-task state as a checkpoint sibling

**R15.** The state/outcome of a headless task SHALL be persisted in a distinct S3 object
next to the workspace's checkpoint under the same key prefix
(`checkpoints/<workspace>/`), independent of the checkpoint manifest for both writes and
reads. It SHALL be written by the shim at submission (`state=running`), roughly every 30s
(heartbeat, cheap JSON PUT), and at every terminal state. The write SHALL be
overwrite-only (no read-modify-write) and MUST NOT touch the checkpoint manifest, which
remains written exclusively by the checkpoint cycle.

**R16.** The sibling object format SHALL include `harness` (the harness used for the
task) in addition to `task_id`, `state`, `exit_code`, `prompt`, `started_utc`,
`finished_utc`, `duration_s`, `heartbeat_utc`, `harness_session_id`, `image_version`,
`error`, `checkpoint_status`.

**R17.** `sch status <workspace>` SHALL read the sibling object using the operator's
local AWS credentials, without invoking the microVM and without depending on the
checkpoint manifest. No new AWS permissions SHALL be required beyond those already used
by `sch deploy` and checkpoint fetching. A workspace that never executed headless tasks
SHALL report the canonical `state=none`, distinguishable from "missing object" and "task
in progress". If the forced checkpoint on task completion failed, the sibling object
SHALL still be persisted with the terminal state and `checkpoint_status=failed` plus a
warning, so `sch status` distinguishes the harness outcome from checkpoint durability.

### Orphan reconciliation

**R18.** At startup (`_bootstrap`), if the shim detects a sibling object with
`state=running` for its own workspace, it MUST NOT attempt to resurrect the dead task: it
SHALL rewrite it as `state=interrupted, outcome=unknown`, consult the object's `harness`
field to record which harness was in progress (for reconstructing
`harness_session_id`), and proceed to the idle state returning `Healthy` (no phantom
`HealthyBusy`). `interrupted` SHALL be a distinct terminal state, distinguishable from
`failed` and `timed-out`.

**R19.** After reconciliation, submission of a new task MUST be accepted freely (no guard
based on the persisted state), only for the harness persisted in the marker.

### Verification of the checkpoint → loss → restore cycle

**R20.** The repository SHALL provide a reproducible verification procedure for the full
cycle: creation of known state (including uncommitted changes) → forced checkpoint →
simulation of L1 loss via sessionId rotation (`sch reset-session <workspace>`, with
confirmation; the local mapping points to a new compliant sessionId ≥ 33 characters and
the previous one is no longer used) → reopening → assertion that the worktree, the
OpenCode sessions, and the config have been restored. Each check SHALL report an explicit
pass/fail outcome.

## Behavior

- Checkpoint fires with worktree changes (even without a commit) → the repo archive on S3
  is updated and the manifest reports the new timestamp; for the `s3` backend the
  generation-scoped archive is uploaded directly without staging on `/mnt/workspace`.
- An artifact upload of the new generation fails → the manifest is not updated and
  restore keeps using the previous generation.
- Idle workspace → no uploads.
- `sch stop` → synchronous checkpoint completes before microVM shutdown; manifest
  reports the stop timestamp. Checkpoint failure → explicit warning, stop proceeds with
  `StopRuntimeSession`.
- Headless task ends (`succeeded`, `failed`, `timed-out`) → forced checkpoint before
  `complete_async_task`; manifest reports the end-of-task timestamp; `/ping` returns
  `Healthy` only afterwards. Checkpoint failure → warning in the task's persisted state,
  task still completed.
- Restart with empty storage and an existing checkpoint → worktree, OpenCode state,
  config, and claude/pi JSONL state restored; sessions reappear with history at the next
  TUI startup; the manifest's `harness` drives seed, restore, and dispatcher.
- `sch status myws` with the microVM stopped → the operator sees
  `state`/`exit_code`/`finished_utc`/`harness` of the last task without waking the
  microVM; after a restart of a microVM that died mid-run, the state shows
  `interrupted` with the orphaned task's harness, and a new `sch task` submission for the
  persisted harness is accepted normally.
- Concurrent manifest update from two microVMs → only the writer with the more recent
  epoch completes the conditional write.

## Invariants

**I1.** The authoritative manifest is only ever published after all artifacts of the new
generation have been uploaded successfully; restore never observes a half-published
generation.

**I2.** A checkpoint cycle on an unchanged workspace performs zero uploads to S3.

**I3.** Every headless-task terminal state is preceded by a forced synchronous
checkpoint attempt; no terminal state leaves unattempted data.

**I4.** A failed checkpoint never blocks `sch stop`, never blocks task completion, and
never leaves the session in `HealthyBusy`.

**I5.** The L2 restore is attempted only when the operational root has no pre-existing
content, only with a known workspace name, and only from a verified manifest; any failure
is fail-closed (no readiness).

**I6.** The checkpoint key prefix is a pure function of the workspace name and remains
stable across `runtimeSessionId` rotations.

**I7.** The runtime never performs application-level deletions of checkpoint objects;
expiry is solely the bucket lifecycle policy's job.

**I8.** A writer with an earlier `sessionEpoch` can never overwrite data published by a
later epoch.

**I9.** The task-state sibling object is written only by the shim's task lifecycle
(submission/heartbeat/terminal) and never modifies the checkpoint manifest.

**I10.** `state=interrupted` is reachable only via orphan reconciliation (microVM died
mid-run), never via the normal task execution path.

## Cross-references

- Durability contract and loss window: [workspace-persistence](workspace-persistence.md).
- Purge of the checkpoint prefixes at deletion: [workspace-deletion](workspace-deletion.md).
- Rationalized from openspec/specs/workspace-checkpointing (git tag `pre-openspec-retirement`).
- Headless task semantics: [headless-task-execution](../access-surfaces/headless-task-execution.md); harness binding: [harness-selection](../platform/harness-selection.md).
- [MANIFESTO](../../../MANIFESTO.md).
- Code: `image/scripts/harness-wrapper.sh` (shim checkpoint/restore actions), `image/scripts/init-workspace.sh` (seed/restore bootstrap), CLI in `cli/sch`; end-to-end check in `bin/verify-l2.sh`.
