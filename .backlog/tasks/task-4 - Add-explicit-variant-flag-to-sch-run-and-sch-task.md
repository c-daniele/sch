---
id: TASK-4
title: Add explicit --variant flag to sch run and sch task
status: Done
assignee: []
created_date: '2026-09-14 16:38'
updated_date: '2026-09-14 16:55'
labels: []
dependencies: []
type: feature
ordinal: 7000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Reasoning effort (opencode model variant, e.g. high/xhigh) is currently only selectable in the TUI and preservable on headless continue; there is no way to request it per-invocation. Operators hitting thinking-replay failures or wanting a different effort for one run have no lever. Mirror the --model contract end to end.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 sch task --variant <name> forwards it headlessly; explicit variant beats stored variant, explicit model without variant still drops stored variant
- [x] #2 Malformed variant rejected with usage error client-side and status=error shim-side, no side effects
- [x] #3 Ack echo, task-status/heartbeat/terminal/info mirror the --model contract (present-iff-requested)
- [x] #4 sch run --variant is rejected client-side with an explicit usage error (pinned TUI has no --variant flag; in-TUI picker remains the interactive path)
- [x] #5 sch task --variant is rejected unless the workspace harness is opencode (explicit flag and resolved binding, fail-fast like --handoff)
- [x] #6 Specs updated (R8a deliberate-no-flag clause removed); full suites + verify-docs pass
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. CLI: validate_variant_or_die + --variant parsing in run/task commands, USAGE_TEXT, payload builders. 2. Shim task action: variant validation/echo/status/heartbeat/terminal/info; worker precedence (explicit variant wins; stored variant only when no explicit model). 3. Shim prepare-run: variant validation/echo/marker; sch-run-profile.sh autostart handling (opencode only). 4. Tests across CLI + shim. 5. Specs/guides update. 6. Verify suites + verify-docs.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Precedence: explicit variant wins; stored variant applies only when no explicit model; explicit model drops stored variant and skips the session lookup. sch run --variant rejected (pinned TUI has no such flag — verified against opencode 1.18.30 tui.ts builder + local binary). task --variant gated to opencode (explicit flag pre-resolution, binding post-resolution). Verified: 428 image/app OK, 586 cli/tests OK, verify-docs PASSED.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Added explicit --variant to sch task (opencode-only): CLI validation/parsing/gates/payload/ack-warning, shim validation/echo/status/heartbeat/terminal/info mirroring --model, worker precedence, R8b + R8/R10/I7 spec updates and guide updates. sch run --variant is rejected with remedy. Verified with full unit suites and docs checks.
<!-- SECTION:FINAL_SUMMARY:END -->
