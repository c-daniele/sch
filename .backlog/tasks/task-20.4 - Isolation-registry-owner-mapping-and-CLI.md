---
id: TASK-20.4
title: 'Isolation: registry owner mapping and CLI'
status: To Do
assignee: []
created_date: '2026-09-27 14:50'
labels:
  - security
dependencies:
  - TASK-20.3
parent_task_id: TASK-20
priority: high
type: feature
ordinal: 18000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Slice 4 of TASK-20. Registry maps callers by bound identity, refuses unlisted callers, returns plane fields; the CLI uses the owner runtime and reads checkpoints through the access role. Context, decisions, defaults, lessons and constraints live in the parent TASK-20; read it first and follow it. Headless run: apply the defaults, record every assumption in the implementation notes, and stop if the dependency is not Done. Work on `feat/task-20`, commit at the end, never push.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 The registry maps callers by bound identity: an unlisted caller gets HTTP 403 naming the identity to add and no record is created, two Identity Center users of one permission set are different owners, a new session name keeps the owner; unit tests cover each case
- [ ] #2 With isolation off the registry keeps the current owner derivation and responses
- [ ] #3 Registry responses carry the plane fields, and every sch command uses the owner runtime with no fallback to the shared runtime
- [ ] #4 Owner checkpoint reads (sch status, sch list --remote-check, sch dashboard) go through the access role with credential refresh; sch dashboard keeps working past expiry
- [ ] #5 No sch command creates or updates a runtime, IAM role or resource policy; registry-off sch status output is unchanged
- [ ] #6 Every AWS request the new code builds passes botocore ParamValidator in tests
<!-- AC:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [ ] #1 Relevant suites pass (cli, infra, tunnel, image-side tests with sandboxed paths) and bin/verify-docs.sh when docs changed
- [ ] #2 Implementation notes list every assumption and the verification results; a journal entry is created and MASTERPLAN.md updated as AGENTS.md requires
- [ ] #3 Work committed on feat/task-20 with conventional commits, never pushed
<!-- DOD:END -->
