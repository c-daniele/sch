---
id: doc-25
title: 2026-10-02 Telegram on an isolated stack binds to one listed principal
type: other
created_date: '2026-10-02 16:21'
updated_date: '2026-10-02 16:24'
tags:
  - journal
---
# 2026-10-02 Telegram on an isolated stack binds to one listed principal

Task: TASK-29. Decision: decision-17 (supersedes point 9 of decision-14). Spec:
`docs/specs/security/per-principal-isolation.md` (R6, R18, R20, R21, R44, X10, I9),
`docs/specs/access-surfaces/telegram-notifications.md` (R3).

## The problem

Since the per-principal isolation work (TASK-20) the deploy refused Telegram together with
`ISOLATED_PRINCIPALS`. Telegram had been wired at runtime level: the shared runtime carried the
bot token and the table names, every plane runtime received the shared environment minus the
Telegram variables, the plane boundary denied every SCH DynamoDB table, and the Telegram
session policy lived inline on the shared role only. On an isolated stack the maintainer lost
every Telegram hook: milestones, remote approvals, follow-ups after a session.

The maintainer asked whether the hooks could come back with isolation on. Two routes were
weighed. Per-owner Telegram (one bot and one chat per principal) needs webhook demultiplexing
by secret, owner-prefixed table keys, per-owner secrets for the Lambda and the watchdog, and
per-entry configuration. Binding the existing single-operator channel to one listed principal
needs none of that. The maintainer chose the binding.

## What changed

- A new deploy variable, `TELEGRAM_PRINCIPAL`, names one `ISOLATED_PRINCIPALS` entry. The
  preflight refuses the Telegram switches without it, and refuses it without Telegram, without
  isolation, with more than one entry, or with an unlisted entry, before any stack changes. The
  plan it writes marks the bound plane.
- Only the bound plane receives the token, the chat id and, with the inbound router, the table
  names and the Telegram policy on its execution role. Its boundary names the registry table
  instead of every SCH table, because one IAM Deny cannot say "every SCH table but two". Every
  other plane is exactly the plane of TASK-20.
- The Telegram session policy became a customer managed policy of the runtime stack with its
  own output, kept out of the shared-policy list, so the plane template attaches one document
  defined once.
- The task watchdog no longer uses the plain-chat fallback for an owner-tree workspace without
  a topic mapping. Only the bound owner's shim writes mappings, so another owner's stale task is
  reconciled silently instead of being announced in the bound chat.
- The Telegram verify scripts skip on an isolated stack unless `TELEGRAM_PRINCIPAL` is exported,
  and then say that the caller must be that principal.
- Spec, decision record, deploy header, setenv example, operator guides and changelog state the
  new behavior; nothing says Telegram is refused with isolation on any more.

## Outcome

Infra suite 226 tests and cli suite 678 tests pass; `bin/verify-docs.sh` passes. New tests pin
the bound and unbound plane shapes (environment, managed policies, boundary), every refused
switch combination, the per-plane deploy parameters, and the watchdog's owner-tree rule. The
plane template validates with CloudFormation. Not exercised live yet: a deploy with
`TELEGRAM_PRINCIPAL` and the two Telegram verify scripts run as the bound principal; the first
deploy also swaps the inline Telegram policy of the shared role for the managed one.

## Lesson

A feature refused "because of isolation" is often refused because of how it was wired, not
because the boundary forbids it. Here the boundary already had every piece needed (one plane
per principal, a per-plane environment and role), and the real blockers were three defaults
written for the shared runtime. Listing those defaults first made the smallest safe option
obvious and kept the larger one (per-owner bots) as a follow-up instead of a prerequisite.

One IAM detail is worth remembering: a Deny statement cannot express "every resource matching a
pattern except these two". When a carve-out is needed, the Deny has to name the remaining
resources explicitly, and the spec has to say so, or the next reader will try to tighten it
back.
