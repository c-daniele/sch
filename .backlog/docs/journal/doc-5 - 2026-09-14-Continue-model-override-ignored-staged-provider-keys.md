---
id: doc-5
title: 2026-09-14 Continue-model override ignored staged provider keys
type: other
created_date: '2026-09-14 14:43'
updated_date: '2026-09-14 14:43'
tags:
  - journal
---
# 2026-09-14 Continue-model override ignored staged provider keys

## Problem

The TASK-2 preservation (forward the resumed session's stored model on
headless continue) routed every continue through the unavailable-provider
override, which judged availability off the seeded `opencode.json` provider
block alone (`amazon-bedrock` only). Key-based providers that work fine at
runtime via staged API keys (`opencode`, `anthropic`, ...) were therefore
still overridden to the default model — defeating the preservation for
exactly the reported case (`opencode/muse-spark-1.3-contributor-free`).

Live forensics confirmed the mechanics: the deployed shim faithfully
forwarded the remote session row, but the row never held the user's
selection (it lives in the laptop's local session on the sync mirror —
see the TASK-2 journal entry), and even a remote key-based selection
would have hit this override.

## What changed

See task TASK-3 and spec rule R8a. Provider availability is now config-file
entries plus staged-key providers (mapping mirrors `harness-wrapper.sh`)
plus `amazon-bedrock` via the execution role. Genuinely unavailable
providers still resolve to the runtime default.

## Outcome

Three new unit tests (staged-key preservation with the exact reported
model+variant, key-removed-mid-life fallback, genuine-unavailable
override). Full shim (419) and CLI (564) suites and documentation checks
pass.

## Lesson

Availability checks must mirror every credential channel, not just the
config file: the dispatcher adds providers at runtime from staged keys,
so any shim logic that reasons about "can this provider serve" must read
the same staging file.
