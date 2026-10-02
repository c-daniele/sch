---
id: decision-17
title: Telegram on an isolated stack binds to exactly one listed principal
date: '2026-10-02 16:13'
status: accepted
---
## Context

TASK-20 refused Telegram together with `ISOLATED_PRINCIPALS` (decision-14 point 9, spec
per-principal-isolation R44). The channel was wired at runtime level: the shared runtime
carried the bot token, the chat id and the table names; plane runtimes received the shared
environment minus every `SCH_TELEGRAM_*` variable (R21); the plane boundary denied every SCH
DynamoDB table (R20); the Telegram session policy was an inline policy of the shared role only
(R18). On an isolated stack the maintainer therefore lost every Telegram hook: milestones,
remote approvals, follow-ups.

On 2026-10-02 the maintainer asked whether the hooks could work with isolation on. Two options
were weighed: bind the existing single-operator channel to one listed principal, or per-owner
Telegram with one bot and one chat per principal (webhook demultiplexing by secret,
owner-prefixed partition keys scoped with `dynamodb:LeadingKeys`, per-owner secrets readable
by the webhook Lambda and the watchdog, per-entry configuration). The maintainer chose the
first; the second stays a follow-up.

## Decision

- Telegram stays a single-operator channel (one bot, one chat). With isolation on it binds to
  exactly one listed principal, named by the deploy-time variable `TELEGRAM_PRINCIPAL=<entry>`,
  one `ISOLATED_PRINCIPALS` entry.
- Only that principal's plane receives `SCH_TELEGRAM_BOT_TOKEN`, `SCH_TELEGRAM_CHAT_ID` and,
  with the inbound router, the two table names and the Telegram session policy on its
  execution role. Its boundary names the registry table instead of every SCH table, because
  one IAM Deny cannot say "every SCH table but the two Telegram tables". Every other plane is
  the R18/R21 plane of decision-14.
- The Telegram session policy becomes a customer managed policy of the runtime stack with its
  own output (`TelegramInteractionPolicyArn`), never part of `SharedRuntimePolicyArns`, so the
  plane template attaches one document defined once, as for every other shared policy.
- `infra/deploy.sh` refuses, before any stack changes: the Telegram switches with isolation on
  and no `TELEGRAM_PRINCIPAL`; `TELEGRAM_PRINCIPAL` without the Telegram switches, without
  isolation, naming more than one entry, or naming an unlisted entry. The plan written by the
  preflight marks the bound plane; the token and chat id reach the plane stack from the deploy
  environment, the table names and policy ARN from the runtime stack outputs.
- The task watchdog never applies the plain-chat fallback to an owner-tree workspace without a
  persisted topic mapping: only the bound owner's shim writes mappings, so another owner's
  stale task is reconciled silently instead of being announced in the bound chat.
- The Telegram verify scripts skip on an isolated stack unless `TELEGRAM_PRINCIPAL` is
  exported, and then must run as that principal.
- This supersedes point 9 of decision-14.

## Consequences

- Every Telegram hook works again on an isolated stack, for one listed principal; the other
  planes have no Telegram at all. The chat must be that principal's: everyone in it sees that
  principal's milestones and prompts.
- The bound principal's agent can read the bot token and holds the Telegram policy, exactly as
  the operator's agent on an isolation-off stack (spec residual risk X10).
- The first deploy of this template replaces the inline Telegram policy of the shared role with
  the managed one; with isolation off a running session may see a few seconds of AccessDenied
  on the Telegram tables during the swap (as for the TASK-20.3 policy move). With isolation on
  the shared runtime serves nobody, so nothing is affected.
- The plan column, the plane parameters and the managed policy are the pieces per-owner
  Telegram would reuse; that follow-up needs per-owner secrets and owner-scoped table keys on
  top of them.
- Not exercised live yet: a deploy with `TELEGRAM_PRINCIPAL` and the Telegram verify scripts
  run as the bound principal are operator-side checks (TASK-29).
- Spec: [`docs/specs/security/per-principal-isolation.md`](../../docs/specs/security/per-principal-isolation.md)
  (R6, R18, R20, R21, R44, X10, I9). Task: TASK-29.
