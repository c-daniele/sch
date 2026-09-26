# Telegram interaction

> Domain: [Access surfaces](../README.md) · Status: Implemented · Source: rationalized from openspec specs (2026-08-28)

## Purpose

Minimal inbound interaction from Telegram to SCH workspaces: approve or deny the use
of a tool, answer a question from the agent in an active opencode session, and send
a follow-up after the session has ended — routed per topic, authenticated,
single-user, with explicit degradations where the channel cannot act. The outbound
companion channel (lifecycle events and milestones) is specified by
[Telegram notifications](telegram-notifications.md).

## Scope

In scope:
- Webhook-based central inbound router: authentication, chat allowlist, topic→
  workspace routing.
- Command delivery to the microVM by polling, without inbound connections.
- Remote tool approval (inline keyboard) and exclusive ownership of approvals
  versus the local native prompt.
- Text injection into active opencode sessions and after-session follow-up via
  `sch task --continue`.
- Explicit claude and pi limitations.

Out of scope:
- Outbound lifecycle events and spontaneous milestones, including the
  interactive-presence gate ([Telegram notifications](telegram-notifications.md)).

## Requirements

### Authenticated central inbound router

**R1.** The system SHALL receive Telegram updates through a webhook registered with
a secret token on a central endpoint (a single instance per bot token, as required
by the Telegram Bot API). The router MUST discard, with no side effects: requests
lacking the correct secret token, updates originating from chats other than the
configured one (single user), and messages in topics with no mapping to a known
workspace.

**R2.** Accepted updates SHALL be transformed into commands queued for the workspace
resolved from the `message_thread_id` (or from the fallback chat when operating
without Topics). Discarding a message for an unmapped topic SHALL produce a courtesy
reply in the topic explaining how the channel works.

### Command delivery to the microVM

**R3.** The microVM SHALL obtain the commands for its own workspace by periodically
polling the queue (low-frequency poll, only while a session or a task is active) and
acknowledging their consumption, without exposing any inbound endpoint. A command
MUST be consumed at most once.

**R4.** Commands older than an expiry threshold MUST be discarded with a
notification in the topic, so the operator knows the command was not applied. If the
inbound channel is not configured or the queue is unreachable, the primary flows
MUST continue unchanged, with the problem tracked in the logs.

### Remote tool approval

**R5.** When a detached interactive session of a harness equipped with permission
prompts reaches a new tool-approval wait, the notification in the topic SHALL
include an inline keyboard with Approve and Deny actions unambiguously tied to that
specific request. The received decision SHALL unblock the harness wait with the
corresponding outcome for claude (`PreToolUse` hook) and opencode (the OpenCode 2
plugin `evaluate` permission hook: the plugin holds an `ask` evaluation while the
remote decision is pending and resolves it to `allow`/`deny`; on timeout the
evaluation stays `ask` and the native prompt is published unchanged); the request
message SHALL be updated with the outcome (`editMessageText`).

**R6.** Before publishing the request the system MUST re-verify that no client lease
is valid; if the session is attached, the remote request SHALL be suppressed and
control MUST return to the native behavior without waiting for the timeout. Absent a
decision within the configured timeout, the wait MUST end, returning control to the
harness's native behavior, and the message SHALL be updated to indicate the timeout.

### Exclusive ownership of interactive approvals

**R7.** For opencode and claude, a tool approval request SHALL have a single owning
channel based on client presence at the moment it is born: the local native prompt
while at least one client lease is valid, or Telegram while the session is detached.
The system MUST NOT create a remote request nor wait for the Telegram broker when
the local channel is the owner. A native request already open MUST NOT be
retroactively migrated to Telegram after a detach.

**R8.** If a client re-attaches while a Telegram request is still pending, the
broker SHALL close it as resolved elsewhere, return control to the harness's native
behavior, and render late Telegram decisions ineffective (the topic reports that the
request was already resolved). Pi MUST remain excluded from any approval gating.

### Text injection and follow-up

**R9.** A free-form text message in the topic of an opencode workspace with an
active session on the shared backend SHALL be injected as a user message into the
most recent session of that backend. The end-of-turn milestone
([Telegram notifications](telegram-notifications.md)) SHALL be the only visible
reply on success. If the injection fails (backend not active, session not found),
the topic MUST receive an actionable error proposing the after-session follow-up.

**R10.** A free-form text message in the topic of a workspace with no interactive
session and no active tasks SHALL be submitted as a headless `--continue` task with
that text as the prompt, reusing the existing task path (same concurrency, timeout,
and checkpoint rules). The acceptance (task id) and the outcome arrive in the topic
as normal lifecycle notifications. If a task is already running, the text MUST be
rejected with a message reporting the active task (the "one task per workspace"
rule unchanged).

### Explicit harness limitations

**R11.** For a claude or pi workspace with an active detached interactive session, a
free-form text message MUST NOT be injected into the TUI. The topic SHALL receive a
reply that makes the limitation explicit along with the available alternatives
(re-attach with `sch run`, or wait for the end of the turn/session and use the
follow-up). Remote tool approval remains available for claude; pi offers no approval
gating.

**R12.** SCH SHALL preserve pi's native behavior, which executes tools without
permission prompts. The pi extension MUST NOT register a decision-making
`tool_call` hook nor produce Telegram approval requests — whether headless, with an
attached TUI, or with a detached TUI. Configuring the Telegram channel must not
change these semantics. The authorization boundary remains IAM/microVM.

## Behavior

- Operator writes in the topic of `my-project`: the router queues a command for
  `my-project` with the message text.
- Webhook request without the correct secret token: rejected without queuing
  anything and without revealing system details. A different user's chat: no command
  is queued for any workspace.
- Command queued while the microVM is active: applied exactly once, never
  re-applied on subsequent polls. Command left in the queue beyond the expiry
  threshold (microVM off): discarded, with a notification in the topic.
- MicroVM cannot poll the queue: sessions, tasks, and outbound notifications
  proceed without fatal errors; the problem is logged.
- Operator taps Approve on a detached-born request: the harness executes the tool
  and the message is updated with the approved outcome. Deny: the tool is not
  executed and the message shows the denied outcome.
- No decision within the timeout: the harness behaves as if the integration did not
  exist (native prompt/native rules) and the message indicates the timeout. A late
  tap after resolution (timeout, reconnect, or native decision) has no effect and
  the topic reports the request was already resolved.
- A request created detached but attached before publication: no Approve/Deny
  message is sent and the hook returns to the native approval flow without waiting
  for the full remote timeout.
- OpenCode or Claude requests permission while a client is attached: only the native
  prompt is used; nothing is published. A native prompt open at detach time is not
  replicated on Telegram; only a future request may choose the remote channel.
- Pi executes tools in attached, detached, or headless execution: no Approve/Deny
  request, no broker wait, unchanged by Telegram configuration.
- Answer to an opencode agent's question written in the topic: enters the active
  session as a user message, the turn resumes, and the end-of-turn milestone with
  the agent's reply arrives in the topic.
- "also fix the integration tests" in a quiescent workspace's topic: a `--continue`
  task starts with that prompt and the topic receives the submit notification with
  the task id. Text while a task is `running`: no new task; the topic receives the
  rejection with a reference to the active task.
- Free-form text on an active claude or pi detached session: no injection; the topic
  receives the explanation of the limitation with the alternatives.

## Invariants

**I1.** No update is ever processed without the correct webhook secret token, and no
update from a chat other than the configured one ever queues a command.

**I2.** A command is applied at most once, and an expired command is never applied.

**I3.** Every tool approval request has exactly one owning channel at any moment
(native prompt or Telegram), determined by client presence at the request's birth;
late decisions on a resolved request have no effect.

**I4.** The microVM exposes no inbound endpoint: it only polls the outbound command
queue.

**I5.** Pi is never gated: no Telegram approval request or broker wait exists on any
pi execution path.

**I6.** Unavailability of the inbound channel never degrades the primary flows
(tasks, checkpoints, sessions, outbound notifications).

## Cross-references

- Outbound notifications, milestone presence gate, topic routing:
  [Telegram notifications](telegram-notifications.md).
- Code: [image/app/telegram_interaction.py](../../../image/app/telegram_interaction.py),
  [image/app/main.py](../../../image/app/main.py) (permission hook, webhook side),
  [bin/verify-telegram-interaction.sh](../../../bin/verify-telegram-interaction.sh).
- Operator setup: [docs/telegram-setup.md](../../../docs/telegram-setup.md).
- [MANIFESTO](../../../MANIFESTO.md).
