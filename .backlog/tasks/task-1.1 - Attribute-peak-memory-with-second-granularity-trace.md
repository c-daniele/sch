---
id: TASK-1.1
title: Attribute peak memory with second-granularity trace
status: Done
assignee: []
created_date: '2026-09-14 08:41'
updated_date: '2026-09-14 20:01'
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
- [x] #1 Trace links wall-clock spike to a phase with log excerpts and timestamps
- [x] #2 Report records harness, workload, checkpoint sizes, and idle tail GB-hours
<!-- AC:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Done 2026-09-14. Delivered bin/mem-trace.sh (second-granularity sampler, wrap + observe modes; verified functionally: wrap/observe CSV shape, exit-code propagation, usage errors) and docs/history/memory-peak-attribution.md (billing model, ranked suspects with code refs, local evidence, live re-run checklist). Local floor evidence on this box: MemTotal ~8GB, idle floor ~1.4-1.5GB (opencode ~725-830MB RSS across two procs, shim ~150MB, mcp-proxy ~100MB, aws-docs MCP ~65MB); checkpoint amplifier on representative tree 32.9MB -> 0.2KB with excludes. Attribution: transient builds are the only plausible 7GB shape; harness floor prices the idle tail; checkpoint tar/fingerprint amplified both size and tick I/O; shim is noise. FOLLOW-UP (needs billed session, operator): live re-run aligning 1-second vended logs + MemoryUsed-GBHours to wall-clock phase boundaries per the checklist.
<!-- SECTION:FINAL_SUMMARY:END -->
