---
id: TASK-20.5
title: 'Isolation: verify scripts and documentation'
status: To Do
assignee: []
created_date: '2026-09-27 14:50'
labels:
  - security
dependencies:
  - TASK-20.4
parent_task_id: TASK-20
priority: high
type: docs
ordinal: 19000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Slice 5 of TASK-20. Make bin/verify-*.sh usable on isolation stacks, add bin/verify-isolation.sh, update the guides and SECURITY.md, then set the parent In Progress with only the live check open. Context, decisions, defaults, lessons and constraints live in the parent TASK-20; read it first and follow it. Headless run: apply the defaults, record every assumption in the implementation notes, and stop if the dependency is not Done. Work on `feat/task-20`, commit at the end, never push.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 The bin/verify-*.sh scripts run against an isolation-enabled stack as a listed user
- [ ] #2 bin/verify-isolation.sh checks two principals end to end: join denied even with a known session ID, cross-owner checkpoint reads denied from the CLI and from inside the agent, own workflow works, unlisted caller refused
- [ ] #3 docs/workspaces.md, docs/deploy.md, docs/security.md and SECURITY.md explain enabling the feature, adding and removing principals, caller permissions, Telegram and ReadOnlyAccess behavior, the AgentCore runtime quota and the residual risks, and claim only what the tests and the simulator prove
- [ ] #4 Parent TASK-20 has its first criterion checked and stays In Progress with the live-check criterion open
<!-- AC:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [ ] #1 Relevant suites pass (cli, infra, tunnel, image-side tests with sandboxed paths) and bin/verify-docs.sh when docs changed
- [ ] #2 Implementation notes list every assumption and the verification results; a journal entry is created and MASTERPLAN.md updated as AGENTS.md requires
- [ ] #3 Work committed on feat/task-20 with conventional commits, never pushed
<!-- DOD:END -->
