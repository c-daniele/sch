---
id: TASK-1.4
title: 'Lifecycle guardrails: idle timeout, recycle-after-peak, cost alarm'
status: In Progress
assignee: []
created_date: '2026-09-14 08:42'
updated_date: '2026-09-14 19:02'
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
- [ ] #1 Idle-after-peak GB-hours measured before and after timeout guidance
- [ ] #2 Recycle-after-peak flow documented with checkpoint-safety note
- [ ] #3 Cost alarm definition proposed with threshold and target doc file
<!-- AC:END -->
