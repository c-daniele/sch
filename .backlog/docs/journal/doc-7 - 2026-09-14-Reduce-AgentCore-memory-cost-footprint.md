---
id: doc-7
title: 2026-09-14 Reduce AgentCore memory-cost footprint
type: other
created_date: '2026-09-14 20:01'
updated_date: '2026-09-14 20:02'
tags:
  - journal
---
# 2026-09-14 Reduce AgentCore memory-cost footprint (TASK-1)

## Problem

AgentCore microVMs bill memory on the peak consumed up to each second
(128 MB minimum) from boot until termination, so one transient spike
prices the whole session. Operator measurement over ~10 days: memory was
~78% of sch cost with peaks around 7 GB on an ~8 GB box.

## What changed

Four child tasks, all completed and verified locally in this session
(the prior session left a complete WIP snapshot on
`feat/memory_optimization`; this session verified it, filled the gaps,
and finalized):

- Attribution (TASK-1.1): `bin/mem-trace.sh`, a second-granularity memory
  sampler (wrap + observe modes), plus `docs/history/memory-peak-attribution.md`
  with the billing model, ranked suspects, local floor evidence
  (~1.4-1.5 GB idle: opencode ~750 MB, shim ~150 MB, mcp-proxy ~100 MB,
  aws-docs MCP ~65 MB), and a live re-run checklist for the operator.
- Peak caps (TASK-1.2): Node heap cap (`SCH_NODE_HEAP_MB`, default 1792)
  and build parallelism (`SCH_BUILD_JOBS`, default 2, fanned out to
  MAKEFLAGS/CMake/Cargo and restated for repo runners) on every harness
  launch path — interactive wrapper, headless env, serve path, and the
  post-restore rebuild. Deploy-time defaults are `NodeHeapMb`/`BuildJobs`
  stack parameters; per-workspace plain-env overrides need no redeploy.
  Out-of-memory kills fail loudly naming both knobs instead of a bare
  non-zero exit.
- Checkpoint shrink (TASK-1.3): regenerable dirs (`node_modules`, `.venv`,
  build outputs, caches) excluded from the repo tarball and the repo
  fingerprint by name at any depth; the env is rebuilt best-effort after
  an L2 restore and never fails closed. Local evidence: representative
  tar 791235 B down to 221 B; fingerprint stable under regenerable churn.
- Lifecycle guardrails (TASK-1.4): recycle-after-peak guidance
  (`sch stop` + fresh session on stable checkpoint identity), steady-state
  audit notes, and a proposed CloudWatch alarm on `MemoryUsed-GBHours`
  in `docs/deploy.md`.
- Specs: checkpointing R21, runtime-image R47 (+I9), runtime-provisioning
  R17. Tests: `image/app/test_memory_footprint.py` (29 tests),
  `infra/test_memory_caps.py` (7 tests, new — default agreement across
  template, deploy.sh, image ENV, wrapper, and shim).

## Outcome

Suites green: cli 586 tests, infra 127 tests, image/app all green except
three failures that reproduce identically on the base release (pre-existing
environment issues, not regressions). `bin/verify-docs.sh` passes and
`bin/mem-trace.sh` was verified functionally (wrap/observe, exit-code
propagation, usage errors). Also removed a stray empty `dummy.txt` left
over from an earlier commit.

## Lesson

Live billed-peak confirmation (vended 1-second logs, `MemoryUsed-GBHours`,
fresh-workspace bootstrap, full `verify-l2.sh` cycle, alarm threshold)
cannot be produced inside a container without AWS — the task plan scoped
those as operator follow-ups from the start, and each child summary names
them explicitly. Local mechanism evidence (caps applied on all paths,
tar/fingerprint behavior, unit tests) is what this environment can verify,
and it is recorded as such rather than claimed as billed-cost proof.

## Follow-ups (operator, need a billed session)

Live re-run per the attribution checklist, caps-on peak comparison, fresh
bootstrap confirmation, full `verify-l2.sh` loss-restore cycle, idle-tail
GB-hours measurement, and alarm threshold validation against one billed week.
