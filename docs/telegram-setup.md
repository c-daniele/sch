# Telegram Notifications — Setup Guide

Step-by-step configuration of the optional Telegram push channel described in
[Telegram notifications](telegram.md#telegram-notifications) (which explains
*what* the channel does and *when* it fires — this document is only about
*getting it working*).

The feature is **opt-in and off by default**: a deployment without the two
values below behaves exactly as if the feature did not exist.

**Contents**

1. [Create the bot](#1-create-the-bot-and-get-the-token)
2. [Create the chat](#2-create-the-chat-that-receives-the-notifications)
3. [Get the chat id](#3-get-the-chat-id)
4. [Verify the credentials before deploying](#4-verify-the-credentials-before-deploying)
5. [Deploy](#5-deploy-with-the-credentials)
6. [Verify the live channel](#6-verify-the-live-channel)
7. [Configuration reference](#configuration-reference)
8. [Troubleshooting](#troubleshooting)
9. [Security notes](#security-notes)
10. [Turning the feature off](#turning-the-feature-off)

---

## 1. Create the bot and get the token

1. In Telegram, open [@BotFather](https://t.me/BotFather) (the official one,
   blue checkmark).
2. Send `/newbot`.
3. Pick a display name (e.g. `SCH Notifier`) and a username ending in `bot`
   (e.g. `myproject_sch_bot`).
4. BotFather replies with the token:

   ```
   Use this token to access the HTTP API:
   123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   ```

That string is your `TELEGRAM_BOT_TOKEN`. Copy it **whole and unmodified** —
the part after the `:` is case-sensitive and a single altered character
produces a `401 Unauthorized` (see [Troubleshooting](#troubleshooting)).

Two BotFather commands are easy to confuse: `/token` **re-displays** the
current token (use it if you lost it), while `/revoke` **rotates** it —
see [Rotating the token](#rotating-the-token).

## 2. Create the chat that receives the notifications

The recommended target is a **supergroup with Topics enabled**: the notifier
then gives each workspace its own topic, so concurrent sessions never mix
(see [Telegram notifications](telegram.md#workflow) for the routing rules).

1. Create a new Telegram group and **add your bot** to it.
2. In the group settings, enable **Topics**. This converts the group into a
   supergroup (its chat id becomes a `-100…` number).
3. Promote the bot to **admin** with the **Manage topics** permission
   (required for `createForumTopic`). Being an admin also lets the bot see
   ordinary group messages, which makes step 3 easier.

A private chat with the bot also works, with a degraded layout: all
notifications arrive in the same conversation, each prefixed with
`[<workspace>]`. Nothing else changes.

## 3. Get the chat id

With the bot in the group and promoted, **post any message in the group**
(the bot only learns about a chat once it receives an update from it), then:

```sh
curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | python3 -m json.tool
```

Look for the `chat` object:

```json
"chat": {
    "id": -1001234567890,
    "title": "Sch notifier",
    "is_forum": true,
    "type": "supergroup"
}
```

- `chat.id` is your `TELEGRAM_CHAT_ID` (negative, starting with `-100` for a
  supergroup).
- `"is_forum": true` confirms Topics are enabled — that is what allows
  one topic per workspace.

**Alternatives** if `getUpdates` stays empty (see also
[Troubleshooting](#troubleshooting)):

- Send `/start@<your_bot_username>` in the group: commands addressed to a bot
  are always delivered, regardless of privacy settings, then retry
  `getUpdates`.
- Open [web.telegram.org](https://web.telegram.org), enter the group and read
  the id from the URL fragment (prefix it with `-100` if it is not already
  there).
- Forward any message from the group to [@getidsbot](https://t.me/getidsbot),
  which replies with the origin chat id.

## 4. Verify the credentials before deploying

Three curl calls confirm the whole chain in under a minute — do this *before*
deploying, so a later failure can only be on the SCH side.

```sh
TOKEN='123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
CHAT='-1001234567890'

# a) the token is valid
curl -s "https://api.telegram.org/bot${TOKEN}/getMe" | python3 -m json.tool
#> "ok": true, with the bot's username

# b) the bot can post in that chat
curl -s "https://api.telegram.org/bot${TOKEN}/sendMessage" \
     -d chat_id="${CHAT}" -d text="SCH test"
#> "ok": true, and the message appears in the group

# c) the bot can create topics (skip for a private chat)
curl -s "https://api.telegram.org/bot${TOKEN}/createForumTopic" \
     -d chat_id="${CHAT}" -d name="sch-setup-test"
#> "ok": true, with a message_thread_id
```

If (c) fails with `not enough rights`, the bot is missing the **Manage
topics** admin permission: notifications will still work, but they degrade to
the `[<workspace>]` prefix in the main chat instead of per-workspace topics.

Delete the `sch-setup-test` topic afterwards — the notifier creates its own
topics named exactly after each workspace.

## 5. Deploy with the credentials

The two values are passed to the deploy through the environment. They become
CloudFormation parameters (`TelegramBotToken`, declared `NoEcho`, and
`TelegramChatId`) and from there the runtime environment variables
`SCH_TELEGRAM_BOT_TOKEN` / `SCH_TELEGRAM_CHAT_ID`.

This step is the same whether you are installing SCH for the first time or
adding the channel to a running deployment — the channel is not a separate
installer, just two variables on the ordinary deploy. On a first install, do it
at step 6 of [From scratch on a new AWS
account](getting-started.md#from-scratch-on-a-new-aws-account), so the very first
runtime version already carries the channel and no redeploy is needed.

```sh
TELEGRAM_BOT_TOKEN='123456789:AA...' \
TELEGRAM_CHAT_ID='-1001234567890' \
sch deploy -s
```

`sch deploy` and `./infra/deploy.sh` are the same script — use the second one
when you work inside a git checkout. `-s` skips the image build; use it only
when the deployed image already contains the notifier. Otherwise build too (the
default build runs on CodeBuild, which is mandatory on x86_64 hosts since
AgentCore only accepts `linux/arm64`; `-l` builds locally with Docker instead).

**Both values are required.** The runtime activates the feature only when both
are non-empty; the deploy rejects a half-configured pair rather than deploying a
stack whose notifications are silently off.

**They must be re-supplied on every deploy.** Like every other deploy-time
switch (see [Deploy-time switches](deploy.md#deploy-time-switches-optional-features)),
an omitted value is deployed as empty and turns the feature off. Keep them in
`infra/setenv.sh` inside the support repo — the deploy sources that file itself,
so there is nothing to remember at deploy time:

```sh
# managed checkout (pipx/uv install): ~/.local/share/sch/repo/infra/setenv.sh
# git checkout:                       <your-clone>/infra/setenv.sh
export TELEGRAM_BOT_TOKEN=123456789:AA...
export TELEGRAM_CHAT_ID=-1001234567890
```

```sh
sch deploy -s
```

Two details of that file are worth knowing. The `export` form above is what
`infra/setenv.sh.example` uses and what `deploy.sh` warns about when it detects a
half-configured pair, but it is not what makes the deploy see the values: the
deploy sources the file in its own shell, so a plain assignment works too —
`export` additionally hands the values to anything else you run from a shell that
sourced the file. What does bite: the file's assignments **win over the
surrounding environment**, so once the token is in the file,
`TELEGRAM_CHAT_ID=other sch deploy` does not override it — edit the file to
change a value.

Confirm what actually landed on the stack:

```sh
aws cloudformation describe-stacks --stack-name sch-dev-runtime --region eu-west-1 \
  --query "Stacks[0].Parameters[?ParameterKey=='TelegramChatId'].ParameterValue" --output text
#> -1001234567890
```

(The token parameter always reads back as `****` because it is `NoEcho`, so
the chat id is the one to check.)

> **Session storage reset.** Changing the runtime environment updates the
> AgentCore Runtime version, and a version update resets the session storage
> of every session. This is not data loss — each workspace is repopulated
> from its S3 checkpoint on its next access — but microVMs do restart from
> their last checkpoint. See [L2 durability](workspaces.md#l2-durability-s3-checkpoint).

## 6. Verify the live channel

```sh
./bin/verify-telegram-notifications.sh <workspace-a> <workspace-b>
```

The script asserts what can be asserted programmatically:

- the deployed stack really carries the chat id (otherwise it aborts
  immediately: the feature is off);
- a real headless task in each workspace reaches a terminal state;
- each workspace has its own `checkpoints/<workspace>/telegram-topic.json`
  and the two mappings differ — the proof that messages are routed per
  workspace and cannot mix;
- the mapping object carries no token material.

A useful side check, straight from the runtime logs:

```sh
aws logs filter-log-events \
  --log-group-name /aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT \
  --region eu-west-1 --filter-pattern "telegram" \
  --query 'events[].message' --output text
#> ... INFO sch_shim: telegram notifier started (chat configured)
```

That line means the shim booted with the feature active. Its absence means
the environment variables never reached the microVM.

And the mapping itself tells you which routing mode you got:

```sh
aws s3 cp s3://<checkpoint-bucket>/checkpoints/<workspace>/telegram-topic.json - --region eu-west-1
#> {"chat_id": "-1001234567890", "thread_id": 9, "fallback": false}
```

`"fallback": false` with a `thread_id` = per-workspace topic.
`"fallback": true` with `"thread_id": null` = plain chat with `[workspace]`
prefixes (chat without Topics, or bot lacking *Manage topics*).

**Manual check (nothing can automate this):** the Bot API gives a bot no way
to read back its own messages, so open the chat on your phone and confirm the
messages are actually readable: the ▶️ submit and ✅/❌ terminal message of
each task, in the topic named after the workspace.

### Verify attached/detached behavior

Interactive milestone delivery uses a short-lived presence lease for
CommandShell clients opened by `sch run` and `sch shell`:

1. Start `sch run <workspace>` and complete a harness turn while the terminal
   remains connected. Its spontaneous milestone must be suppressed because the
   session is attached.
2. Detach with `Ctrl+]` without terminating the remote TUI, then let it emit a
   new milestone. The new milestone must arrive in the workspace topic.
3. Reconnect to the same `runtimeSessionId` + `shellId` with
   `sch shell <workspace> --shell-id <id>`. Later interactive milestones must
   be suppressed again.

`sch run`/`sch shell` generate and pass an explicit stable `shellId` when one
is not supplied. Each local client gets its own `attachmentId` and renews a
best-effort lease; the session stays attached until the last valid client
leaves. A normal detach removes that client's lease immediately. If the client,
laptop or network disappears without cleanup, stale presence expires within a
bounded 30-second window. Missing, malformed, unreadable or
expired presence fails open as detached, favoring notification delivery over
indefinite suppression.

The notifier decides from the state at the event's emission time, not the later
send time. A milestone suppressed while attached is discarded and is never
replayed after detach; one emitted while detached remains eligible if a client
reconnects before delivery. `sch web`, `sch attach` and `sch acp` do not create
these CommandShell leases.

This gate does not apply to `sch task`: task lifecycle and harness milestones
are marked headless and remain eligible even when an interactive client is
attached to the workspace. Inbound Telegram replies, actionable errors and
administrative events are also independent of presence.

For OpenCode and Claude approvals, ownership is exclusive: attached requests
use only the native prompt, while new detached requests use only Telegram.
Detaching does not copy an already-open native prompt to Telegram. Reconnecting
while a remote request is pending closes it as resolved elsewhere and returns
the harness to native fallback; any late button tap is ignored. Pi is never in
the approval broker and gains no prompt from Telegram: its tools keep their
native no-prompt behavior while attached, detached and headless.

When testing OpenCode with two separate `bash` calls, approve the first call
with the one-shot/non-persistent choice. A session-wide grant such as "always
allow bash" means OpenCode does not emit `permission.ask` for the second call;
without a new permission event there is intentionally nothing for Telegram to
publish after detach.

Mixed versions are safe but do not provide the complete gate. An older CLI
publishes no lease, so a newer runtime treats the session as detached and keeps
the historical always-notify behavior. A newer CLI sends presence updates
best-effort to an older runtime; unsupported updates do not prevent the shell
from opening. Deploy both the updated CLI and runtime image for attached
suppression; no persisted workspace migration is required.

## Configuration reference

| Variable (runtime env) | Source | Default | Meaning |
| --- | --- | --- | --- |
| `SCH_TELEGRAM_BOT_TOKEN` | CFN `TelegramBotToken` (NoEcho) | *empty* | Bot token. Empty = feature off. |
| `SCH_TELEGRAM_CHAT_ID` | CFN `TelegramChatId` | *empty* | Target chat. Empty = feature off. |
| `SCH_TELEGRAM_STALL_SECONDS` | runtime env | `300` | Silence after which a `running` task is reported as possibly stalled (once per task). Minimum 60. |
| `SCH_TELEGRAM_SPOOL_DIR` | runtime env | `/tmp/sch-telegram-spool` | Local spool where the harness hooks drop milestone events for the notifier. Rarely changed; useful for tests. |
| `SCH_TELEGRAM_COMMANDS_TABLE` | CFN (interaction only) | *absent* | Inbound command queue table (add-telegram-interaction). Absent = inbound channel off, hooks observational. |
| `SCH_TELEGRAM_ROUTING_TABLE` | CFN (interaction only) | *absent* | thread→workspace routing table published by the notifier. |
| `SCH_APPROVAL_DIR` | runtime env | `/tmp/sch-approval` | File broker between the permission hooks and the remote decisions. |
| `SCH_APPROVAL_TIMEOUT_S` | runtime env | `600` | Remote approval wait. Must stay below the 660 s hook timeout seeded in `settings.json`. |

### Inbound interaction (add-telegram-interaction)

Deploy with `ENABLE_TELEGRAM_INTERACTION=true` (in addition to the two
credentials): deploy.sh generates a webhook secret (or reuses an exported
`TELEGRAM_WEBHOOK_SECRET`), creates the router (webhook Lambda + API Gateway
+ DynamoDB command/routing tables) and registers the webhook via
`setWebhook`. Verify with `bin/verify-telegram-interaction.sh <workspace>`;
inspect the registration anytime with `curl ".../getWebhookInfo"`. Rollback:
redeploy with the flag `false` — deploy.sh calls `deleteWebhook` and the
stack removes the router. See [Telegram interaction](telegram.md#telegram-interaction).

Where the moving parts live:

| Path | Role |
| --- | --- |
| `image/app/telegram_notifier.py` | The notifier: queue, batching, rate limiting, topic resolution, Bot API client (stdlib only). |
| `image/app/telegram_interaction.py` | Inbound side: command queue poll (exactly-once consume), approval broker, keyboard/outcome message sync. |
| `image/app/main.py` | Lifecycle emission points (task submit/terminal, stall detection, shutdown flush), free-text dispatch (D5), opencode injection. |
| `image/claude-templates/hooks/telegram-hook.py` | Claude Code milestone hook + decisional PreToolUse (remote approval). |
| `image/opencode-templates/plugin/sch-telegram.js` | OpenCode 2 plugin (default export `{id, setup}`): milestones via the event stream, activity markers via tool hooks, decisional `evaluate` permission hook (remote approval). |
| `image/scripts/init-workspace.sh` | Idempotently seeds the hook/plugin into each workspace. |
| `infra/agent_runtime.yaml`, `infra/deploy.sh` | Parameters and wiring. |
| `infra/telegram_webhook_handler.py` | Central webhook router Lambda (secret check, chat allowlist, topic→workspace routing, command enqueue). |
| `infra/task_watchdog_handler.py` | External watchdog Lambda: re-sends terminal notifications the microVM did not confirm as delivered (`notification_status: pending` older than `TASK_WATCHDOG_NOTIFY_AFTER_SECONDS`), besides reconciling orphaned `running` tasks. |
| `bin/verify-telegram-notifications.sh`, `bin/verify-telegram-interaction.sh` | Live verification. |
| `docs/specs/access-surfaces/telegram-notifications.md` | Normative specification for the outbound notification pipeline. |
| `docs/specs/access-surfaces/telegram-interaction.md` | Normative specification for the inbound interaction channel. |

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `{"ok": false, "error_code": 401, "description": "Unauthorized"}` | Token wrong (a mistyped character — `O` vs `0` is the classic) or revoked. | Re-copy it verbatim (`/token` in BotFather re-displays it), validate with `getMe`, and if it really is compromised or lost, rotate it with `/revoke`. |
| `getUpdates` returns `"result": []` | The bot has received no update from that chat yet. | Post a message in the group *after* adding the bot; or send `/start@<bot_username>`, which is always delivered. |
| `getUpdates` still empty | A webhook is registered on the token, which disables `getUpdates`. | `curl ".../getWebhookInfo"`; if `url` is set, `curl ".../deleteWebhook"`. |
| Deploy succeeds, nothing ever arrives | `TelegramChatId` was deployed empty — the values were not supplied to *that* deploy, or a helper file assigns them without `export`. | Put them in `infra/setenv.sh` with `export` (see [step 5](#5-deploy-with-the-credentials)) and check the deployed parameter. |
| No `telegram notifier started` line in the logs | The env vars never reached the microVM (feature off), or the deployed image predates the notifier. | Check the stack parameter and the deployed `ApplicationVersion`. |
| First `sch task` after enabling returns nothing, with a warmup warning | Cold start: the runtime version bump means the first invocation lands on a not-yet-booted microVM. | Retry; the verify script already retries 3×/20s. |
| Messages arrive with `[workspace]` prefixes instead of topics | The chat is not a forum, or the bot lacks *Manage topics*. | Enable Topics and grant the permission; delete `telegram-topic.json` for the affected workspaces so the mapping is recomputed. |
| Notifications are noisy during long tool runs | Expected: consecutive low-relevance events are coalesced into digests, but a busy agent still produces several. | Nothing to configure yet — the categories are fixed in this phase (see the change's design notes, risk "Operator noise"). |
| A topic was deleted by mistake | The persisted `message_thread_id` no longer exists. | Nothing to do: the next notification recreates the topic and rewrites the mapping. |
| Everything works, but tasks are unaffected when Telegram is down | By design: the channel is best-effort and never blocks tasks, checkpoints or the harness. | — |
| A terminal message arrives minutes late with a "delivered by the watchdog" line | The microVM could not confirm its own terminal notification (died before the shutdown flush, Telegram unreachable from inside, notifier error); the watchdog re-sent it after `TASK_WATCHDOG_NOTIFY_AFTER_SECONDS` (300 s). | Nothing to fix for that task. If it repeats on every task, check the shim logs (`telegram send failed`, `telegram flush send failed`) and `sch status --json` (`notification_status`, `notified_by`). |
| Interactive milestones arrive while `sch run`/`sch shell` is attached | The CLI or runtime image is older, the 30-second lease expired because presence heartbeats failed, or the client is `sch web`/`sch attach`/`sch acp` rather than CommandShell. | Update both CLI and runtime; check presence-update errors. Fail-open delivery is intentional when presence cannot be proven. |
| Milestones remain suppressed briefly after an abrupt disconnect | The lost client could not send cleanup and its lease is still fresh. | Wait for the bounded 30-second stale window; normal `Ctrl+]` detach removes the lease immediately. |
| Detaching one terminal still does not enable milestones | Another local client still has a valid attachment lease for the same session. | Detach the remaining CommandShell client(s), or wait for stale leases to expire. |
| An approval keyboard is closed on reconnect | Expected exclusive ownership: reconnect transfers pending approval back to native harness behavior. | Answer the native prompt; late Telegram taps are intentionally ignored. |
| No approval keyboard appears for Pi | Expected: Pi has no permission prompt and never participates in local or Telegram approval gating. | Use OpenCode or Claude when per-tool approval is required. |

## Security notes

- The token lives in the CloudFormation parameter (`NoEcho`) and in the
  runtime environment. It is **never logged** (error messages are sanitized)
  and **never written to S3** — the persisted mapping contains only chat id,
  thread id and the fallback flag.
- Keep local helper files with the token out of version control
  (`infra/setenv.sh` is git-ignored). If a token is ever exposed — a shell
  history, a log, a screenshot, a pasted transcript — rotate it (see below).
- Anyone holding the token can post as the bot; anyone in the chat sees the
  notifications, which include prompts and assistant text. Treat the chat as
  having the same confidentiality as the workspaces it reports on.
- This phase is deliberately single-user and single-chat. The notifications
  are **outbound only**; enabling `ENABLE_TELEGRAM_INTERACTION` adds one
  inbound surface — the webhook endpoint — authenticated by the `setWebhook`
  secret token, restricted to the configured chat id, and limited to
  enqueueing commands (the Lambda executes nothing; see [Telegram interaction →
  Security](telegram.md#security)).
- Upgrade path for stronger secret handling: read the token from SSM
  Parameter Store (SecureString) instead of an environment variable. The
  behaviour contract does not change, so this is a drop-in improvement.

### Rotating the token

1. In [@BotFather](https://t.me/BotFather), send `/revoke` and select the bot.
   The reply contains a new token and the previous one stops working
   **immediately**. (`/token` only re-displays the current token — it does not
   rotate it.)
2. Update the value in `infra/setenv.sh` (see
   [step 5](#5-deploy-with-the-credentials)) and redeploy so the new token
   reaches the runtime environment:

   ```sh
   sch deploy -s
   ```

3. Confirm the swap:

   ```sh
   curl -s "https://api.telegram.org/bot<new-token>/getMe"   #> "ok": true
   curl -s "https://api.telegram.org/bot<old-token>/getMe"   #> 401 Unauthorized
   ```

Rotation does **not** affect the chat id or the existing topics: the persisted
mapping holds only `chat_id`/`thread_id`, so every workspace keeps posting in
the topic it already had. Remember that the redeploy bumps the runtime version
(microVMs restart from their last L2 checkpoint).

Finally, purge the old token from local traces — shell history in particular:

```sh
grep -n "<old-token-prefix>" ~/.bash_history
history -d <line-number>      # or edit the file, then: history -c && history -r
```

## Turning the feature off

Remove the two values from `infra/setenv.sh` (or leave either one empty) and
deploy:

```sh
sch deploy -s
```

The parameters become empty, the runtime environment variables disappear, and
the runtime returns to its pre-feature behaviour: no network calls, no spool,
no Telegram log lines. The seeded hooks and plugin stay in place but are
immediate no-ops without the configuration.
