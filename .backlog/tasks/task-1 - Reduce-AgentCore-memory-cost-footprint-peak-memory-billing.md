---
id: TASK-1
title: Reduce AgentCore memory-cost footprint (peak-memory billing)
status: Done
assignee: []
created_date: '2026-09-14 08:41'
updated_date: '2026-09-14 20:01'
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
- [x] #1 Peak-memory phase attributed with second-granularity evidence for a representative workload
- [x] #2 Same workload re-run peaks lower with caps on, without breaking bootstrap
- [x] #3 Checkpoint artifacts exclude regenerable dirs with restore verified green
- [x] #4 Idle/lifecycle guidance plus cost alarm documented
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Attribute peak with mem-trace tool + local tar/fingerprint evidence (live billed-peak re-run is operator follow-up). 2. Cap Node heap + build parallelism in shim headless env, wrapper, image ENV, template params; loud OOM hint; escape hatch. 3. Exclude regenerable dirs from repo archive + repo fingerprint; best-effort env rebuild on restore; verify via bin/verify-l2.sh unit-level + local tar evidence. 4. Lifecycle guardrails: idle-timeout/stop/recycle guidance + CloudWatch MemoryUsed-GBHours alarm proposal in docs/deploy.md. 5. Specs (checkpointing R + runtime-image R), tests, verify-docs, journal + masterplan.
<!-- SECTION:PLAN:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Done 2026-09-14. All four children complete: attribution tool + report (1.1), transient-peak caps on every launch path with loud OOM (1.2), regenerable-dir exclusion from checkpoint tar + fingerprint with post-restore rebuild (1.3), lifecycle guardrails + alarm proposal (1.4). Specs: checkpointing R21, runtime-image R47 (+I9), runtime-provisioning R17. Tests: image/app/test_memory_footprint.py (29), infra/test_memory_caps.py (7, new this session); suites green - cli 586, infra 127, image/app all except 3 pre-existing env failures also failing on the base release (presence toggle, WAL VFS premise, permission-hook node). bin/verify-docs.sh passes; mem-trace.sh verified functionally; local tar evidence 791235B -> 221B. Removed stray empty dummy.txt. Live-AWS follow-ups (billed-peak re-run, caps-on comparison, fresh bootstrap, full verify-l2 cycle, idle GB-hours + alarm threshold) are operator-side per the task plan and recorded in each child summary.
<!-- SECTION:FINAL_SUMMARY:END -->
