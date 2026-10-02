---
id: decision-15
title: 'Key the local stack-output cache by the STS caller account, region and stack'
date: '2026-10-02 04:15'
status: accepted
---
## Context

The CLI caches two outputs of the runtime stack, the runtime ARN and the
checkpoint bucket name, so that every command does not have to call
`describe-stacks`. The cache was two flat files in `~/.config/sch/` that did
not record which account, region or stack they came from. On 2026-10-02 the
maintainer, who uses two AWS accounts that both deploy `sch-dev-runtime`,
had a cache written under one account while working with the other. A
running headless task was then reported as `state: none`: `sch status` read
the other account's bucket, got AccessDenied, and showed that as "no task"
(TASK-25). The same cache would send an isolation-off command to the other
account's runtime, and changing `SCH_ENV` reused the previous stack's values.

Three ways to tell entries apart were considered:

- the account of the current credentials, from `sts get-caller-identity`:
  exact, but one more AWS CLI call per command (about 0.6 s measured);
- the `AWS_PROFILE` name: no extra call, but wrong when a profile changes
  account or when credentials come from environment variables;
- no cache at all: two `describe-stacks` calls per command, slower than one
  STS call.

## Decision

The cache lives under
`~/.config/sch/stack-outputs/<account>/<region>/<stack>/` with one plain-text
file per value, and `<account>` comes from STS, looked up at most once per
command and only when no `SCH_RUNTIME_ARN` / `SCH_CHECKPOINT_BUCKET` override
applies. The maintainer chose the exact key over the zero-cost profile key.
The flat files of the earlier layout are never read and are removed when a
keyed entry is written. Normative text: cli-cross-platform R9a.

## Consequences

- A value resolved with one account's credentials is never used with
  another's; switching profiles back and forth keeps both entries warm.
- Commands that resolve the runtime ARN or the bucket from the cache pay one
  STS call, about 0.6 s. The two overrides skip it, and with isolation on the
  runtime ARN comes from the registry plane without it.
- With credentials that do not work, those commands now stop at the account
  lookup with an error that names the AWS error and the two overrides.
- `sch destroy` removes only the destroyed deployment's entry (installation
  R15).
