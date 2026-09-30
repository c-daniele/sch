---
id: doc-19
title: '2026-09-27 Isolation: registry owner mapping and CLI'
type: other
created_date: '2026-09-27 17:11'
updated_date: '2026-09-27 17:11'
tags:
  - journal
---
# 2026-09-27 Isolation: registry owner mapping and CLI

Task: TASK-20.4 (slice 4 of TASK-20). Spec: [per-principal-isolation](../../../docs/specs/security/per-principal-isolation.md) R7-R9, R36-R43. Decision: decision-14.

## Problem

After TASK-20.3, a deploy with `ISOLATED_PRINCIPALS` created one locked plane per listed user, but nothing used them: the registry still keyed owners by the caller ARN and handed out the shared runtime, which isolation locks. Enabling the switch would have broken every `sch` command.

## What changed

- **Registry.** With isolation on, the owner comes from the caller's principal ID, not its ARN. An IAM user is `user:<id>`; a role session is tried first as an Identity Center user (`sso:<role id>:<name>`), then as the whole role (`role:<role id>`). The first owner with a plane parameter wins. So a new session name keeps the owner, and two Identity Center users of one permission set are two owners. A caller without a plane, or from another account, gets HTTP 403 before any record is read. The message names the entry to add, for example `add user:carol to ISOLATED_PRINCIPALS and redeploy`. Plane lookups are cached for 60 seconds. Records store their owner prefix, and responses carry `isolation: true` and the plane. Deletion stops the session on the plane runtime and purges the owner-segment keys. With isolation off, nothing changes.
- **CLI.** A new client-side plane module checks the plane strictly: the runtime name, access role name and owner prefix must belong to this deployment and agree on the owner key. It then makes the plane runtime the only runtime of the command, ignoring `SCH_RUNTIME_ARN`, the cache and the stack output. Every invoke payload carries `owner_prefix`, and the runtime version pin reads the plane runtime. `sch status`, `sch list --remote-check` and `sch dashboard` read owner-segment keys through the access role. Its credentials stay in memory and are renewed five minutes before they expire, so a dashboard left open keeps working.
- **Latent registry bug.** The registry rejected every record read back from DynamoDB, because the boto3 resource returns numbers as `Decimal` and the epoch check accepted only `int`. The new DynamoDB test double, which validates like the real client, exposed it. It is fixed, but not verified live.

## Outcome

Every suite is green (image-side: only the known TASK-22 failures). Every AWS request the new code builds is checked against the botocore models. A golden test pins the registry-off `sch status` output, and a grep test shows that no `sch` command creates or changes runtimes, roles or policies. Still open: the live check with two principals (operator-side, TASK-20), and the verify scripts and operator guides (TASK-20.5).

## Lesson

A hand-written fake that returns what the code expects cannot find type mismatches with the real service. A test double built on the real SDK client, with only the network replaced, found a bug the old tests had hidden. Put the real serialization layer into tests of AWS-facing code. Also, a default argument like `clock=time.monotonic` is bound when the function is defined, so patching `time.monotonic` in a test has no effect. Read the clock inside the function.
