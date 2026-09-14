---
id: TASK-1
title: Reduce AgentCore memory-cost footprint (peak-memory billing)
status: To Do
assignee: []
created_date: '2026-09-14 08:41'
labels: []
dependencies: []
ordinal: 1000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
AgentCore microVMs bill memory on peak-consumed-up-to-that-second per second (128 MB minimum) from boot until termination, so one transient spike poisons the whole session. Operator measurement shows memory at ~78% of sch cost with peaks around 7 GB. Goal: attribute the peak, cap transient spikes, shrink steady-state and checkpoint-amplified cost, and add lifecycle guardrails. Phased child tasks carry the work; this parent tracks the outcome.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Peak-memory phase attributed with second-granularity evidence for a representative workload
- [ ] #2 Same workload re-run peaks lower with caps on, without breaking bootstrap
- [ ] #3 Checkpoint artifacts exclude regenerable dirs with restore verified green
- [ ] #4 Idle/lifecycle guidance plus cost alarm documented
<!-- AC:END -->
