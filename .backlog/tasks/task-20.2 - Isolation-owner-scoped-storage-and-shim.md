---
id: TASK-20.2
title: 'Isolation: owner-scoped storage and shim'
status: To Do
assignee: []
created_date: '2026-09-27 14:50'
labels:
  - security
dependencies:
  - TASK-20.1
parent_task_id: TASK-20
priority: high
type: feature
ordinal: 16000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Slice 2 of TASK-20. Image-side changes: owner prefix from the runtime environment, per-owner layout, no early writes, prompts out of the logs, per-owner sch-build-image key, watchdog listing of the new layout. Context, decisions, defaults, lessons and constraints live in the parent TASK-20; read it first and follow it. Headless run: apply the defaults, record every assumption in the implementation notes, and stop if the dependency is not Done. Work on `feat/task-20`, commit at the end, never push.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 The shim takes the owner prefix only from the runtime environment and rejects a payload that disagrees with it
- [ ] #2 Checkpoints, generations, writer claims, task status and build sources use the per-owner layout of the spec; the registry-off layout is unchanged
- [ ] #3 Nothing is written into the workspace root before the bootstrap restore completes; tests cover s3 and session restores with concurrent invokes
- [ ] #4 Task prompts no longer appear in runtime logs
- [ ] #5 sch-build-image uploads build sources under the owner prefix when it is set, and the task watchdog lists both layouts
- [ ] #6 Runtimes without the owner variable behave exactly as before; existing image-side and infra tests pass
<!-- AC:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [ ] #1 Relevant suites pass (cli, infra, tunnel, image-side tests with sandboxed paths) and bin/verify-docs.sh when docs changed
- [ ] #2 Implementation notes list every assumption and the verification results; a journal entry is created and MASTERPLAN.md updated as AGENTS.md requires
- [ ] #3 Work committed on feat/task-20 with conventional commits, never pushed
<!-- DOD:END -->
