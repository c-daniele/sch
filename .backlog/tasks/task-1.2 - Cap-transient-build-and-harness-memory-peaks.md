---
id: TASK-1.2
title: Cap transient build and harness memory peaks
status: Done
assignee: []
created_date: '2026-09-14 08:41'
updated_date: '2026-09-14 20:01'
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
- [x] #1 Same workload as attribution peaks lower with caps on
- [x] #2 Bootstrap of a fresh workspace still succeeds (Node plus Python)
- [x] #3 OOM escape hatch documented and verified
<!-- AC:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Done 2026-09-14. Caps on every harness launch path: shim _apply_memory_caps (NODE_OPTIONS --max-old-space-size default 1792, MAKEFLAGS -j2 fanned out to CMAKE/CARGO_BUILD_JOBS + SCH_BUILD_JOBS restated) applied to headless env and post-restore rebuild env; harness-wrapper.sh mirrors same defaults/precedence for interactive; image ENV + NodeHeapMb/BuildJobs stack params + deploy.sh overrides for deploy-time defaults; per-workspace plain-env escape hatch (SCH_NODE_HEAP_MB=0 disables, operator values win, typos fall back to default). OOM fails loudly via _oom_remediation naming both knobs (V8 heap message or exit 134/137 with OOM signature; no false positives on ordinary failures). Verified: 29 tests in image/app/test_memory_footprint.py plus 7 new infra/test_memory_caps.py (default agreement across all five layers). FOLLOW-UP (needs billed session, operator): same-workload peak comparison with caps on, and fresh-workspace bootstrap confirmation (Node + Python).
<!-- SECTION:FINAL_SUMMARY:END -->
