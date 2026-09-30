---
id: doc-17
title: '2026-09-27 Isolation: owner-scoped storage and shim'
type: other
created_date: '2026-09-27 16:06'
updated_date: '2026-09-27 16:07'
tags:
  - journal
---
# 2026-09-27 Isolation: owner-scoped storage and shim

Task: TASK-20.2 (slice 2 of TASK-20). Spec: [per-principal-isolation](../../../docs/specs/security/per-principal-isolation.md) R26–R35. Decision: decision-14.

## Problem

Per-principal isolation gives each listed user their own runtime, and a bucket policy will confine that runtime's role to the user's own storage trees. For that to work, the runtime itself has to write under the user's owner segment (`o.<ownerKey>`). It must also never take that segment from anything a caller controls. Before this slice the shim had no notion of an owner. It logged every task prompt. And an invocation that arrived during the boot restore could write into the still-empty workspace root. Such a write makes the restore believe the workspace already has content, so it skips restoring the checkpoint.

## What changed

- A plane runtime receives `SCH_OWNER_PREFIX` from its deploy-time environment, and that is the only source of the owner segment. A malformed value makes every invocation and the boot fail before any storage access. An invocation that claims a different owner is rejected before it has any effect. Only registry workspace identities are accepted.
- Checkpoints, generations, writer claims, task status and image build sources now live under the owner segment on plane runtimes. A restored manifest that points outside the owner's trees is refused. Runtimes without the variable produce exactly the same keys as before.
- On plane runtimes, an early `sch stop` checkpoint waits for the restore, and if the restore is still running it answers "skipped, not ready" instead of writing. The GitHub remote setup also waits and runs once at the end of the boot. Tests hold the restore open for the s3 and session backends, send eleven kinds of invocation at the same time, and check that the root stays empty until the restore finishes.
- Task prompts no longer appear in runtime logs, in any mode. Besides the headless command line, a second leak was found: an invocation without an action was logged under its prompt text.
- The task watchdog now finds tasks in both layouts.

## Outcome

All suites pass. The image-side suite shows only the seven known out-of-container failures (TASK-22). Nothing is deployed yet: the plane stacks come with TASK-20.3, and the CLI that sends the owner prefix comes with TASK-20.4.

## Lesson

The "no early writes" audit turned up the same defect on ordinary runtimes: an `sch stop` that arrives during a boot restore can write into the root there as well. It was left unchanged, because this slice must not alter isolation-off behavior, and it is tracked as TASK-23. A test that holds the boot inside the restore and sends real invocations at it found the problem in minutes. Reading the code had not.
