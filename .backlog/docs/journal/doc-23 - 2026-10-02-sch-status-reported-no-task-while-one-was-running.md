---
id: doc-23
title: 2026-10-02 sch status reported no task while one was running
type: other
created_date: '2026-10-02 04:16'
updated_date: '2026-10-02 04:16'
tags:
  - journal
---
# 2026-10-02 sch status reported no task while one was running

Task: TASK-25. Specs: [cli-cross-platform](../../../docs/specs/access-surfaces/cli-cross-platform.md) R9a, [headless-task-execution](../../../docs/specs/access-surfaces/headless-task-execution.md) R17 and R19, [installation](../../../docs/specs/platform/installation.md) R15. Decision: decision-15.

## Problem

The maintainer started a headless task in workspace `cl_sch` and could not tell whether it was running: `sch status cl_sch` answered `state: none`, as if the workspace had never run a task. The task was in fact running, with a heartbeat a few seconds old.

Two defects combined. The CLI caches the runtime ARN and the checkpoint bucket name in two files under `~/.config/sch/`, and those files did not say which AWS account they came from. The maintainer uses two accounts that both deploy `sch-dev-runtime`, and the cache had been filled while working in the other one. The task itself reached the right runtime, because with per-user isolation on the runtime comes from the registry. The bucket still came from the cache, so `sch status` read the other account's bucket and got AccessDenied. The second defect turned that into the answer: `sch status` treated every failed S3 read as "no task", and threw away the AWS error.

## What changed

- The cache now lives under `stack-outputs/<account>/<region>/<stack>/`. The account comes from `sts get-caller-identity`, once per command, and only when `SCH_RUNTIME_ARN` and `SCH_CHECKPOINT_BUCKET` are not set. The maintainer chose this exact key over a cheaper one based on the AWS profile name; the cost is about 0.6 s per command. The old flat files are never read and are deleted. `sch destroy` removes only the destroyed deployment's entry.
- `sch status` says `state: none` only when S3 answers that the status object does not exist. Any other failure exits 1 and names the bucket, the key and the AWS error, with nothing on stdout.
- The dashboard keeps refreshing when the account lookup fails; that row shows as unknown.
- The stale qualifier of `sch status` and the messages of `bin/sch-watch` are in English. At the end of a task, `sch-watch` suggests `sch fetch` only for `--branch` workspaces and `sch run` for the others.
- The specs describe the cache rule, when `none` is allowed, and the fields that `sch status --json` adds to the S3 object.

## Outcome

On the same laptop and profile, `./bin/sch status cl_sch` built the cache entry for the right account, removed the stale files and showed the finished task with a confirmed checkpoint. Pointing it at the other account's bucket now fails with AccessDenied instead of printing `state: none`. 677 unit tests and the documentation check pass. The `sch` installed with uv comes from a fixed GitHub commit, so it gets the fix only after the merge and a reinstall.

## Lesson

A status command must not turn "I could not read it" into "there is nothing". Map only the specific "not found" answer to the empty state and show every other failure with its cause. A local cache of cloud identifiers must also record the account and region it came from: on a laptop with more than one account, a cache that does not is wrong sooner or later, and nothing reports it.
