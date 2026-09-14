---
id: TASK-2
title: Fix sch task --continue ignoring TUI-selected model/thinking effort
status: Done
assignee: []
created_date: '2026-09-14 11:27'
updated_date: '2026-09-14 11:37'
labels: []
dependencies: []
type: bug
ordinal: 1000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Running sch run, selecting a model (e.g. muse-spark-1.3) and thinking effort in the opencode TUI, then exiting and continuing via sch task MY_WS --continue reuses the same session history but falls back to the default model instead of the previously selected one.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 sch task --continue preserves the model selected in the prior TUI session when no explicit model flag is given
- [x] #2 Thinking effort / model parameters selected in TUI are likewise preserved or explicitly documented as unsupported
- [x] #3 Existing task/run/status tests still pass
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Shim: add _opencode_continue_model(session_id) helper reading the session row model JSON ({providerID, id, variant}) with validation, keeping the unavailable-provider default override. 2. Shim: _run_task uses it when harness=opencode and no explicit --model; extend _build_headless_argv with variant param emitting --variant for opencode. 3. Tests: fix test_task_continue fake_argv signature; add unit tests for preservation, explicit-wins, override, and degrade paths. 4. Docs: update headless-task-execution spec (R8/R10/I7), harnesses.md argv/continue rows, headless-tasks.md guide. 5. Verify: run image/app and cli unit tests.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Root cause: headless opencode argv switches agent to remote-auto with no --model/--variant; opencode resolves model-less prompts to the new agent's configured model and default effort, losing the TUI selection. Fix forwards the resumed session row's stored model+variant. Verified: 417 image/app tests OK, 564 cli/tests OK, bin/verify-docs.sh PASSED. Live e2e (bin/verify-headless-tasks.sh) not run — needs AWS.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
sch task --continue on opencode now forwards the resumed session's stored model (--model) and reasoning effort (--variant, e.g. high) when no explicit --model is given; explicit --model still wins. Changed image/app/main.py (_opencode_session_model_variant, _opencode_continue_model, _build_headless_argv variant, _run_task wiring), added unit tests, updated headless-task-execution spec (new R8a), harnesses.md, and headless-tasks.md. Verified with 417 shim + 564 CLI unit tests and verify-docs.sh, all passing.
<!-- SECTION:FINAL_SUMMARY:END -->
