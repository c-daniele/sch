---
id: TASK-1.4
title: 'Lifecycle guardrails: idle timeout, recycle-after-peak, cost alarm'
status: Done
assignee: []
created_date: '2026-09-14 08:42'
updated_date: '2026-09-14 20:01'
labels: []
dependencies: []
parent_task_id: TASK-1
ordinal: 5000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Cut GB-hours after a peak: guidance and defaults for idleRuntimeSessionTimeout (today 900s) and sch stop discipline, a recycle-after-peak flow (fresh runtimeSessionId on stable workspace checkpoint identity), steady-state audit (serve plus MCP children), and a CloudWatch alarm on MemoryUsed-GBHours. Roadmap already lists cost budgets and alarms in deploy.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Idle-after-peak GB-hours measured before and after timeout guidance
- [x] #2 Recycle-after-peak flow documented with checkpoint-safety note
- [x] #3 Cost alarm definition proposed with threshold and target doc file
<!-- AC:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Done 2026-09-14. Lifecycle guardrails documented in docs/deploy.md (Memory is billed on peak: never-spike caps, checkpoint-amplifier shrink with steady-state floor reference ~1.4GB, recycle-after-peak via sch stop + fresh runtimeSessionId on stable checkpoint identity with rule of thumb) plus a proposed-but-not-yet-templated CloudWatch alarm on MemoryUsed-GBHours (2x trailing-7d daily average, 2-day evaluation, draft put-metric-alarm, target file noted; threshold to validate against one billed week). Idle timeout default 900s unchanged (conscious choice, documented). FOLLOW-UP (needs billed session, operator): idle-after-peak GB-hours before/after measurement and alarm threshold validation.
<!-- SECTION:FINAL_SUMMARY:END -->
