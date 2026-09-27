---
id: TASK-20.4
title: 'Isolation: registry owner mapping and CLI'
status: To Do
assignee: []
created_date: '2026-09-27 14:50'
updated_date: '2026-09-27 15:20'
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

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
Spec: docs/specs/security/per-principal-isolation.md (R7-R9, R36-R43). Decision: decision-14. Check first that TASK-20.3 is Done.

1. Registry handler (infra/workspace_registry_handler.py).
   - ISOLATION_ENABLED unset or false: current code path untouched (R9); keep the existing tests green unchanged.
   - Isolation on: read requestContext.identity.user and accountId (R36); build candidates (user:<id>; sso:<id> then role:<AROA prefix>); look up /<p>/<e>/planes/<ownerKey> with ssm get_parameter, cache hits and misses for at most 60 s; ownerId = sha256(owner string).
   - No plane: 403 with the suggested entry derived from userArn (R37), before any table access.
   - Records store ownerPrefix at creation; responses add "isolation": true and per-record plane {runtimeArn, accessRoleArn, ownerPrefix} (R38); delete stops the session on the plane runtimeArn and purges the owner-prefixed keys for the stored ownerPrefix; bulk delete likewise.
   - Unit tests: unlisted caller 403 and no put_item; two Identity Center users of one permission set are two owners; new session name of an IAM user and of a role: entry keeps the owner; account mismatch refused; cache expiry; purge prefixes; botocore ParamValidator on every ssm, s3, dynamodb and bedrock-agentcore request.
2. CLI registry client (cli/sch/workspace_registry.py): parse and validate isolation and plane strictly (R41); RegistryWorkspace carries the plane.
3. Runtime selection (cli/sch/config.py, cli/sch/runtime.py, cli/sch/harness.py and every command using runtime_arn()): with a plane, use plane.runtimeArn everywhere (invoke, stop, attach, web, acp, task, shell, version pin R15d via get-agent-runtime on the plane ARN); never read SCH_RUNTIME_ARN or the runtime-arn cache for an isolated workspace (R40). Send owner_prefix in every invoke payload.
4. Access-role reads: a small helper (stdlib, aws CLI subprocess) that runs sts assume-role on plane.accessRoleArn, keeps the credentials in memory only, passes them to aws s3api subprocesses through the environment, and refreshes them five minutes before expiry. Use it in cli/sch/commands/status.py (task-status key with owner segment), cli/sch/commands/list.py --remote-check (writer claims and checkpoint listing under workspace-writers/o.<k>/ and checkpoints/o.<k>/), cli/sch/dashboard.py (manifest; refresh inside the long-running loop; fix any dashboard read that assumes the flat layout), cli/sch/deletion.py prefix list when it is used with isolation.
5. Invariants: no sch command creates or updates a runtime, role or resource policy (grep test over cli/sch for create-/update-/put-resource-policy calls); registry-off sch status output unchanged (golden test).
6. Update the specs touched (planned-change notes in iam-workspace-control-api, workspace-registry, iam-workspace-registry, owner-scoped-workspace-storage become requirement text). Suites, bin/verify-docs.sh, journal, masterplan, commit on feat/task-20.
<!-- SECTION:PLAN:END -->
