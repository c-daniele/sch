---
id: TASK-20.3
title: 'Isolation: per-principal plane stacks and deploy'
status: To Do
assignee: []
created_date: '2026-09-27 14:50'
labels:
  - security
dependencies:
  - TASK-20.2
parent_task_id: TASK-20
priority: high
type: feature
ordinal: 17000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Slice 3 of TASK-20. The plane template, runtime-template changes (shared managed policies, bucket policy, shared-runtime lock, least-privilege registry role), infra/deploy.sh (switch, entry resolution, quota preflight, per-principal stacks, image and configuration updates, orphan deletion), sch destroy, template tests and read-only Access Analyzer and simulator scripts. Context, decisions, defaults, lessons and constraints live in the parent TASK-20; read it first and follow it. Headless run: apply the defaults, record every assumption in the implementation notes, and stop if the dependency is not Done. Work on `feat/task-20`, commit at the end, never push.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 With the allow-list unset, a deploy creates no new resource; the default-rendering tests pass
- [ ] #2 With the allow-list set, infra/deploy.sh resolves every entry to its bound identity, refuses unknown or ambiguous entries before changing any stack, warns on unverified sso usernames, checks the AgentCore runtime quota, and keeps exactly one plane stack per listed principal (created, updated on every deploy, deleted when the entry is removed)
- [ ] #3 Runtime and DEFAULT endpoint locks are AWS::BedrockAgentCore::ResourcePolicy resources in the plane stack; the shared runtime denies user data-plane actions when isolation is on
- [ ] #4 The registry role keeps only its table, checkpoint purge and StopRuntimeSession permissions, and no role used at request time can create or change IAM, AgentCore runtimes or resource policies
- [ ] #5 Every new or changed policy document validates with IAM Access Analyzer without ERROR findings, and a committed read-only script reproduces the check
- [ ] #6 Committed simulator script and redacted output show that an owner execution role reads and writes only its own storage (including through ReadOnlyAccess), still uploads build sources and writes to the data bucket, and is denied other owners storage, the registry table, the Telegram tables and other SCH runtime log groups
- [ ] #7 Simulator evidence shows each user runtime and endpoint policy denies every data-plane action to another IAM user, another Identity Center user of the same permission set and an unlisted role session, all holding bedrock-agentcore:* on *, and allows the owner (IAM user and Identity Center cases); the simulator limits are recorded next to the evidence
- [ ] #8 User runtimes receive image and configuration changes during deploy only, and sch destroy removes every plane stack before the runtime stack
<!-- AC:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [ ] #1 Relevant suites pass (cli, infra, tunnel, image-side tests with sandboxed paths) and bin/verify-docs.sh when docs changed
- [ ] #2 Implementation notes list every assumption and the verification results; a journal entry is created and MASTERPLAN.md updated as AGENTS.md requires
- [ ] #3 Work committed on feat/task-20 with conventional commits, never pushed
<!-- DOD:END -->
