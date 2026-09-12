# Telegram: notifications and remote interaction

What the optional Telegram channel does and when it fires (notifications), and what you can do from the phone once the inbound channel is enabled (interaction). The step-by-step setup is in [Telegram setup](telegram-setup.md). Normative behavior: [`telegram-notifications.md`](specs/access-surfaces/telegram-notifications.md) and [`telegram-interaction.md`](specs/access-surfaces/telegram-interaction.md).

## Telegram notifications

Optional push channel: each microVM sends notifications straight to a Telegram
chat through the Bot API — outbound HTTPS only, so no central component, no
new infrastructure and no new IAM permissions. Off by default; it activates
only when both `SCH_TELEGRAM_BOT_TOKEN` and `SCH_TELEGRAM_CHAT_ID` are present
in the runtime environment.

**→ Setup, verification and troubleshooting: [docs/telegram-setup.md](telegram-setup.md)**

### Why

A headless task or a detached interactive session is otherwise observable only
by pulling state from the laptop (`sch status`, `sch dashboard`). The channel
answers, from the phone: *has my task finished?*, *is that detached session
still going where I expected?*, *is the agent stuck on a question?*

### Workflow

```
  lifecycle (shim: submit, terminal,        milestones (harness extensions:
  stall, shutdown)                          Claude hooks / OpenCode plugin /
                                            Pi sch-pi.ts extension)
         │ in-process call                          │ one JSON file per event
         ▼                                          ▼  in a local spool dir
   ┌───────────────────────────────────────────────────────┐
   │ notifier (one daemon thread in the shim)              │
   │  bounded queue → coalescing → topic resolve → send    │
   └───────────────────────────────────────────────────────┘
                         │ HTTPS sendMessage / createForumTopic
                         ▼
                   api.telegram.org  →  one topic per workspace
```

The harness extensions never touch the network: they drop an event file and
return immediately, so a slow or broken notification can never lengthen an
agent's turn. The shim's notifier thread is the single sender, which is what
makes batching, rate limiting, topic routing and error handling exist in
exactly one place.

**Routing — one topic per workspace.** With a supergroup that has Topics
enabled, the first notification of a workspace creates a forum topic named
after it, and the mapping is persisted in
`checkpoints/<workspace>/telegram-topic.json`, so a later microVM of the same
workspace keeps posting in the same topic. Concurrent sessions therefore never
mix. A chat without Topics degrades to plain messages prefixed with
`[<workspace>]`; a topic deleted by hand is recreated on the next event.
Granularity is per **workspace**, not per task — tasks are messages inside the
workspace's topic, matching the mental model of `sch list`.

**What you receive**

| Source | Events |
| --- | --- |
| Shim lifecycle | ▶️ task submitted (task id, harness, model, truncated prompt) · ✅/❌ terminal state (`succeeded`/`failed`/`timed-out`, terminal checkpoint `confirmed`/`failed`, duration, exit code on failure; delivered at-least-once — the external watchdog re-sends it, with a "delivered by the watchdog" line, when the microVM could not confirm delivery) · ⚠️ possible stall (at most once per task) |
| Harness milestones | 💬 end of turn with the final assistant text · 📝 todo-list updates · 🔧 in-turn tool digests (one coalesced message every few seconds at most during heavy tool use — prolonged silence is again a symptom, not the norm) · ⏸️ agent waiting for operator input · 🔐 OpenCode/Claude tool-permission request while detached |

opencode and claude produce the same *categories* of events (Claude Code hooks
in `settings.json`, an OpenCode plugin in the config `plugin/` dir, both seeded
idempotently by `init-workspace.sh` and never overwriting operator files).
Wording and detail differ where the extension APIs differ. **pi** ships its own
seeded extension (`sch-pi.ts`) and guarantees end-of-turn milestones only; Pi
has neither a permission prompt nor remote approval, and its APIs expose no
todo-list or waiting-for-input hook, so those categories degrade silently.

### Interactive attached/detached delivery

`sch run` and `sch shell` register a best-effort CommandShell presence lease
before opening the terminal. Each logical shell has an explicit, stable
`shellId`; each local client has a distinct `attachmentId`, renews its own lease
while connected, and removes only that lease on normal exit or `Ctrl+]`.
A session is **attached** while at least one client lease remains valid, so
disconnecting one of several clients does not enable notifications while
another client is still watching.

Spontaneous interactive milestones are suppressed while attached, delivered
while detached, and suppressed again after reconnect to the same
`runtimeSessionId` + `shellId`. Eligibility is fixed at the event's emission
time: an attached event is permanently discarded rather than replayed after a
later detach, while an event emitted detached remains eligible even if the
client reconnects before Telegram sends it. `sch web`, `sch attach` and
`sch acp` do not create CommandShell leases.

Normal detach removes the lease immediately. If a laptop, network or client
process disappears without cleanup, the heartbeat lease expires within the
bounded 30-second stale window. Missing, malformed, unreadable
or expired presence is treated as **detached** (fail-open toward delivery), so
a broken presence signal can cause a duplicate notification but cannot suppress
remote-work notifications indefinitely.

This gate applies only to spontaneous milestones from an interactive harness.
`sch task` lifecycle notifications and milestones are explicitly headless and
remain eligible regardless of attached interactive clients; inbound replies,
actionable errors and administrative events are also unchanged.

**Contract — best-effort, never in the way**

- Unconfigured means byte-identical behaviour: no network calls, no spool
  files, no Telegram log lines.
- Telegram being down, slow or misconfigured never fails or delays a task, a
  checkpoint, the harness startup or the microVM shutdown. Send errors are
  logged (with the token redacted) and dropped.
- The internal queue is bounded and prioritised: under saturation milestones
  are dropped first, terminal task notifications last. High-frequency events
  are coalesced into digests, each topic has a minimum send interval, and
  messages are truncated to the Bot API's 4096-char limit with an explicit
  marker. A `429` is honoured via `retry_after`.
- Terminal notifications still pending get one best-effort flush during
  ordered shutdown. There is no delivery guarantee: S3 (`sch status`) remains
  the authoritative state — the channel is observability, not bookkeeping.
- A lifecycle event emitted before the notifier thread exists (the `task`
  action is servable while the microVM is still booting) is spooled like a
  harness milestone and delivered when the notifier starts, instead of being
  dropped. Every successful send is logged at INFO, so "delivered" and "never
  processed" are distinguishable from the logs alone.

**Compatibility.** Presence is ephemeral and requires both sides of the
protocol. An older CLI does not publish leases, so an updated runtime treats it
as detached and preserves the historical always-notify behavior. A newer CLI
sends presence updates best-effort; an older runtime may reject or ignore them
without preventing the shell from opening. Attached suppression becomes fully
effective only when both CLI and runtime image are updated; there is no
workspace-data migration or cleanup.

**Limits** — single bot token and single chat per deployment; the token
travels as a CloudFormation `NoEcho` parameter into the runtime environment
(upgrade path: SSM SecureString); milestones require an image that ships the
hooks, older workspaces simply fall back to lifecycle notifications. The
channel is **outbound only**: replying, approving a tool or answering an
agent's question from Telegram is the scope of the follow-up section below,
**Telegram interaction**.

## Telegram interaction

Optional inbound channel on top of the notifications: act from the phone on
what the notifications show — approve or deny a tool, answer an agent's
question, send a follow-up prompt. Off by default; requires the notification
channel plus an explicit deploy flag:

```bash
export TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...
ENABLE_TELEGRAM_INTERACTION=true ./infra/deploy.sh
```

The deploy creates the router (webhook Lambda + API Gateway route + two
DynamoDB tables), generates a webhook secret and registers the webhook with
Telegram (`setWebhook`) automatically. Verification against a live runtime:
`bin/verify-telegram-interaction.sh <workspace>`.

### Architecture

The Bot API allows **one** update consumer per token, so with N concurrent
microVMs the inbound direction needs a central router — the same pattern as
the workspace registry (API Gateway + Lambda + DynamoDB, conditional, zero
always-on compute). The Lambda never executes anything: it validates and
enqueues; execution happens only in the microVM of the resolved workspace,
which polls its own queue. No inbound connections toward AgentCore, ever.

```
Telegram ──POST /telegram-webhook (secret header)──▶ Lambda
                                                       │ secret + chat allowlist
                                                       │ topic → workspace (routing table)
                                                       ▼
                                DynamoDB: commands (PK=workspace, TTL)
                                          routing  (PK=thread_id → workspace)
                                                       ▲
                microVM (notifier thread) ── poll + conditional Delete ──┘
```

The shim's notifier thread polls the queue every few seconds **only while**
an interactive session, a running task or the supervised backend is active;
a quiescent microVM stops polling and idles out normally. Consumption is a
conditional Delete before applying (exactly-once); commands not consumed
within 15 minutes (microVM off) are discarded **with a notification** — a
text written to a dead workspace is never applied hours later.

### What you can do per harness

| Capability | opencode | claude | pi |
| --- | --- | --- | --- |
| Approve/deny a new tool request from the inline keyboard | ✅ while detached | ✅ while detached | ❌ — Pi has no permission prompt or approval flow |
| While a CommandShell client is attached | native prompt only; no Telegram request | native prompt only; no Telegram request | tool executes with Pi's native no-prompt behavior |
| Timeout or reconnect with an unanswered remote approval | falls back to native behavior | falls back to native behavior | n/a |
| Answer the agent's question (free text → active session) | ✅ (via the shared `opencode serve` backend) | ❌ **accepted limit** — the detached TUI has no injection API; the topic replies with the alternatives (reattach with `sch run`, or wait and use the follow-up) | ❌ same accepted limit as claude, same reply |
| Follow-up at session end (free text → `task --continue`) | ✅ | ✅ | ✅ |
| Milestone categories | end of turn · todo list · waiting for input · tool-permission request | same | **end of turn only** (Pi has no todo tool and no waiting-for-input hook; the other categories degrade silently) |

Free text in a workspace topic is dispatched by state, in this order: a
pending approval gets a courtesy reminder that **only the buttons decide**;
an active opencode backend gets the text injected into its most recent
session (confirmation names the target session; the end-of-turn milestone is
the reply); an interactive claude **or pi** session gets the limit message; a
quiescent workspace gets a `task --continue` with the text as prompt — same
one-task-per-workspace rule, a running task refuses with its id.

### Remote approval, exclusive ownership and fail-safe fallback

The permission hooks for Claude and OpenCode become *decisional*: the Claude
`PreToolUse` hook and the OpenCode `permission.ask` plugin choose exactly one
owner for each new request. While at least one CommandShell lease is valid,
the native local prompt owns the request and no broker entry or Telegram
keyboard is created. While detached, the hook registers the request in a local
file broker, Telegram carries an Approve/Deny inline keyboard, and the hook
waits for the decision (default 10 minutes, `SCH_APPROVAL_TIMEOUT_S`, kept below
the 11-minute harness hook timeout):

- **approve** → the tool runs ("approved via Telegram");
- **deny** → the tool is blocked ("denied via Telegram");
- **timeout** → the hook steps aside and the harness's **native** behavior
  resumes (prompt/rules) — never auto-approval on timeout;
- the request message is edited with the outcome; late or duplicate taps get
  an "already resolved" answer and change nothing.

Ownership is exclusive and does not migrate retroactively on detach: a native
prompt already open remains local, and only a later request can select
Telegram. If a client reconnects while a remote request is pending, the broker
closes it as resolved elsewhere, updates any published message, and returns the
hook to native fallback without waiting for the remote timeout; late Telegram
taps cannot change the outcome. Headless tasks retain their existing
non-interactive permission behavior and never wait on this broker. Pi remains
outside the approval system in every state: its tools execute with Pi's native
no-prompt behavior while attached, detached and headless.

### Security

- The webhook endpoint is public but authenticated by the `setWebhook`
  secret token (verified on every request); everything else — wrong secret,
  foreign chats, unmapped topics — is dropped with an empty 200 and zero
  detail. Update rate is bounded by API Gateway's default throttling.
- Only the single configured chat id is accepted (single user).
- The Lambda can only enqueue commands and read the routing table; the
  runtime role gains only `Query`/`DeleteItem` on its command queue and
  `PutItem` on the routing table. The bot token stays in the runtime and
  Lambda environments (same SSM upgrade path as the notifications).

### Rollback

Redeploy with `ENABLE_TELEGRAM_INTERACTION=false` (or unset): deploy.sh calls
`deleteWebhook` and the stack removes every router resource. The notification
channel keeps working unchanged; the permission hooks fall back to their
observational behavior (no commands table in the environment → no wait, no
requests). Runtime images predating the feature simply never poll: commands
expire by TTL with the discard notification.
