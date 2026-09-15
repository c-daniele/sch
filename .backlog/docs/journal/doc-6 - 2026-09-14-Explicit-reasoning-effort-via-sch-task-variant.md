---
id: doc-6
title: 2026-09-14 Explicit reasoning effort via sch task --variant
type: other
created_date: '2026-09-14 16:55'
updated_date: '2026-09-14 16:55'
tags:
  - journal
---
# 2026-09-14 Explicit reasoning effort via sch task --variant

## Problem

Reasoning effort was only selectable in the TUI and preservable on
headless continue — no per-invocation lever existed, and the pinned
opencode TUI defines no `--variant` flag at all (only `opencode run`
does), so `sch run` could never forward one.

## What changed

See task TASK-4 and spec rules R8a/R8b. `sch task --variant <name>`
(opencode only, fail-fast on other harnesses) mirrors the `--model`
contract end to end: validation, payload, ack echo, status, heartbeat,
terminal record, and info. Precedence on continue: explicit variant wins,
stored variant applies only without an explicit model, explicit model
drops the stored variant without even reading the session. `sch run
--variant` is rejected client-side with the remedy (in-TUI picker).

## Outcome

New unit tests on both sides (CLI parsing/gates/payload/echo-warning,
shim validation/echo/observability/precedence). Full shim (428) and CLI
(586) suites and documentation checks pass.

## Lesson

Before plumbing a harness flag through, check the exact subcommand the
launch path execs: `opencode run --variant` exists but the bare TUI does
not, which fixed the design (headless-only flag) before any code was
written.
