---
id: doc-18
title: '2026-09-27 Isolation: per-principal plane stacks and deploy'
type: other
created_date: '2026-09-27 16:40'
updated_date: '2026-09-27 16:40'
tags:
  - journal
---
# 2026-09-27 Isolation: per-principal plane stacks and deploy

Task: TASK-20.3 (slice 3 of TASK-20). Spec: [`per-principal-isolation.md`](../../../docs/specs/security/per-principal-isolation.md). Decision: decision-14.

## Problem

After TASK-20.2 the runtime could write under an owner segment, but nothing created a runtime per user, locked it, or kept one user's agent out of another user's storage. The deploy had no way to take a list of users, and `sch destroy` did not know that per-user stacks could exist.

## What changed

- The shared runtime's permissions now live in customer managed policies, so the shared role and every per-user role attach the same documents. With isolation off the permissions are the same as before; only the policy moved out of the role.
- A new template creates one "plane" per listed user: a runtime whose resource policies deny everyone except that user, an execution role capped by a permissions boundary, a read-only role the user's CLI will use for its own checkpoints, and a small parameter that tells the registry where the user's runtime is. The parameter is created only after both locks exist.
- With isolation on, the runtime stack adds one bucket policy that confines every plane role to its own storage (using the role's owner tag), locks the shared runtime, and narrows the registry role.
- `infra/deploy.sh` accepts `ISOLATED_PRINCIPALS`. Before touching any stack it resolves every entry with IAM reads, refuses unknown, duplicate or ambiguous entries and the Telegram switches, warns about unverifiable Identity Center usernames, checks the runtime quota and the managed-policy size. Then it deletes planes whose entry was removed, updates the runtime stack, and deploys one stack per user; a failing user does not block the others.
- `sch destroy` removes the plane stacks first.
- Two read-only scripts reproduce the evidence: Access Analyzer on every rendered policy and the IAM policy simulator on the storage and lock cases. Their reports are committed with a placeholder account.

## Outcome

Access Analyzer reports no errors on 37 documents. The simulator gives the expected decision in all 222 cases: owners reach their own storage and runtime; other users, a second Identity Center user of the same permission set and unlisted roles are denied, even with broad permissions. All suites pass. An actual deploy with two users is still the operator-side check of TASK-20.

## Lesson

The simulator can be wrong in both directions for reasons unrelated to the policy under test: it reports an implicit deny for `logs:GetLogEvents` on a log-stream ARN even under `logs:Get*` on `*`. A denial only counts as evidence when a control case, the same action on a non-SCH resource, is allowed; every boundary denial in the report is paired with one. Also, moving a policy between CloudFormation resource types needs a new logical ID, because a stack cannot change the type of an existing resource.
