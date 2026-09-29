---
id: TASK-24
title: Registry DELETE times out while AgentCore is still stopping the live microVM
status: To Do
assignee: []
created_date: '2026-09-29 09:05'
labels:
  - registry
  - reliability
dependencies: []
priority: medium
type: bug
ordinal: 21000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
## Problem

Deleting a workspace through the registry (`sch delete`, registry cleanup of `bin/verify-isolation.sh`) fails with a generic error when the workspace's session is still running. Seen live on 2026-09-29 (TASK-20 live check, twice): the registry Lambda ran into its 10 s timeout (`Timeout: 10` in `infra/agent_runtime.yaml`, REPORT `Status: timeout`) before purging anything, and the CLI client (`cli/sch/workspace_registry._request`, urllib timeout 10 s) reported `workspace registry request failed`. The slow step is `StopRuntimeSession` on a session whose microVM is alive; with the session already stopping, the same DELETE (stop + S3 purge + record delete) completes in 1-2 s. Not specific to isolation: the registry-off path calls the same `stop_runtime_session` on the shared runtime.

The resumable deletion of iam-workspace-registry R8 contained the damage: the record stayed in `deleting`, `sch list` hid it, and a retry finished the job. But the user sees a failure with no hint that a retry will succeed, and half-deleted records survive until someone retries.

## Proposed fix

- Raise the registry Lambda timeout (for example 60 s) and the client's read timeout for DELETE (longer than the Lambda's), or make the Lambda return 202 with `deletionState=deleting` when the stop has not finished within budget.
- `sch delete` reports "deletion in progress, retry to finish" on a timeout or 202 instead of a generic failure, and exits non-zero only when the retry hint does not apply.
- Consider a bounded retry inside the registry for `StopRuntimeSession` (ConflictException while stopping).
- Tests: registry handler with a slow stop; client with a 202 and with a timeout; `bin/verify-isolation.sh` cleanup already retries three times (TASK-20).

Evidence: TASK-20 implementation notes (2026-09-29) and journal doc-21.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 A delete of a workspace with a live session completes on the first sch delete call, or ends with an explicit 'deletion in progress, retry' outcome; no generic 'request failed'
- [ ] #2 The registry Lambda timeout and the client timeout are consistent (client waits longer than the Lambda) and documented in the spec iam-workspace-registry
- [ ] #3 Unit tests cover a slow StopRuntimeSession in the handler and the timeout/202 paths in the CLI client; a live delete of a running workspace passes
<!-- AC:END -->
