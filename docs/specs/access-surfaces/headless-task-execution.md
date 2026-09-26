# Headless task execution

> Domain: [Access surfaces](../README.md) · Status: Implemented · Source: rationalized from openspec specs (2026-08-28), merged with code-complete changes `add-task-liveness-safety` and `add-task-model-flag`

## Purpose

Submission and lifecycle of "detached" headless tasks: a task started from the laptop keeps running even with the laptop shut down, with `/ping` responding `HealthyBusy` for the entire duration (until AgentCore terminates the session at `MaxLifetime`), a persisted outcome readable offline (`sch status` without an active microVM), `--continue` handoff from interactive sessions, auto-approved permissions (headless path only), per-invocation model selection, "one task per workspace" concurrency, and an application timeout strictly below `MaxLifetime`.

## Scope

In scope:
- `sch task` detached submission, payload contract, per-harness headless argv (opencode, claude, pi).
- Task concurrency guard, double-writer advisory, application timeout, lifecycle and heartbeat, persisted state object.
- Offline observability, running-state staleness rendering, external watchdog reconciliation.
- Optional per-invocation `--model` selection and its observability.
- Synchronized preflight before a detached task.

Out of scope:
- Interactive/TUI sessions and their argv (see [interactive-shell-access.md](interactive-shell-access.md)); the TUI path never gains auto-approval flags.
- CLI cross-platform rules, stdout discipline, argv safety (see [cli-cross-platform.md](cli-cross-platform.md)).
- Sync reconciliation internals (`local-workspace-sync` capability); checkpoint backends and runtime image internals (`runtime-image` capability); Telegram delivery mechanics (`telegram-notifications` capability); runtime version/session rotation (`runtime-provisioning` capability).

## Requirements

### Detached submission

**R1.** The CLI SHALL expose `sch task <workspace> [--harness <opencode|claude|pi>] [--model <id>] [--variant <name>] [--continue] "<prompt>" [--timeout <s>]` which sends the `task` action to the shim via `InvokeAgentRuntime` and returns immediately with a `task_id`, without waiting for the prompt to complete. The client MUST NOT keep a persistent connection for the duration of the task; completion is observable later with `sch status`. The payload SHALL include the `harness` field so the shim builds the correct argv.

**R2.** The `--continue` flag SHALL request resumption of the most recent session of the workspace's chosen harness (same conversational context): claude resumes its JSONL transcript via `--resume <session-id>`; opencode resumes the same `opencode.db` session via `--session <session-id>`; pi resumes the worktree's most recent session via its native resume mechanism. Session resolution SHALL degrade to a fresh session on any error (no sessions, resolution failure), without failing the task. The resolution SHALL run after the workspace is ready (session-store restore landed), never at worker start on a cold boot, so a `sch stop` followed by `sch task --continue` resumes the pre-stop session instead of missing it. A requested continuation that resolves to nothing SHALL be surfaced, not silent: the persisted status records the miss (R18) and the submit acknowledgement echoes the request so the CLI can warn on images predating the feature. Without `--continue` the task starts from a fresh harness session.

**R3.** The headless argv MUST apply harness-specific auto-approval flags — `--auto` for opencode, `--dangerously-skip-permissions` for claude, no flag for pi (Pi has no permission prompts by design; SCH adds no approval gating in any mode) — and MUST redirect `stdin` from `/dev/null` for all harnesses. Auto-approval MUST be limited to the headless argv; the interactive TUI argv SHALL remain unchanged and keep presenting confirmation prompts.

**R4.** For harness=opencode and harness=claude the shim MUST explicitly select the `remote-auto` role contract via `--agent remote-auto` on the headless argv (argv-only; never added to interactive sessions). For harness=pi, which has no agent files, the same contract SHALL be applied via system-prompt (`pi -p --provider amazon-bedrock --append-system-prompt <remote-auto role file>`). The `SCH_TASK_AGENT` environment variable SHALL control the selection for opencode and claude: the default is `remote-auto`, a non-empty value replaces the forwarded name (validated by the harness per its own semantics), and an empty value omits `--agent` while keeping the auto-approval flags.

**R5.** When `sch task --continue` resumes a Claude transcript created by an interactive session, the shim SHALL combine `--resume <session-id>` with `--agent remote-auto`: the transcript and context remain those of the resumed session, while the new turns use the headless agent's prompt, tools, and model, with `stdin` from `/dev/null` and `--dangerously-skip-permissions` preserved.

### Per-invocation model selection

**R6.** The CLI SHALL accept an optional `--model <id>` on `sch task`, validated client-side by the shared helper `validate_model_or_die` (allowlist `[A-Za-z0-9._:/-]+`): an empty or out-of-allowlist value SHALL cause a usage error and exit without invoking the runtime. The value is opaque to `sch`: any identifier the harness accepts (`provider/model` for opencode, a Bedrock inference-profile ID for claude) is forwarded without interpretation; an unknown model produces the harness's native error and a normal `failed` task.

**R7.** The `task` action payload SHALL accept an optional `model` field, omitted from the JSON when not requested. When present, the shim SHALL validate it against `MODEL_ID_RE` (`[A-Za-z0-9._:/-]+`) as a second line of defense: a malformed value SHALL be rejected with `status=error` before any mutation — no task-slot update, no status object write to S3, no worker thread — leaving the slot free for a corrected resubmission.

**R8.** On a valid model the shim SHALL echo it in the submit acknowledgement (`{status: accepted, task_id, harness, model}`; key present only when requested) and forward it to the headless argv as a discrete `--model` pair: `opencode run --standalone [--session <id>] --model <id>[#<variant>] [--agent …] --auto -- <prompt>` and `claude -p [--resume <id>] --model <id> [--agent …] --dangerously-skip-permissions <prompt>`; for pi the model is forwarded as the provider/model pair of `pi -p`. On opencode (2.x) the reasoning-effort variant rides the model reference as `provider/model#variant` (there is no separate `--variant` flag), `--standalone` keeps the run inside a private embedded server (no per-user background service is started in the microVM), `--agent` is passed only when the seeded agent file exists (an unknown agent is a hard error on 2.x), and `--` guards the prompt so a prompt equal to a boolean literal (`y`, `true`, ...) is never consumed by `--auto`. When the flag is omitted, the payload has no `model` field and the argv carries no `--model` (the harness resolves its own default model), except for opencode `--continue` (R8a), which forwards the resumed session's own stored model and reasoning-effort variant. The same echo/forward contract applies to an explicit `--variant` (R8b).

**R8a.** On opencode `--continue` without an explicit `--model`, the shim SHALL forward the resumed session's own stored model (`provider/model` from the `opencode.db` `session_v2` row) and its stored reasoning-effort variant (opencode's provider-specific effort selector, e.g. `high`; the `"default"` sentinel and any malformed value are omitted) as one `--model provider/model#variant` reference. Rationale: the headless argv switches the agent to `remote-auto`, and opencode resolves a model-less prompt to the new agent's configured model and default effort — the TUI-selected model and effort would be lost and the session row clobbered. An explicit `--model` always wins (the stored variant is then dropped: it belongs to the previous model). A stored provider that cannot serve at runtime (neither in the seeded config, nor backed by a stored OpenCode credential — the `credential` table of `opencode.db`, where 2.x keeps `auth login`/`/connect` credentials —, nor backed by a staged provider key, nor `amazon-bedrock` via the execution role) still resolves to the runtime default model with no variant, and any read or validation failure degrades to the pre-feature behavior (no `--model`; the harness default applies). An explicit `--variant` (R8b) always wins over the stored one.

**R8b.** The CLI SHALL accept an optional `--variant <name>` on `sch task` (opencode only), validated client-side by `validate_variant_or_die` (same allowlist as `--model`; the effort name stays opaque). An explicit `--harness claude|pi` with `--variant` SHALL die before any mutation, as SHALL `--variant` on a workspace bound to another harness (after harness resolution, before the warmup). The payload SHALL carry `variant` only when requested; the shim SHALL validate it against `MODEL_ID_RE` (malformed → `status=error`, no side effects, same contract as R7), echo it in the ack (key present only when requested), forward it on the opencode headless argv as the `#<variant>` suffix of the `--model` reference (a variant with no model to attach to — no explicit model and no stored session model — is dropped), and record it in the initial status, every heartbeat, the terminal record, and `info` (present-iff-requested, like `model`). The CLI SHALL warn on a missing echo like R9. Precedence on opencode `--continue`: explicit `--variant` always wins; the stored variant applies only when no explicit `--model` was given. `sch run --variant` SHALL die client-side with a usage error naming the remedy (the opencode TUI has no `--variant` flag; effort is picked in the TUI model picker).

**R9.** After a positive acknowledgement, the CLI SHALL warn on stderr when `--model` was requested but the response does not echo it (a runtime image predating the feature: the task then runs with the default model), while still printing the `task_id` and completing normally.

**R10.** When a task was submitted with an explicit model, the persisted state SHALL include the `model` field in the initial status object, in every heartbeat, and in the terminal record, and the `info` action SHALL report it for the running task. The same present-iff-requested contract applies to an explicit `variant` (R8b). When no model was requested the field SHALL be absent (absence means "the harness's default model", except on opencode `--continue`, where absence means "the resumed session's stored model", forwarded per R8a without being recorded as a requested model; the shim MUST NOT attempt to resolve the default in any case). Readers (`sch status`, dashboard) MUST tolerate the fields' absence. Implemented refinement: for opencode `--continue` resuming an imported session that names an unconfigured provider, the shim MAY override the effective model with the runtime default and log the override.

### Concurrency and double-writer guard

**R11.** The shim SHALL maintain a single in-process task slot per workspace and SHALL reject a second submission while the slot is occupied, returning an explicit error with the in-flight `task_id` and starting no new work. The guard SHALL rely on in-process state (presence of the microVM = single writer) and MUST NOT trust any `running` state persisted to S3. After the in-flight task completes (any outcome), a new submission SHALL be accepted normally.

**R12.** On shim startup, a persisted `state=running` for its own workspace (previous microVM died mid-task) SHALL be reconciled to `state=interrupted` with unknown outcome; the shim then reports `Healthy` and a subsequent submission MUST be accepted without interpreting the orphan as a task in progress.

**R13.** When a submission receives the advisory `mark-interactive` signal (an OpenCode TUI active on the same workspace session), the shim SHALL proceed with the submission while emitting an explicit warning in the acknowledgement result rather than blocking. The advisory signal is best-effort: its absence is not treated as a safety guarantee.

### Timeout

**R14.** The shim SHALL enforce an application timeout (`SCH_TASK_TIMEOUT_S`, default 25200 s = 7 h; the implemented value is clamped to the bounds 5–27900 s) strictly below AgentCore's `MaxLifetime` (8 h), leaving margin for the final checkpoint so the terminal state is always owned by the shim, never imposed by the runtime. On reaching the timeout the shim SHALL terminate the harness process group, record `state=timed-out` (distinct from `failed` and `succeeded`), and perform the forced checkpoint.

### Lifecycle

**R15.** On submission the shim SHALL start the harness's headless command in the background, register the async task (`/ping` → `HealthyBusy`), persist `state=running` with `task_id` and `started_utc`, keep the heartbeat updated (recent `heartbeat_utc` without interfering with the workspace's periodic checkpoint), and allow the client to disconnect.

**R16.** On every terminal state the shim SHALL execute, in order: a synchronous forced checkpoint of the active backend, async completion, and persistence of the terminal state with separate `state` and `checkpoint_status` fields. A positive harness outcome with a failed checkpoint MUST be shown as `state=succeeded, checkpoint_status=failed` and MUST NOT be described as confirmed durability. On a non-zero harness exit the state SHALL be `failed` with `checkpoint_status` matching the checkpoint outcome, `exit_code`, `error` (truncated stderr), and timestamps; `/ping` returns `Healthy` and the microVM can go idle normally.

### Observability

**R17.** Task state (current and last terminated) SHALL be readable with `sch status <workspace>` without the microVM being active, using the operator's local AWS credentials against the object persisted on S3 alongside the checkpoint. `sch status` MAY enrich the view when the microVM is active (via the `info` action) but MUST work offline-first. On a workspace that never ran headless tasks the response SHALL be `state=none` (canonical), not a missing-object error.

**R18.** The persisted state object SHALL include at least: `task_id`, `state` (`running`/`succeeded`/`failed`/`timed-out`/`interrupted`/`none`), `checkpoint_status`, `exit_code`, `prompt`, `harness` (`opencode`/`claude`/`pi`), `started_utc`, `finished_utc`, `duration_s`, `heartbeat_utc`, `harness_session_id` (the resumed OpenCode session or Claude JSONL id when `--continue`, distinguishing handoff from fresh sessions), `image_version`, `error`; plus `model` when requested (R10) and `reconciled_by`/`reconciled_utc` when watchdog-reconciled (R20). When `--continue` was requested the state SHALL also carry `continue_requested: true` (present-iff-requested, like `model`) and, once the worker has resolved, `continue_resolved` (`true` when a prior session was resumed, `false` when the task started fresh because none was found); `continue_resolved` is absent while the resolution has not run yet. When the Telegram channel is configured, a terminal record SHALL also carry `notification_status` (`pending` until the terminal notification is accepted by the Bot API, then `delivered` with `notified_utc` and `notified_by` set to `shim` or `task-watchdog`); the three fields are absent when the channel is disabled and on `running` records (telegram-notifications R10).

**R19.** `sch status` SHALL compare `heartbeat_utc` with the local clock when the persisted state is `running`: beyond the staleness threshold (150 s; the heartbeat beats every ~30 s) the human rendering SHALL explicitly qualify the state as stale with the readable age of the last heartbeat, and the command SHALL exit with the dedicated code 3 (distinct from success 0 and errors 1). Missing or unparsable `heartbeat_utc` on a `running` state SHALL be treated as suspect (same stale qualification with a "no heartbeat" wording), without exceptions. Below the threshold and for terminal states the rendering and exit code MUST stay unchanged. The `--json` output MUST remain a byte-for-byte passthrough of the S3 object (no synthetic fields; the classification is observable only through the exit code). The command MUST NOT rewrite the persisted state in any case.

**R20.** A watchdog external to the microVM (Lambda on an EventBridge schedule, `rate(2 minutes)`) SHALL scan `checkpoints/*/task-status.json`. For each object with `state=running` and a heartbeat older than the reconciliation threshold (`SCH_WATCHDOG_STALE_S`, default 600 s, minimum 120 s, configurable) the watchdog SHALL rewrite the state to `interrupted`, annotating `reconciled_by: "task-watchdog"` and `reconciled_utc`, and SHALL notify the operator via Telegram when the channel is configured. The rewrite MUST preserve the object's existing `writer_token` and `session_epoch` and MUST be conditional (ETag match; no write on a lost race): an object written by a newer-epoch writer MUST NEVER be regressed, and the terminal record of a slow-but-alive task that completes after reconciliation MUST win over the watchdog's rewrite. The notification SHALL be sent only when the conditional write succeeds and, when the channel is configured, the rewritten record SHALL carry `notification_status: pending`, marked `delivered` by a second conditional write once the send succeeded: a failed send is retried by the terminal re-send below, so the interrupted notification is at-least-once, never lost. For each terminal object (`succeeded`/`failed`/`timed-out`/`interrupted`) with `notification_status: pending` and a `finished_utc` older than `SCH_WATCHDOG_NOTIFY_AFTER_S` (default 300 s, minimum 60 s) and younger than 24 h, the watchdog SHALL re-send the terminal notification and mark the record `delivered` (`notified_by: "task-watchdog"`) only on success, again with a conditional write fenced on the ETag read in the same tick; records without the field are never touched (telegram-notifications R10). State reconciliation is active even without Telegram configured, and without the channel no `pending` promise is written. Deliberate asymmetry with R19: a missing or unparsable heartbeat on a `running` record is NOT stale for the watchdog (it rewrites remote state; the suspect-but-unprovable case stays in the CLI's exit code 3 + stale rendering, which only degrades a presentation). The watchdog has no unconditional-write path and MUST NOT be able to wake the session it observes.

### Synchronized preflight

**R21.** `sch task <workspace>` SHALL accept the binding, bootstrap, and conflict options defined by `local-workspace-sync`. When sync is active, the client SHALL acquire the lease, complete reconciliation and the application barrier, and only then send the `task` action; the immediate return with `task_id` still refers to the remote submission that follows the preflight. After a positive acknowledgement the client SHALL close the `fs` channel and release the lease, while the task continues exclusively on the remote worktree; the detached task MUST NOT depend on the client staying online.

**R22.** The task's output SHALL be imported via the three-way reconciliation on the next synchronized invocation (compare against the preflight baseline, apply locally if non-conflicting). On a failed preflight (conflict, excluded file required by policy, unconfirmed barrier) the `task` action SHALL NOT be sent, no `task_id` SHALL be created, and the previous baseline SHALL stay intact.

### Combined handoff submission

**R23.** `sch task <workspace> --handoff [--handoff-session <id>] [--sanitize]` SHALL export the local OpenCode session (most recent session of the current directory by default, `--handoff-session` bypasses the resolution, `--sanitize` delegates redaction to `opencode session export --sanitize`), transfer it through the bundle channel, import it via the `session-import` action, and only then submit the `task` action with `continue=true` (the flag is implied by `--handoff`; an explicit `--continue` is accepted and redundant). `--handoff-session` and `--sanitize` without `--handoff` SHALL die with a usage error before any mutation.

**R24.** `--handoff` is opencode-only: `--harness claude|pi` combined with `--handoff` SHALL die before any local or remote mutation naming the opencode-only constraint; on a workspace already bound to `claude` or `pi` the command SHALL die the same way after harness resolution but before the warmup. On a new workspace `--handoff` without `--harness` SHALL bind the workspace to `opencode`. An invalid `--harness` value SHALL die with a usage error.

**R25.** With `--branch`, the seed SHALL complete and be persisted before the session import (seed-then-handoff), so a freshly seeded workspace never reports an empty-worktree warning for the import. With mirror sync, the sync preflight SHALL complete before the import. The import SHALL reuse the single task warmup (no second warmup); its `reimported` and `repoEmpty` warnings, version-skew diagnostics, and unknown-action remedy SHALL match `sch handoff`. On import failure the `task` action SHALL NOT be sent and no `task_id` SHALL be created. The export failure SHALL die before workspace creation. Stdout SHALL carry only the `task_id`; the imported sessionID SHALL be reported on stderr.

## Behavior

```
sch task myws "build and test"                  # submit, sub-second return with task_id
sch task myws --continue "proceed with build"   # resume the harness's latest session
sch task myws --model anthropus/claude-x "..."  # per-invocation model (echoed in ack)
sch task myws --timeout 3600 "..."              # application timeout override
sch task myws --handoff --branch feat/x "go"    # export local session, seed branch, import, submit with --continue
sch status myws                                 # offline read of the persisted outcome
sch status myws --json                          # pure S3 passthrough; exit 3 if running-stale
```

Edge cases:

- Laptop shut down right after submission → task continues; outcome persisted on S3; `sch status` on reconnect shows it.
- Second submit while `running` → explicit error naming the in-flight `task_id`; after any terminal outcome a resubmit is accepted.
- MicroVM killed mid-task with no trace → CLI shows `running (STALE: <age>)` with exit 3 within 150 s; the watchdog rewrites the state to `interrupted` (with `reconciled_by`/`reconciled_utc`) within ~600 s + one poll cycle and notifies via Telegram; a slow-but-alive task's own terminal write still wins.
- Task ends but its terminal Telegram notification is lost (microVM dies before the shutdown flush, Telegram unreachable from inside) → the record stays `notification_status: pending`; the watchdog re-sends the terminal notification within ~300 s + one poll cycle and marks it `delivered` by `task-watchdog`.
- Task exceeds the timeout → process group terminated, `state=timed-out`, forced checkpoint.
- Harness exits 0 but the final checkpoint fails → `state=succeeded, checkpoint_status=failed` with a diagnostic (execution and durability are reported separately).
- New CLI against an older image without `--model` support → warning that the runtime did not echo the requested model; the task runs with the default model.
- `--continue` with no prior sessions (or resolution failure) → fresh session, no error; the miss is recorded (`continue_resolved: false`) and rendered by `sch status`, and the submit acknowledgement echoes the request (`continue: true`) so the CLI warns on images predating the feature.
- Opencode `--continue` without `--model` → the resumed session keeps the model and reasoning effort selected in the TUI (forwarded as one `--model provider/model#variant` reference); an explicit `--model` overrides the model and drops the stored effort, while an explicit `--variant` overrides just the effort. `--variant` with a non-opencode harness dies client-side, as does `sch run --variant` (the opencode TUI has no such flag).
- Worktree modified by a detached task → retrieved by the next synchronized invocation's three-way reconciliation.

## Invariants

**I1.** Auto-approval flags (`--auto`, `--dangerously-skip-permissions`) appear only on headless argv, never on interactive/TUI argv of any harness.

**I2.** At most one active task slot per workspace; a `running` S3 state alone never blocks a submission.

**I3.** The application timeout is always strictly below AgentCore `MaxLifetime`.

**I4.** Terminal persistence always separates `state` from `checkpoint_status`; a failed checkpoint is never reported as confirmed durability.

**I5.** `sch status --json` is a byte-for-byte passthrough of the S3 object; the CLI never rewrites the persisted task status.

**I6.** The watchdog never regresses a newer-epoch writer's object, preserves the existing `writer_token`, and never performs an unconditional write.

**I7.** The `model` field is present in the payload/ack/state if and only if a model was requested; absence means "harness default", except on opencode `--continue`, where the resumed session's stored model/variant is forwarded to the argv (R8a) without being recorded as a requested model. The same present-iff-requested contract holds for `variant` (R8b).

**I8.** A malformed `model` produces no side effects anywhere in the chain: no slot mutation, no S3 write, no thread.

**I9.** An accepted task survives client disconnection; its durable outcome is readable offline with `state=none` as the canonical "never ran" answer.

## Cross-references

- [MANIFESTO](../../../MANIFESTO.md) — project constitution; historical source: `headless-task-execution` spec and changes `add-task-liveness-safety`, `add-task-model-flag` (git tag `pre-openspec-retirement`).
- CLI implementation: [cli/sch/commands/task.py](../../../cli/sch/commands/task.py) (parsing, `--model`, `--handoff`/`--handoff-session`/`--sanitize` with implied `--continue`, opencode-only fail-fast, seed-then-handoff ordering, ack echo warnings for `--model` and `--continue`), [cli/sch/commands/handoff.py](../../../cli/sch/commands/handoff.py) (`export_local_session`, `upload_and_import` shared helpers), [cli/sch/commands/status.py](../../../cli/sch/commands/status.py) (`STALE_AFTER_S = 150`, `EXIT_RUNNING_STALE = 3`, staleness rendering, continuation rendering), [cli/sch/cli.py](../../../cli/sch/cli.py) (`validate_model_or_die`), [cli/sch/runtime.py](../../../cli/sch/runtime.py) (`payload_task`).
- Shim implementation: [image/app/main.py](../../../image/app/main.py) (`_handle_task_action` validation and `continue` echo, post-ready continuation resolution (`_resolve_continue_session`, outcome persisted via `_persist_continue_outcome`), `_build_headless_argv`, `effective_model`/`effective_variant`, opencode session model+variant preservation (`_opencode_continue_model`, `_opencode_session_model_variant`), orphan reconciliation, `SCH_TASK_TIMEOUT_S`).
- Watchdog: [infra/task_watchdog_handler.py](../../../infra/task_watchdog_handler.py) (`SCH_WATCHDOG_STALE_S`, conditional rewrite, `reconciled_by`; `SCH_WATCHDOG_NOTIFY_AFTER_S`, terminal notification re-send and `notification_status` mark), declared in `infra/agent_runtime.yaml`.
- Verification: [bin/verify-headless-tasks.sh](../../../bin/verify-headless-tasks.sh) (both harnesses).
- Related capabilities (specs to be rationalized): `harness-selection`, `local-workspace-sync`, `runtime-image`, `run-model-selection`, `telegram-notifications`, `runtime-provisioning` (session rotation on runtime version change).
- Interactive counterpart: [interactive-shell-access.md](interactive-shell-access.md); shared CLI contract: [cli-cross-platform.md](cli-cross-platform.md).
