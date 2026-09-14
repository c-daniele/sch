---
id: TASK-1.2
title: Cap transient build and harness memory peaks
status: To Do
assignee: []
created_date: '2026-09-14 08:41'
labels: []
dependencies: []
parent_task_id: TASK-1
ordinal: 3000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Bound the spikes that poison peak billing: Node heap cap (NODE_OPTIONS max-old-space-size) on harness and build argv, plus default build parallelism limits (MAKEFLAGS, npm/uv concurrency, tsc/test workers). Provide a documented per-workspace escape hatch. Trade-off: caps convert expensive-success into cheap-OOM, so OOM must fail loudly with remediation.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Same workload as attribution peaks lower with caps on
- [ ] #2 Bootstrap of a fresh workspace still succeeds (Node plus Python)
- [ ] #3 OOM escape hatch documented and verified
<!-- AC:END -->
