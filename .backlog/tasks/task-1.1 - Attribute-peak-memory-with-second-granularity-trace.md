---
id: TASK-1.1
title: Attribute peak memory with second-granularity trace
status: In Progress
assignee: []
created_date: '2026-09-14 08:41'
updated_date: '2026-09-14 19:02'
labels: []
dependencies: []
parent_task_id: TASK-1
ordinal: 2000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Harness is opencode (operator-confirmed 2026-09-14). Exact historical task unknown, but workload class is sch self-improvement: coding, analysis, tool usage, tests on this repo. Trace a representative instance of that class: fresh opencode workspace on this repo, run the kind of work the agent does (explore plus edit plus the repo test suites), and correlate AgentCore session-level 1-second vended logs and MemoryUsed-GBHours against shim task-status heartbeats and checkpoint last_sizes via the info action. Output: which phase spiked (env bootstrap/build vs harness/MCP vs checkpoint tar/gzip) with evidence.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Trace links wall-clock spike to a phase with log excerpts and timestamps
- [ ] #2 Report records harness, workload, checkpoint sizes, and idle tail GB-hours
<!-- AC:END -->
