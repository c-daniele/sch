# Session handoff

> Domain: [Workspace lifecycle](../README.md) · Status: Implemented · Source: rationalized from openspec specs (2026-08-28)

## Purpose

Transfer a local OpenCode session into a remote SCH workspace, preserving its context and
making it immediately resumable through the normal remote OpenCode paths (`sch task
--continue`, interactive sessions).

## Scope

In scope:
- The `sch handoff` command: export, transfer via the bundle channel, import via the shim, output.
- Automatic selection of the local session and opt-in redaction.
- Implicit harness binding to `opencode` on never-used workspaces; rejection on other bindings.
- Re-push semantics (last-write-wins), automatic resumption of the imported session.
- Orthogonality with repo alignment, and diagnostics for version skew and older images.

Out of scope:
- Repo seeding and synchronization options (`--branch`, `--sync`) of other commands — see [local-workspace-sync](../sync-and-git/local-workspace-sync.md).
- Headless task semantics (`sch task`) beyond `--continue` resumption — see [headless-task-execution](../access-surfaces/headless-task-execution.md).
- Harness binding rules — see [harness-selection](../platform/harness-selection.md).
- Durability of the imported session state — see [workspace-checkpointing](workspace-checkpointing.md).

## Requirements

### The `sch handoff` command

**R1.** `sch` SHALL expose `sch handoff <workspace> [--harness <opencode|claude|pi>] [--session <id>] [--sanitize] [--storage <s3|session>]` that
exports an OpenCode session from the laptop (via `opencode session export --standalone`;
the `session list --standalone --format json` and `session export` subcommands of
OpenCode 2 — `--standalone` keeps the laptop-side calls out of OpenCode's per-user
background service), transfers it into
the remote workspace's staging area through the existing bundle channel, and requests its
import from the shim via the `session-import` action. On success the command SHALL print
the imported sessionID and exit with code 0. The `--harness` flag is accepted only for
invocation-shape consistency with the other workspace commands: `opencode` (or omitting
the flag) proceeds, any other value is rejected per R6.

**R2.** The local `opencode` binary is a prerequisite of this command only: if it is
absent from the `PATH`, the command MUST fail with a readable error before any local or
remote mutation (including before invoking the remote runtime).

**R3.** The command MUST NOT accept the mirror-sync options or `--branch`: the handoff is
orthogonal to the repo contents.

### Automatic selection of the local session

**R4.** Without `--session`, the command SHALL non-interactively resolve the most recent
OpenCode session belonging to the current directory's project (via
`opencode session list --format json`) and SHALL print the title and id of the selected
session before the export. With `--session <id>` the automatic resolution MUST be
bypassed. If no session for the current project exists, the command MUST fail with an
error suggesting `--session`.

### Redaction

**R5.** The export SHALL be faithful by default (no redaction). With `--sanitize` the
command SHALL delegate redaction to `opencode session export --sanitize`, so that the
redacted content is what gets transferred.

### Harness binding

**R6.** On a never-used workspace (no harness binding), `sch handoff` SHALL provision the
microVM (warmup like the other commands) and SHALL persist the harness binding to
`opencode`, following the resolution and immutability rules of the `harness-selection`
capability; an explicit congruent `--harness opencode` behaves exactly like omitting the
flag. On a workspace already bound to a harness other than `opencode` (`claude` or
`pi`), the command MUST fail with an error naming the current binding and clarifying that
the handoff supports only the opencode harness, without remote mutations. An explicit
`--harness claude|pi` on the command line MUST fail the same way (naming the requested
value instead of a persisted binding), before any local or remote mutation.

### Re-push with last-write-wins semantics

**R7.** A repeated handoff of the same session SHALL be allowed without additional flags
and SHALL update the remote session to the freshly exported content (last-write-wins).
When the remote reports that the session already existed (`reimported`), the command
SHALL print a warning that any remote work done in the meantime on that session may have
been overwritten.

### Automatic resumption of the imported session

**R8.** After a successful handoff, the imported session SHALL be the most recent
OpenCode session of the remote workspace, so that `sch task <workspace> --continue` and
the interactive paths resume it without additional flags or configuration.

**R9.** If the imported session references a provider not configured in the runtime, the
headless resumption SHALL preserve the context but use the runtime's default OpenCode
model; a model explicitly requested by the operator SHALL keep precedence.

### Orthogonality with repo alignment

**R10.** The handoff MUST NOT require the remote worktree to be seeded or synchronized.
When the remote reports an empty or uninitialized worktree, the command SHALL print a
warning that the session may reference files not present on the remote (suggesting
`--branch` or `--sync` on the other commands) and SHALL still complete successfully.

### Diagnostics for version skew and older images

**R11.** The command MUST NOT apply preventive gates on the OpenCode version. When the
remote import fails, the error SHALL report the local and remote OpenCode versions. When
the runtime does not recognize the `session-import` action, the command MUST fail with a
message indicating the remedy (rebuild/update the runtime image).

### Combined submission via `sch task --handoff`

**R12.** `sch task <workspace> --handoff` SHALL provide the same transfer semantics as
`sch handoff` (export, bundle upload, `session-import`, warnings, diagnostics) followed
by a headless task submission with `continue=true`. The full contract (flag gating,
opencode-only rejection, seed-then-handoff ordering, stdout purity) lives in
[headless-task-execution](../access-surfaces/headless-task-execution.md) R23–R25; this
spec owns the transfer mechanics reused through `export_local_session` and
`upload_and_import`.

## Behavior

- `sch handoff myws --session ses_abc` with a valid local session on a workspace bound to
  opencode → export, upload to staging, `session-import`, `ses_abc` printed as the
  imported sessionID, exit code 0.
- `sch handoff myws` from a project with sessions → most recent session of the project is
  selected, its title and id printed before proceeding.
- No sessions in the current project → error suggesting `--session <id>`; no `opencode`
  on the laptop → readable prerequisite error, remote runtime untouched.
- `sch handoff brand-new-ws` on a never-used workspace → microVM provisioned, workspace
  bound to `opencode`, handoff proceeds. Workspace bound to `claude` or `pi` → failure
  naming the binding and the opencode-only constraint, without remote mutations.
- Re-push after continuing the session locally → success, remote session reflects the new
  content, output contains the possible-overwrite warning.
- `sch handoff myws` followed by `sch task myws --continue "<prompt>"` → the headless task
  resumes the just-imported session and the local conversational context; with a
  provider not configured in the runtime and no explicit model, the task resumes the same
  context on the runtime's default OpenCode model.
- `sch task myws --handoff --branch feat/x "go"` → same transfer as `sch handoff myws`
  (seed completes first), then the task is submitted with `continue=true`; stdout carries
  only the `task_id`.
- Empty remote worktree → success with the unaligned-worktree warning.
- Import failure with divergent versions → error includes both versions; image without
  `session-import` → failure instructing to update the runtime image.

## Invariants

**I1.** No local or remote mutation occurs if any prerequisite fails (missing `opencode`
binary, non-opencode harness binding, unknown `session-import` action).

**I2.** The handoff never mutates repo contents: it accepts no sync/branch options and
succeeds on an empty worktree.

**I3.** Re-pushing a session always makes the remote session reflect the most recent
local export (last-write-wins).

**I4.** After a successful handoff, the imported session is the most recent OpenCode
session of the remote workspace and is resumable without extra flags.

**I5.** The handoff never persists a harness binding other than `opencode`, and never
overrides an existing non-opencode binding (it fails instead).

## Cross-references

- Durability of the imported session state: [workspace-checkpointing](workspace-checkpointing.md), [workspace-persistence](workspace-persistence.md).
- Removal of workspaces holding imported sessions: [workspace-deletion](workspace-deletion.md).
- Rationalized from openspec/specs/session-handoff (git tag `pre-openspec-retirement`).
- Harness binding rules: [harness-selection](../platform/harness-selection.md); headless resumption: [headless-task-execution](../access-surfaces/headless-task-execution.md).
- [MANIFESTO](../../../MANIFESTO.md).
- Code: `cli/sch/commands/handoff.py` (CLI, `export_local_session`/`upload_and_import` shared with `sch task --handoff`), `image/scripts/harness-wrapper.sh` (shim `session-import` action), end-to-end check in `bin/verify-handoff.sh`.
