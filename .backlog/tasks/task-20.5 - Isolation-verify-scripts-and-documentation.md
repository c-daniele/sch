---
id: TASK-20.5
title: 'Isolation: verify scripts and documentation'
status: To Do
assignee: []
created_date: '2026-09-27 14:50'
updated_date: '2026-09-27 15:20'
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

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
Spec: docs/specs/security/per-principal-isolation.md (whole spec, especially Threat model and Residual risks). Decision: decision-14. Check first that TASK-20.4 is Done.

1. Audit bin/verify-*.sh for assumptions that break on an isolation stack: the shared runtime ARN from stack outputs (use the registry plane instead), flat checkpoint keys (use the owner segment and the access role), Telegram checks (skip with a clear message when isolation is on). Each script detects isolation from the registry response and adapts; registry-off behavior stays unchanged.
2. New bin/verify-isolation.sh (operator-side, two AWS profiles A and B that are listed, one unlisted profile C, optional Identity Center profile): A creates a workspace and starts a task; B tries to invoke and stop A's session with A's runtime ARN and session ID and must get AccessDenied; B reads A's task-status and manifest keys directly and through A's access role and must be denied; A's agent (sch task on A's workspace) tries to read B's owner tree and the registry table and must be denied; A's own status/list/dashboard reads work; C's resolve gets HTTP 403 naming the identity to add. The script is read-only apart from its own test workspaces, which it deletes at the end. No account IDs in output.
3. Guides: docs/workspaces.md (enabling, entries, owner mapping, no migration), docs/deploy.md (ISOLATED_PRINCIPALS, deploy flow, adding and removing principals, retained storage and purge, runtime quota, sch destroy), docs/security.md and SECURITY.md (what isolation guarantees, boundary administrators, residual risks X1-X9, Telegram refused, ReadOnlyAccess behavior), docs/getting-started.md (caller permissions: execute-api:Invoke, bedrock-agentcore data-plane on the own plane, sts:AssumeRole on the own access role; deploy-principal permissions for plane stacks). Claim only what tests and simulator output prove; propose examples to the maintainer as AGENTS.md requires instead of inserting them unasked (headless: record them in the task notes).
4. Flip per-principal-isolation.md to "Partially verified" (live check open) and update docs/specs/README.md.
5. Check TASK-20 AC #1 once all subtasks are Done; leave AC #2 (live check) open and TASK-20 In Progress.
6. Suites, bin/verify-docs.sh, journal, masterplan, commit on feat/task-20.
<!-- SECTION:PLAN:END -->
