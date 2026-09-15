---
id: doc-1
title: 2026-09-14 Headless continue kept the conversation but lost the model
type: other
created_date: '2026-09-14 11:36'
updated_date: '2026-09-14 11:36'
tags:
  - journal
---
# 2026-09-14 Headless continue kept the conversation but lost the model

## Problem

Continuing an interactive session headlessly (`sch run`, pick a model and
thinking effort in the opencode TUI, `/exit`, then
`sch task MY_WS --continue "..."`) resumed the same conversation history
but ran the new turns with the default model and default reasoning effort
instead of the TUI selection.

## Cause

The headless opencode command switches the agent to `remote-auto` without
passing a model. Opencode resolves such a model-less prompt to the new
agent's configured model and default effort, so the TUI selection was
discarded (and the session row overwritten). The reasoning effort was
always lost because nothing ever forwarded opencode's `--variant` flag.

## What changed

See task TASK-2 and the spec `docs/specs/access-surfaces/headless-task-execution.md`
(new rule R8a). On opencode `--continue` without an explicit `--model`, the
shim now reads the resumed session's stored model and effort from
`opencode.db` and forwards them on the headless command. An explicit
`--model` still wins. Every read or validation failure degrades to the
previous behavior (harness default), and there is deliberately no
`--variant` CLI flag: an explicit effort cannot be requested, only
preserved from the resumed session. The `model` field in task status stays
request-only, so its absence on a continued task means "the resumed
session's model".

## Outcome

Unit coverage for the preservation, the explicit-wins precedence, the
unavailable-provider default override, and all degrade paths. Full shim
(417) and CLI (564) suites pass, as do the documentation checks. The live
end-to-end headless verification was not run here (it needs AWS); worth
running on the next deploy.

## Lesson

When a harness resolves omitted flags from the *agent* rather than the
*session*, switching agents on resume silently changes the model. Any
future headless flag that opencode resolves per-agent needs the same
treatment: forward the session's stored value explicitly on continue.
