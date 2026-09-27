---
id: TASK-20.1
title: 'Isolation design: decision record and normative spec'
status: To Do
assignee: []
created_date: '2026-09-27 14:50'
labels:
  - security
dependencies: []
parent_task_id: TASK-20
priority: high
type: docs
ordinal: 15000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Slice 1 of TASK-20. Write the design that the other subtasks implement: bound identities per entry form, the per-principal plane stack, the storage layout, the resource-policy lock, the execution and access roles, the bucket policy, the threat model and the residual risks. Context, decisions, defaults, lessons and constraints live in the parent TASK-20; read it first and follow it. Headless run: apply the defaults, record every assumption in the implementation notes, and stop if the dependency is not Done. Work on `feat/task-20`, commit at the end, never push.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 A decision record (backlog decision create) records the final defaults of TASK-20, including one stack per principal and unverified sso usernames, with the reasons
- [ ] #2 A normative spec under docs/specs/security/ describes the bound identities, the deploy-time planes, the storage layout, the threat model (who administers the boundary) and the residual risks (ReadOnlyAccess configuration reads, plane creation window, unverified sso usernames, simulator limits)
- [ ] #3 docs/specs/README.md and the existing security specs link the new spec, and statements they make that the design changes are marked or corrected
- [ ] #4 The implementation plans of TASK-20.2 to TASK-20.5 are written in those tasks, consistent with the spec
<!-- AC:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [ ] #1 Relevant suites pass (cli, infra, tunnel, image-side tests with sandboxed paths) and bin/verify-docs.sh when docs changed
- [ ] #2 Implementation notes list every assumption and the verification results; a journal entry is created and MASTERPLAN.md updated as AGENTS.md requires
- [ ] #3 Work committed on feat/task-20 with conventional commits, never pushed
<!-- DOD:END -->
