---
id: TASK-25
title: >-
  Key the runtime ARN and bucket cache by account, region and stack; surface sch
  status read errors
status: Done
assignee: []
created_date: '2026-10-02 03:51'
updated_date: '2026-10-02 04:17'
labels:
  - cli
  - reliability
dependencies: []
references:
  - cli/sch/config.py
  - cli/sch/commands/status.py
  - cli/sch/commands/destroy.py
  - bin/sch-watch
documentation:
  - docs/specs/access-surfaces/cli-cross-platform.md
  - docs/specs/access-surfaces/headless-task-execution.md
  - docs/specs/platform/installation.md
priority: medium
type: bug
ordinal: 22000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
## Problem

On 2026-10-02 a headless task in workspace `cl_sch` was running (fresh heartbeat), but `sch status cl_sch` printed `state: none`, as if the workspace had never run a task. Two defects combined:

1. The CLI caches the runtime ARN and the checkpoint bucket name in `~/.config/sch/runtime-arn` and `~/.config/sch/checkpoint-bucket` without recording which AWS account, region or stack they came from. The maintainer uses two AWS accounts that both have a `sch-dev-runtime` stack, and the cache had been written while using the other account. The task itself reached the right runtime, because with per-principal isolation on the runtime ARN comes from the registry plane, but `sch status` read the task status from the other account's bucket. The same stale cache sends an isolation-off command to another account's runtime, and changing `SCH_ENV` or `SCH_PROJECT` reuses the previous stack's values. This is the masterplan open question "Disk cache invalidation".
2. `read_offline_status` (`cli/sch/commands/status.py`) turns every failed `s3api get-object` into `{"state":"none"}` and discards stderr, so an AccessDenied on the wrong bucket looks exactly like "never ran a task". headless-task-execution R17 makes `none` the answer for a workspace without a task, not for a read that failed.

While touching these files: the stale qualifier of `sch status` and the messages of `bin/sch-watch` are still in Italian, against the project language rule.

## Evidence

Same laptop and credentials: `sch status cl_sch` printed `state: none` with the cached bucket and `state: running` (heartbeat 13 s old) with `SCH_CHECKPOINT_BUCKET` set to the bucket of the current account.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 A runtime ARN or checkpoint bucket name cached under one AWS account, region or runtime stack name is never used under another; each combination is resolved from its own stack and cached separately
- [x] #2 `SCH_RUNTIME_ARN`, `SCH_CHECKPOINT_BUCKET` and the isolation plane runtime keep precedence over the cache, and an override needs no extra AWS call
- [x] #3 The legacy flat cache files are never read; `sch destroy` removes the cached values of the destroyed deployment and the legacy files; `bin/sch-cleanup` keeps preserving the cache
- [x] #4 `sch status` prints `state: none` only when the task-status object does not exist; any other read failure (access denied, missing bucket, expired credentials) exits 1 with an `sch:` message naming the bucket, the key and the AWS error, with nothing on stdout (also with `--json`)
- [x] #5 The stale qualifier of `sch status` and every message of `bin/sch-watch` are in English
- [x] #6 Specs (cli-cross-platform R9, headless-task-execution R17, installation R15) describe the new behavior; unit tests cover account and stack separation, override precedence, a failed account lookup, `none` versus read errors, and destroy invalidation; `cli/tests` and `bin/verify-docs.sh` pass
- [x] #7 `bin/sch-watch` suggests `sch fetch` only for git-native workspaces and `sch run` for the others, using the `session_mode` field of `sch status --json`, which headless-task-execution now documents
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
Decisions taken with the maintainer on 2026-10-02: the cache key is the caller account from STS (one `sts get-caller-identity` per command, about 0.6 s, only when no override applies), plus region and stack; the `sch-watch` retrieval hint is in scope.

1. Branch `fix/stack-cache-account-keys` from `dev`.
2. `cli/sch/config.py`: `caller_account(cfg)` runs `aws sts get-caller-identity`, validates 12 digits, memoizes the result on the config object for the whole command (lock: the dashboard resolves from a thread pool) and dies naming the AWS error and the `SCH_RUNTIME_ARN` / `SCH_CHECKPOINT_BUCKET` escape hatch. Cache layout `stack-outputs/<account>/<region>/<stack>/{runtime-arn,checkpoint-bucket}`, plain text, atomic write; an empty file is a miss; a region or stack name that is not a safe path component bypasses the cache. Override first (no STS), then keyed cache, then `describe-stacks`. The legacy flat files are never read and are removed when a keyed entry is written. Helpers to locate and drop one entry. Fix the stale module docstring (the bash reference implementation is gone).
3. `cli/sch/commands/destroy.py`: drop the destroyed deployment's entry (account from STS, region, `<project>-<env>-runtime`) and the legacy files.
4. `cli/sch/commands/status.py`: capture stderr of `get-object`; `NoSuchKey` gives `{"state":"none"}`; any other failure dies with bucket, key and AWS error; the dashboard's strict mode keeps raising. English stale qualifier.
5. `cli/sch/dashboard.py`: a `SystemExit` from bucket or account resolution degrades the row to unknown instead of ending the refresh thread (dashboard-tui R7); the failed STS call is a new path to it.
6. `bin/sch-watch`: English messages; terminal and stale hints suggest `sch fetch` for `session_mode: git-native` and `sch run <ws>` otherwise.
7. Tests: new `cli/tests/test_stack_cache.py`; `test_dashboard_data.py` (none vs errors, SystemExit row); `test_status_staleness.py`; `test_destroy.py`; `test_cleanup.py`; new `test_sch_watch.py`.
8. Specs and docs: cli-cross-platform R9 (+ cache rule), headless-task-execution R17, R19 and I5 (`--json` carries the git-native fields and `--live` fields, never a staleness field), installation R15, `bin/sch-cleanup` message, masterplan open question and roadmap.
9. Verify: `cd cli && python3 -m unittest discover -s tests`, `bin/verify-docs.sh`, read-only live check with `personal_dev_mfa` (fresh config dir: entry created under the right account, `sch status cl_sch` reads the task; wrong-bucket override exits 1 with AccessDenied).
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-10-02: implementation done on `fix/stack-cache-account-keys`. `config.py` keys the cache by STS account, region and stack (`stack-outputs/<account>/<region>/<stack>/`), memoizes the account lookup per config object (failure included, so degrading callers do not repeat it), writes atomically, ignores and removes the flat files, and now names the AWS error when `describe-stacks` fails. `sch status` maps only `NoSuchKey` to `none`; other failures die with bucket, key and AWS error. The dashboard row reader also catches `SystemExit`, which the new account lookup can raise and which used to end the refresh thread. `sch-watch` messages in English, hint by `session_mode`; its header pointed at a stale `main.py` line number and now names the functions. Live checks of the AWS CLI wording: a missing key gives `(NoSuchKey)`, the other account's bucket gives `(AccessDenied)`, both exit 254. Tests: 677 pass (19 new: `test_stack_cache.py`, `test_sch_watch.py`, offline-status and dashboard cases); obsolete cache attributes dropped from five test stubs.

2026-10-02 verification: `cd cli && python3 -m unittest discover -s tests` 677 OK; `bin/verify-docs.sh` all checks passed (new untracked files checked by hand for account IDs and home paths: none). Live, read-only, with the `personal_dev_mfa` profile and a copy of the real `~/.config/sch` (still holding the other account's flat files): `./bin/sch status cl_sch` resolved `stack-outputs/<account>/eu-west-1/sch-dev-runtime/checkpoint-bucket` for the current account, removed both flat files and showed the task (`state: succeeded`, `checkpoint: confirmed`, finished 03:44:03Z); repeat runs 3.7 s and 2.5 s against 7.1 s for the first one, which also resolved the stack. Forcing the other account's bucket with `SCH_CHECKPOINT_BUCKET` and `--json`: exit 1, 0 bytes on stdout, `sch: cannot read the task status from s3://<bucket>/checkpoints/o.<owner>/<identity>/task-status.json: An error occurred (AccessDenied) ...`. `./bin/sch status oc_sch` (a local workspace never run in this account): `state: none`, exit 0, from `NoSuchKey`. Note: the `sch` on PATH is a uv tool install pinned to a GitHub commit, so it gets the fix only after merge and reinstall; `./bin/sch` has it now.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
A running headless task showed up as `state: none`. Two defects caused it: an account-blind cache of the runtime ARN and checkpoint bucket, and `sch status` turning every S3 read failure into "no task". Both are fixed on branch `fix/stack-cache-account-keys`.

- Cache: values live under `~/.config/sch/stack-outputs/<account>/<region>/<stack>/`. The account comes from one STS call per command, made only when no override applies (decision-15, cli-cross-platform R9a). The flat files are no longer read and are removed. `sch destroy` drops only its own deployment's entry (installation R15).
- `sch status`: `none` only on `NoSuchKey`. Any other read failure exits 1 and names the bucket, the key and the AWS error, with nothing on stdout (headless-task-execution R17). The dashboard degrades a resolution failure to an unknown row instead of losing its refresh thread.
- English stale qualifier and `bin/sch-watch` messages. `sch-watch` suggests `sch fetch` only for git-native workspaces and `sch run` otherwise; R19 and I5 now list the fields `--json` adds.

Verification: 677 unit tests (19 new) and `bin/verify-docs.sh` pass. Read-only live run with the maintainer's profile: `./bin/sch status cl_sch` built the right account's entry, removed the stale flat files and showed the finished task. Forcing the other account's bucket exited 1 with AccessDenied and empty stdout.

Not covered: the installed `sch` (uv tool from GitHub) gets the fix only after merge and reinstall. Italian phrases remain in comments of `cli/sch/runtime.py`, `image/app/main.py` and two tests, which `bin/verify-docs.sh` does not scan.
<!-- SECTION:FINAL_SUMMARY:END -->
