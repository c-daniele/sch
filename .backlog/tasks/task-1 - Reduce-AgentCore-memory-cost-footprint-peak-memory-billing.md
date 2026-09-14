---
id: TASK-1
title: Reduce AgentCore memory-cost footprint (peak-memory billing)
status: In Progress
assignee: []
created_date: '2026-09-14 08:41'
updated_date: '2026-09-14 19:02'
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

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Attribute peak with mem-trace tool + local tar/fingerprint evidence (live billed-peak re-run is operator follow-up). 2. Cap Node heap + build parallelism in shim headless env, wrapper, image ENV, template params; loud OOM hint; escape hatch. 3. Exclude regenerable dirs from repo archive + repo fingerprint; best-effort env rebuild on restore; verify via bin/verify-l2.sh unit-level + local tar evidence. 4. Lifecycle guardrails: idle-timeout/stop/recycle guidance + CloudWatch MemoryUsed-GBHours alarm proposal in docs/deploy.md. 5. Specs (checkpointing R + runtime-image R), tests, verify-docs, journal + masterplan.
<!-- SECTION:PLAN:END -->
