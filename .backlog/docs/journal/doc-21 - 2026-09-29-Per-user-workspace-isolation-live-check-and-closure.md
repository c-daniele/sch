---
id: doc-21
title: '2026-09-29 Per-user workspace isolation: live check and closure'
type: other
created_date: '2026-09-29 08:55'
updated_date: '2026-09-29 08:56'
tags:
  - journal
---
# 2026-09-29 Per-user workspace isolation: live check and closure

Task: TASK-20 (parent of TASK-20.1 to TASK-20.5). Spec: [per-principal-isolation](../../../docs/specs/security/per-principal-isolation.md). Decision: decision-14.

## Problem

The five slices of TASK-20 were done and every requirement had unit tests, Access Analyzer and simulator evidence, but nothing had ever been deployed. Four things could only be learned from a real stack: whether CloudFormation accepts the `AWS::BedrockAgentCore::ResourcePolicy` locks, how AgentCore evaluates the runtime and endpoint policies together, which field API Gateway hands the registry as the caller's principal ID, and whether the bucket policy holds against real callers. The maintainer ran the check himself on his single-user PoC stack.

## What changed

- A double check of the slices against the code found one defect before the run: the cleanup trap of `bin/verify-isolation.sh` never fired under bash 3.2, the `/bin/bash` of macOS, because it was set inside a pipeline subshell. The suite had been green only on Linux.
- The stack was deployed in place with two listed IAM users (the operator and a test user) and one unlisted test user; the test users came from a small CloudFormation template with deliberately broad grants, so that every denial had to come from the resource policies, the access-role trust and the bucket policy, not from a missing identity grant. The deploy created the bucket policy, the shared-runtime locks and two plane stacks as designed.
- The first run stopped at once: the operator's own user lacked `execute-api:Invoke`, so API Gateway answered 403 before the registry ran, and the client showed a bare "403" because it looked for the registry's `error` field, not API Gateway's `Message`. Attaching the caller policy fixed the run; the client now reports both fields.
- The second run passed 36 of 37 checks. The one failure was a false negative: the `agentcore` CLI drops the AWS error text in `--json` mode and prints only `{"success": false}`. Run by hand, the same call fails with an explicit deny in a resource-based policy on `InvokeAgentRuntimeCommand`. The check now runs without `--json`, and the fake `agentcore` of the script tests behaves like the real one.
- The script's cleanup failed twice for two different reasons. First, the registry URL was assigned inside `main`, which the bash 3.2 fix had moved into a pipeline subshell, so the cleanup deleted in local-index mode against the locked shared runtime; it is resolved in the main shell now, and a test records the URL the fake `sch delete` receives. Second, the registry's DELETE timed out while AgentCore was still stopping the live microVM: the Lambda has a 10 second timeout and so does the client. The resumable deletion did what it was designed for, the records stayed in `deleting` and a retry finished in one or two seconds, so the cleanup now retries. The timeout itself is not specific to isolation and is left as a proposed follow-up.
- The third run passed 37 of 37. The spec moved to Implemented, and the guides, `SECURITY.md` and the changelog now claim the live result instead of announcing a pending check.

## Outcome

TASK-20 is Done. A second user with `bedrock-agentcore:*` and read access to the whole checkpoint bucket cannot join, stop or open a command on another user's session, cannot read another user's task status or checkpoints with its own credentials, its own access role or the other user's access role, and an agent inside a user's microVM cannot read another owner's tree, the registry table, another plane's parameter or runtime configuration. An unlisted user is refused with the entry to add. Not exercised live: an Identity Center owner.

## Lesson

A live run finds a different class of bug than tests and simulators: nothing in the isolation design was wrong, but four things around it were, and three of them were in the verification tooling itself (a trap that never fired, a variable lost to a subshell, a CLI that hides errors in JSON mode). Two habits would have caught them earlier. Run shell-based tooling with the platform's own `/bin/bash`, not only the one in the microVM. And make fakes lie the way the real tool lies: the fake `agentcore` reported the denial on stderr in `--json` mode, so the script test could not see the problem.

Also, when a permission is missing at the front door, the error looks like the application's own refusal. API Gateway's 403 and the registry's 403 differ only in the body's field name; a client that reads both saves the next person an hour.
