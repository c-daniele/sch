---
id: doc-20
title: '2026-09-27 Isolation: verify scripts and documentation'
type: other
created_date: '2026-09-27 20:47'
updated_date: '2026-09-27 20:48'
tags:
  - journal
---
# 2026-09-27 Isolation: verify scripts and documentation

Task: TASK-20.5 (slice 5 of TASK-20). Spec: [per-principal-isolation](../../../docs/specs/security/per-principal-isolation.md). Decision: decision-14.

## Problem

After TASK-20.4 an isolated stack worked through `sch`, but nothing could prove the boundary on a real deployment, and the operator tooling assumed a single shared runtime. The live `bin/verify-*.sh` scripts read the shared runtime ARN from the stack, invented local session IDs, sent the logical workspace name in payloads and read flat checkpoint keys with the caller's own credentials. On an isolated stack every one of those is refused. The guides still said isolation was "implemented but not documented", and `SECURITY.md` said it was "being built".

## What changed

- **One way to find the target.** A small helper beside the CLI (not a `sch` command) and a bash library that every live script sources tell a script which mode the stack is in. On registry-off stacks nothing changes. With the registry, a script resolves the workspace through it and uses the registry session and workspace identity. With isolation, it also uses the caller's plane runtime and reads owner checkpoints through the access role. The Telegram checks print `SKIP` on an isolated stack.
- **Scripts brought up to date.** Besides the plane changes: the runtime-IAM tuning check had been stale since TASK-20.3 (it looked for inline policies that became managed policies), so it now reads the managed policies. On an isolated stack it refuses to run without `ISOLATED_PRINCIPALS`, because its redeploys would otherwise delete every plane, and it also checks every plane role and plane runtime version. The legacy-index checks of the multi-harness script now always run in local index mode, and the deletion check uses a real registry deletion when owners' trees cannot be seeded.
- **`bin/verify-isolation.sh`.** The operator-side live check with two listed principals, one unlisted one and an optional Identity Center user. For example, B calls `invoke-agent-runtime` with A's runtime ARN and A's session ID and must get an authorization error, while A's identical call is the positive control. The script also covers stops, the shared runtime, cross-owner reads from the CLI (own credentials, own access role, A's access role), reads from inside A's microVM (B's tree, registry table, plane parameter, runtime configuration), the 403 for the unlisted caller, and both owners' own workflow. It masks account IDs and deletes its test workspaces.
- **Documentation.** The workspaces, deploy, security and getting-started guides and `SECURITY.md` explain how to enable the feature, the entry forms, adding and removing principals, retained storage and its purge, the runtime quota, teardown, caller and deploy-principal permissions, Telegram and `ReadOnlyAccess` behavior, boundary administrators and the residual risks. They claim only what tests, Access Analyzer and the simulator show, and say the live check is still to run. The spec is now "Partially verified". Parent TASK-20 has its first criterion checked and stays In Progress.

## Outcome

All suites are green (image-side: only the known TASK-22 failures). The new script tests run the verify scripts against a fake world that enforces the isolation rules. `bin/verify-isolation.sh` passes there and fails on each injected leak: a join, an in-agent read, and a bucket-policy gap. Nothing ran against AWS: the microVM is read-only and no isolated stack exists. The live two-principal run is the operator's next step (TASK-20 AC #2). Proposed doc examples wait in the task notes for the maintainer.

## Lesson

A verification script is code too, and it needs a test. The first draft of `verify-isolation.sh` passed shell functions to `env`, which cannot run them, so every positive control would have failed on the first live run. Review caught it; the fake world of about a hundred lines now catches that kind of bug in seconds, without AWS. Also, check each denial for the reason it was denied: an error such as `NoSuchKey`, or a missing object, looks like a denial if the script counts only non-zero exit codes.
