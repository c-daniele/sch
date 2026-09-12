---
id: decision-8
title: >-
  Resolve deferred session continuation after the workspace-ready gate and
  persist the outcome in task status
date: '2026-09-06 07:11'
status: accepted
---
## Context

On a cold microVM the shim restores the harness session store (`opencode.db`, Claude JSONL transcripts, Pi session files) asynchronously after the first invocation, while `sch task` must return a `task_id` immediately (headless-task-execution R1). `sch task --continue` resolved the session to resume at worker start, so a submit right after `sch stop` looked into an empty store, degraded to a fresh session as the spec allowed, and left no trace beyond a shim log line (2026-09-05 incident, TASK-27). Two placements were possible for the resolution: at accept time, where the acknowledgement can report it, or after the workspace-ready wait, where the store is guaranteed to be present but the acknowledgement has already been sent.

## Decision

Resolve the continuation after the workspace-ready gate, never at accept time on a cold boot, and treat the outcome as data the operator reads later rather than as part of the acknowledgement. The persisted task status carries `continue_requested` (present iff requested) and `continue_resolved` (`true`/`false` once the worker resolved, absent before), plus the resolved `harness_session_id`; `sch status` renders the three states in words. The acknowledgement echoes only the request (`continue: true`), and the CLI uses the missing echo to warn about a runtime image predating the feature, the same contract as `--model` and `sch run --continue`. An explicit handoff hint still wins without a store lookup, and a miss still degrades to a fresh session without failing the task.

## Consequences

`sch stop` followed by `sch task --continue` resumes the pre-stop session regardless of how long the restore takes (verified live with the restore still in flight after the acknowledgement). Acceptance criteria and verification scripts must assert on `sch status` and the persisted object, not on the submit output, for anything the worker decides after the gate; `bin/verify-headless-tasks.sh` step 7b exists because a warmed-and-waited check never exercises the race. The same principle applies wherever a synchronous handler consults the restored store before readiness: `sch run --continue` (prepare-run) did so and was fixed in TASK-29 with the synchronous variant of the rule. There the operator is waiting for the TUI, so the handler itself waits for the gate (bounded, with the CLI read timeout raised above the bound) and the response can carry the outcome; a workspace still not ready after the bound is an explicit error rather than a fresh TUI.
