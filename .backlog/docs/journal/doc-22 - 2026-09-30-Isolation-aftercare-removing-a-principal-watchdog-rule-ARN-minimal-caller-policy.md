---
id: doc-22
title: >-
  2026-09-30 Isolation aftercare: removing a principal, watchdog rule ARN,
  minimal caller policy
type: other
created_date: '2026-09-30 08:18'
updated_date: '2026-09-30 08:19'
tags:
  - journal
---
# 2026-09-30 Isolation aftercare: removing a principal, watchdog rule ARN, minimal caller policy

Tasks: TASK-20 (Done), TASK-24 (open). Spec: [per-principal-isolation](../../../docs/specs/security/per-principal-isolation.md) R12, R45. Guide: [Getting started, caller permissions](../../../docs/getting-started.md#caller-permissions-on-an-isolated-stack).

## Problem

After the live check the maintainer kept isolation on for his own user and wanted the two test users gone, and the minimal caller policy documented in the getting-started guide had never been used on its own: the live check ran with deliberately broad grants.

## What changed

- Removing the test user's entry from `ISOLATED_PRINCIPALS` and redeploying deleted its plane stack as specified: no runtime, role, boundary or plane parameter of that owner remained, the operator's plane was untouched.
- The same deploy failed on something unrelated to isolation: the Lambda permission of the task watchdog read the EventBridge rule's ARN with `GetAtt`, and CloudFormation now refuses that on rules created by its legacy `AWS::Events::Rule` handler, whose physical ID is the rule name ("Provided Arn is not in correct format"). The stack rolled back cleanly. The rule has an explicit name, so the permission now builds the ARN itself. Any deploy of an existing stack would have hit this, isolation or not.
- The maintainer replaced the broad test policy on his user with the minimal policy of the guide (registry invoke, data plane on his own plane, assume-role on his own access role, describe-stacks). With that policy alone, `sch task`, `sch status` and `sch delete` worked; the guide no longer calls the policy "derived".
- `sch delete` on a workspace whose task was still running failed once with the registry timeout of TASK-24 and completed on retry, as the resumable deletion is designed to.

## Outcome

The dev stack runs isolated with one listed user, and the documented minimal caller policy is confirmed live. TASK-24 remains the one open defect of the registry.

## Lesson

A CloudFormation resource provider can change its identifier contract between two deploys of the same template; attributes that a resource can compute from its own explicit name are safer built with `Fn::Sub` than read with `GetAtt`. Also, an operator-side check of the minimal permissions is worth the ten minutes: "derived from what the code calls" is a hypothesis until someone runs with nothing else attached.
