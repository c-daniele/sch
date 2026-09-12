# Telegram notifications

> Domain: [Access surfaces](../README.md) · Status: Implemented · Source: rationalized from openspec specs (2026-08-28)

## Purpose

Outbound-only push notification channel from SCH to Telegram: the operator receives
on mobile the lifecycle events of headless tasks and the milestones of detached
interactive sessions, with messages from different sessions separated per topic and
without ever compromising the reliability of the existing flows. The inbound
companion channel (approvals, replies) is specified by
[Telegram interaction](telegram-interaction.md).

## Scope

In scope:
- Opt-in activation via runtime environment configuration.
- One forum topic per workspace, with mapping persistence and degradation.
- Headless task lifecycle notifications and harness session milestones.
- The interactive-presence gate for spontaneous milestones.
- Batching, rate limiting, and best-effort delivery of milestones; at-least-once
  delivery of terminal-status notifications.

Out of scope:
- Inbound commands, webhook authentication, and the chat allowlist
  ([Telegram interaction](telegram-interaction.md)).
- Tool approval flows ([Telegram interaction](telegram-interaction.md)).

## Requirements

### Opt-in activation

**R1.** Telegram notifications SHALL be disabled by default and activate only when
the runtime environment contains both `SCH_TELEGRAM_BOT_TOKEN` and
`SCH_TELEGRAM_CHAT_ID` non-empty. With the feature disabled, the runtime behavior
MUST be byte-identical to the pre-feature behavior: no network calls to Telegram, no
spool files, no Telegram-related error logs. The token MUST NOT appear in the logs
nor in the state objects persisted to S3.

### One topic per workspace

**R2.** When the configured chat is a supergroup with Topics enabled, the notifier
SHALL publish each workspace's messages in a dedicated forum topic, creating it on
the workspace's first notification (title containing the workspace name) and
persisting the `workspace → message_thread_id` mapping in the workspace's S3
checkpoint prefix, so that a subsequent microVM of the same workspace reuses the
same topic instead of creating a new one.

**R3.** If the chat does not support Topics, the notifier MUST degrade to messages
in the plain chat with a `[<workspace>]` textual prefix, without errors. If the
persisted topic turns out to be deleted (the API rejects the `message_thread_id`),
the notifier MUST create a new one and update the mapping.

### Headless task lifecycle notifications

**R4.** The notifier SHALL publish a notification for the following state
transitions of a headless task: submit accepted (workspace, abbreviated task id,
harness, explicit model if any, truncated prompt); terminal state
(`succeeded`/`failed`/`timeout`, with the terminal checkpoint outcome
`confirmed`/`failed` and the duration); heartbeat stall (heartbeat not updated
beyond a threshold while the task appears `running`). The terminal-status
notification MUST be emitted even when the terminal checkpoint fails, and its
delivery is at-least-once (R10). The stall notification MUST be emitted at most
once per task.

### Harness session milestones

**R5.** The notifier SHALL publish milestone events originating from the harnesses'
native extension mechanisms (for headless tasks and for interactive sessions with no
attached CommandShell clients): end of turn with the text of the concluding assistant
message (truncated to the Telegram length limit), agent todo-list update (concise
current state), agent waiting for operator input, tool approval request, and
supported progress digests. The per-harness mechanism is: native hooks in
`settings.json` for claude, a plugin in the config for opencode, a seeded SCH
extension for pi.

**R6.** Each milestone SHALL declare whether it originates from an `interactive` or
`headless` execution, and its emission instant. An `interactive` milestone SHALL be
eligible only if, at that instant, no client presence lease for the session is
valid; as long as at least one client remains attached, the milestone MUST be
dropped before enqueueing and MUST NOT be sent after a later detach. Absent,
unreadable, or expired presence state SHALL be treated as detached. A `headless`
milestone and the `sch task` lifecycle notifications MUST remain independent of
interactive presence. Replies to inbound Telegram commands, actionable errors, and
administrative events MUST remain outside this gate
([Telegram interaction](telegram-interaction.md)).

**R7.** Milestone categories that a harness's extension mechanism does not expose
MUST degrade silently (the minimum guaranteed set for pi is end of turn). Admitted
milestone events MUST traverse the same per-topic routing as lifecycle events. The
absence of the milestone mechanisms (hooks/extension not seeded, older harness) MUST
degrade to lifecycle events only, without errors.

### Batching and rate limits

**R8.** The notifier SHALL apply coalescing and rate limiting before sending:
high-frequency events from the same workspace (e.g. consecutive tool uses) MUST be
aggregated into a digest, the minimum interval between messages of the same topic
MUST be respected, and every message MUST be truncated to the Telegram API length
limit with a truncation marker. On a 429 API response, the notifier MUST honor the
indicated `retry_after` without losing terminal lifecycle events.

### Best-effort delivery

**R9.** Notification delivery MUST be asynchronous with respect to the primary
flows: Telegram API errors, network unreachability, or the absence of the topic MUST
NOT cause failure of, or appreciable delay to, the headless task, the checkpoint
loop, harness startup, or microVM shutdown. Send errors SHALL be recorded in the
runtime logs (without the token) and the internal event queue MUST be bounded: on
saturation, milestone events are discarded first, never the terminal-status
notifications still queued.

### Terminal notification reconciliation

**R10.** Terminal-status notifications SHALL be delivered at-least-once. When the
channel is configured, the shim SHALL write every terminal task record with
`notification_status: "pending"` together with the terminal state, and SHALL
rewrite it to `"delivered"` (adding `notified_utc` and `notified_by: "shim"`)
only after the Bot API accepted the terminal message, through a conditional
write that never lands on a record that meanwhile moved on (another task id, or
`running`). The external task watchdog (headless-task-execution R20) SHALL
re-send, from outside the microVM, every terminal record still `pending` whose
`finished_utc` is older than a grace period (`SCH_WATCHDOG_NOTIFY_AFTER_S`,
default 300 s, minimum 60 s) and younger than 24 h, with the same content as the
in-VM terminal message plus an explicit "delivered by the watchdog" line, and
SHALL mark it `delivered` (`notified_by: "task-watchdog"`) only when its own
send succeeded, with a conditional write fenced on the ETag read in the same
tick. A failed re-send leaves the record `pending` for the next tick. Records
without the field (channel disabled, images predating the protocol) MUST never
be re-sent; with the channel disabled the field MUST be absent, keeping the
record byte-identical (R1). A duplicate terminal notification is acceptable only
when a sender fails between the send and the delivered mark (crash, lost race);
a lost terminal notification is not. Milestones remain best-effort (R9).

## Behavior

- Runtime started without `SCH_TELEGRAM_BOT_TOKEN`: no Telegram event, no
  Telegram-related log line, task/checkpoint flows identical to the pre-feature
  behavior.
- Runtime started with a valid token and chat id, headless task submitted: task
  notifications appear in the configured chat (in the workspace's topic when
  Topics are available).
- A notifier log of a Telegram API error never contains the bot token value.
- First notification of `my-project` to a supergroup with Topics: a topic whose
  title contains `my-project` is created, the notification appears in it, and the
  `message_thread_id` is persisted in the workspace's S3 prefix. A new microVM of
  the same workspace (idle timeout, version bump) reuses that topic.
- `ws-a` and `ws-b` emitting in parallel: each message appears exclusively in its
  own workspace's topic.
- Private chat or group without Topics: notifications arrive with the
  `[<workspace>]` prefix; no error is logged for the `createForumTopic` failure.
- Headless task reaching `succeeded` with a confirmed checkpoint: the topic receives
  outcome `succeeded`, `checkpoint: confirmed`, and the duration. A harness exiting
  non-zero produces outcome `failed` with the exit code. A task appearing `running`
  with a stalled heartbeat produces a single possible-stall notification.
- Detached interactive session completes a turn: the topic receives the concluding
  assistant message, possibly truncated. The same turn while a client lease is
  valid: the milestone is dropped before enqueueing and never sent after the detach.
  After the last client detaches, subsequent milestones are published without
  terminating or restarting the remote session; a client reconnecting and renewing
  a lease re-enables suppression while the remote process continues.
- Two clients attached, one detaches: interactive milestones remain suppressed as
  long as the other client's lease remains valid.
- A headless milestone emitted while an interactive client is attached remains
  eligible; the gate does not modify the task's lifecycle, priority, or delivery.
- Agent todo-list update: the topic receives a concise representation of the list's
  current state (completed/in-progress/pending). Agent waiting beyond the harness
  threshold: the topic receives a blocked-on-question notification.
- A burst of low-relevance events is aggregated into a limited number of digest
  messages, not one message per event. Text beyond the Telegram length limit is
  truncated with an explicit marker.
- Telegram unreachable during a task termination: the task and its terminal
  checkpoint proceed exactly as without notifications; the send error is logged;
  the record stays `notification_status: pending` and the watchdog delivers the
  terminal message within the grace period plus one tick, then marks it
  `delivered` by `task-watchdog`.
- MicroVM killed between the terminal record and the shutdown flush: same
  recovery, the operator still receives outcome, checkpoint status and duration.
- Healthy path: the shim's notifier delivers the terminal message within seconds
  and marks the record `delivered` by `shim`; the watchdog finds nothing to
  re-send and no duplicate is produced.
- A new task submitted while the previous terminal mark is in flight: the mark
  refuses to overwrite the new `running` record; the worst case is one duplicate
  terminal message from the watchdog, never a regressed record.
- Runtime image without the protocol, or channel disabled: the record carries no
  `notification_status` and the watchdog never re-sends it.
- Saturated event queue: milestone events are discarded before terminal-status
  notifications; the runtime does not block.

## Invariants

**I1.** With the feature disabled, the runtime makes no Telegram network call and
produces no Telegram artifact (logs, spool files).

**I2.** The bot token never appears in any log line or in any state object persisted
to S3.

**I3.** At most one topic exists per workspace per configured chat; sessions of
different workspaces never mix topics.

**I4.** A milestone dropped by the presence gate is never replayed after the detach;
a milestone emitted while detached is never suppressed retroactively.

**I5.** Terminal-status notifications are never lost to batching, 429 handling,
queue saturation, or an in-VM send failure: a terminal record whose notification
is not confirmed as delivered within the grace period is re-sent by the external
watchdog (at-least-once). A duplicate is possible only when a sender fails
between the send and the delivered mark; every send failure is logged.

**I6.** No Telegram failure ever fails or appreciably delays a task, checkpoint,
harness startup, or microVM shutdown.

## Cross-references

- Inbound commands, approvals, webhook authentication, chat allowlist:
  [Telegram interaction](telegram-interaction.md).
- Terminal record schema and the external watchdog:
  [Headless task execution](headless-task-execution.md) (R18, R20).
- Code: [image/app/telegram_notifier.py](../../../image/app/telegram_notifier.py),
  [image/app/test_telegram_notifier.py](../../../image/app/test_telegram_notifier.py),
  [infra/task_watchdog_handler.py](../../../infra/task_watchdog_handler.py)
  (terminal re-send, `SCH_WATCHDOG_NOTIFY_AFTER_S`),
  [infra/test_task_watchdog_handler.py](../../../infra/test_task_watchdog_handler.py).
- Operator setup: [docs/telegram-setup.md](../../../docs/telegram-setup.md).
- [MANIFESTO](../../../MANIFESTO.md).
