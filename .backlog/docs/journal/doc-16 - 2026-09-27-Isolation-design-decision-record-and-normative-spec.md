---
id: doc-16
title: '2026-09-27 Isolation design: decision record and normative spec'
type: other
created_date: '2026-09-27 15:25'
updated_date: '2026-09-27 15:25'
tags:
  - journal
---
# 2026-09-27 Isolation design: decision record and normative spec

Task: TASK-20.1 (slice 1 of TASK-20). Spec: [`per-principal-isolation.md`](../../../docs/specs/security/per-principal-isolation.md). Decision: decision-14.

## Problem

SCH promised per-user isolation, but registry mode only separates names. Every workspace runs on one shared AgentCore runtime whose role reads every checkpoint, and AgentCore authorizes invocations on the runtime, not on the session: a second user who learns a session ID can join it. The maintainer asked for real isolation, created at deploy time, replacing a rolled-back attempt that built resources from the registry at request time.

## What changed

- A decision record fixes the defaults of TASK-20: an explicit allow-list of principals, owners derived from unique IDs, one CloudFormation stack per principal, deny-only resource policies on each user runtime and its endpoint, shared permissions in managed policies, a permissions boundary, a read-only access role per user, no Telegram, no migration.
- A new normative spec (status Proposed) describes how entries are resolved, the owner key, the plane stack, the storage layout with an `o.<key>` owner segment, the registry, shim and CLI behavior, a threat model that names who administers the boundary, and nine residual risks.
- Three choices were made while writing it: one constant bucket policy that uses the role tag `sch-owner` instead of one statement per user (a bucket policy is capped at 20 KB); one SSM parameter per plane so the registry can find a user's runtime; and owner storage that nobody outside SCH's own roles can touch, deletes included.
- The existing security specs link the new one and mark what will change. `SECURITY.md` and `docs/security.md` no longer claim an isolation that is not enforced.
- TASK-20.2 to TASK-20.5 now carry implementation plans that cite the spec requirements.

## Outcome

The design is ready for the implementation slices. The two central policy shapes were checked early: Access Analyzer accepts them, and simulated requests give the expected allow and deny decisions. The check caught one error before any code existed: `s3:ListBucketMultipartUploads` does not support `s3:prefix`. Suites are unchanged; the image-side run shows only the seven known TASK-22 failures.

## Lesson

Validate policy shapes with Access Analyzer and the simulator while designing, not only after implementing. The one invalid condition would have been written into the template and its tests. The simulator also confirmed the fail-closed behavior of a missing tag variable, which the design depends on.
