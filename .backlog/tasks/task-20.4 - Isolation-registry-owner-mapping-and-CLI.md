---
id: TASK-20.4
title: 'Isolation: registry owner mapping and CLI'
status: Done
assignee: []
created_date: '2026-09-27 14:50'
updated_date: '2026-09-27 17:11'
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
- [x] #1 The registry maps callers by bound identity: an unlisted caller gets HTTP 403 naming the identity to add and no record is created, two Identity Center users of one permission set are different owners, a new session name keeps the owner; unit tests cover each case
- [x] #2 With isolation off the registry keeps the current owner derivation and responses
- [x] #3 Registry responses carry the plane fields, and every sch command uses the owner runtime with no fallback to the shared runtime
- [x] #4 Owner checkpoint reads (sch status, sch list --remote-check, sch dashboard) go through the access role with credential refresh; sch dashboard keeps working past expiry
- [x] #5 No sch command creates or updates a runtime, IAM role or resource policy; registry-off sch status output is unchanged
- [x] #6 Every AWS request the new code builds passes botocore ParamValidator in tests
<!-- AC:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [x] #1 Relevant suites pass (cli, infra, tunnel, image-side tests with sandboxed paths) and bin/verify-docs.sh when docs changed
- [x] #2 Implementation notes list every assumption and the verification results; a journal entry is created and MASTERPLAN.md updated as AGENTS.md requires
- [x] #3 Work committed on feat/task-20 with conventional commits, never pushed
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

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Assumptions (headless run, defaults of TASK-20 applied):
- Deployment account (R36) = account of the registry function's own ARN (Lambda context), falling back to requestContext.accountId; a caller of another account gets 403.
- Every successful isolated registry response carries the caller's plane at top level as well as per record (R38 extended), so an owner with no records, and `sch info`, learn the plane. Spec R38 updated.
- A command that needs the runtime before any registry response asks the registry first (GET /workspaces); in practice only `sch info`. Registry failure there is fatal (no fallback). Spec R40 updated.
- The registry purge with isolation also removes builds/<ownerPrefix>/<identity>/ (the sch-build-image scope is the workspace identity from the mount marker). Isolation-off purge unchanged.
- The tunnel helpers (attach, web, acp, sync, bundle) receive the plane runtime ARN through argv; their WebSocket frames are not invoke payloads and carry no owner_prefix (R30 accepts a payload without it; the runtime environment stays authoritative).
- Access-role sessions: `aws sts assume-role`, session name sch-cli-<ownerKey>, 3600 s, renewed 300 s before expiry, credentials in memory only and passed to `aws s3api` through the environment (AWS_PROFILE and other credential variables removed from that environment).
- `sch list --remote-check` ignores children of checkpoints/ containing '.', i.e. owner trees; they are never registry-off workspaces (names cannot contain '.'). Visible only on a stack that once had isolation on.
- `sch info` prints an extra `isolation` line only with isolation on.
- deletion.py (local purge) is only used in registry-off mode, which cannot be isolated, so it is unchanged.

Found and fixed on the way: the registry rejected every existing record read back from DynamoDB (400 "workspace has invalid session epoch metadata") because the boto3 resource returns numbers as Decimal and _epoch_value accepted only int. Found by the validating DynamoDB test double; not verified live. Integral Decimals are now accepted; test added.

Verification:
- infra suite 215 OK (new test_workspace_registry_isolation: 20 tests; a real boto3 Table with client-side botocore validation backs an in-memory table; SSM, S3 and AgentCore fakes run ParamValidator on every request).
- cli suite 626 OK (new test_isolation_cli: 26 tests; every aws argv built by the new code is mapped back to API params and checked with ParamValidator: sts assume-role, s3api get-object/head-object/list-objects-v2, bedrock-agentcore invoke-agent-runtime, bedrock-agentcore-control get-agent-runtime). Golden test for registry-off `sch status` output and request; grep test that cli/sch contains no create/update of runtimes, roles, resource policies or parameters.
- tunnel npm test pass; image-side 534 tests with sandboxed paths: only the 7 known TASK-22 failures, live-session files unchanged (md5 before/after).
- bin/verify-docs.sh passes.
- Not verified: live registry behavior (principal ID field, 403 text) and the two-principal check; operator-side under TASK-20.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Registry and CLI slice of TASK-20. infra/workspace_registry_handler.py: with ISOLATION_ENABLED=true the owner is derived from the principal ID (user:<id>; sso:<AROA:name> then role:<AROA>), looked up in /<p>/<e>/planes/<ownerKey> (60 s cache of hits and misses), the caller account must equal the function's account; unlisted callers get 403 naming the entry to add before any table access; records store ownerPrefix; responses carry isolation and plane (top level and per record); delete and bulk delete stop sessions on the plane runtime and purge the owner-segment keys incl. builds. Isolation-off path unchanged. Also fixed a latent bug: DynamoDB Decimal epochs were rejected on read-back. CLI: new cli/sch/plane.py (strict R41 validation, adopt from every registry response, discovery via GET /workspaces when needed, access-role sessions in memory renewed 5 min before expiry); runtime_arn uses the plane with no fallback; invoke payloads carry owner_prefix; status, list --remote-check and dashboard read owner-segment keys through the access role; sch info shows the plane. Specs updated (per-principal-isolation R36/R38/R40, workspace-registry R4, iam-workspace-control-api R2 and error contract, iam-workspace-registry R4, owner-scoped-workspace-storage R2/R4) and stale guide lines corrected. Verified: infra 215 OK, cli 626 OK (ParamValidator on every new AWS request, registry-off golden status test, no-provisioning grep test), tunnel pass, image-side only the 7 known TASK-22 failures, verify-docs pass. Live checks stay operator-side (TASK-20).
<!-- SECTION:FINAL_SUMMARY:END -->
