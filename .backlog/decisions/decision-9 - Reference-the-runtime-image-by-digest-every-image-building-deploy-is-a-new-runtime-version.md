---
id: decision-9
title: >-
  Reference the runtime image by digest: every image-building deploy is a new
  runtime version
date: '2026-09-06 11:06'
status: accepted
---
## Context

AgentCore resolves a runtime's container image reference once, when it creates
a runtime version. `infra/agent_runtime.yaml` referenced the image by tag alone
(`<repo>:<ApplicationVersion>`), so `sch deploy` could rebuild and push a new
image under the same tag, update the stack successfully, and leave the runtime
on the previous build with no signal to the operator (TASK-30, found while
verifying TASK-29). Two ways to close the gap were on the table: force a
property change on the runtime resource (e.g. an environment variable carrying
the digest) while keeping the tag reference, or reference the image by its
digest. A third option, content-addressed builds that skip the rebuild when
`image/` is unchanged, would avoid spurious version bumps but is a larger
feature and does not by itself make a rebuilt image live.

## Decision

The runtime stack references the image by digest, `<repo>@sha256:<digest>`,
with the digest resolved by `infra/deploy.sh` from ECR right after the push
(or from the existing image when a tag is chosen explicitly with `-s -v`). The
tag stays in the `ApplicationVersion` parameter, in `SCH_IMAGE_VERSION` inside
the image and in the deploy's final output; the digest is also exposed to the
runtime as `SCH_IMAGE_DIGEST`. A stack-only deploy (`-s` without `-v`) reuses
the deployed tag and digest and never changes the runtime version. The
combined form `<repo>:<tag>@sha256:<digest>` is not used: CloudFormation
accepts it and AgentCore creates the version, but the microVM never starts
(verified live). The deploy ends by printing the runtime version and
container URI reported by AgentCore, so the operator sees what the runtime
actually runs. Spec: `runtime-provisioning` R15c/R15d.

## Consequences

- Every deploy that rebuilds the image is a new runtime version, whether or
  not the tag changed and whether or not the sources changed (container builds
  are not reproducible). A new version resets the session storage of every
  workspace and retires the microVMs of the superseded version; workspaces are
  restored from their L2 checkpoint on the next provisioning command, which
  also rotates their session (`sch: runtime version changed ...`). Work never
  checkpointed on the old session is lost, so operators `sch stop` workspaces
  they care about before an image-building deploy, and use `-s` for changes
  that do not touch the image (switches, IAM tuning, watchdog, registry).
- `-v` is a human-readable label for a build, no longer the trigger that makes
  it live. Rollback to a previous build is `-s -v <tag>` only while that tag
  still points at it; a superseded same-tag build becomes untagged and is
  subject to the ECR "last 10 images" lifecycle rule.
- `bin/verify-runtime-iam-tuning.sh` keeps its no-reset invariant because it
  always deploys with `-s`.
- Open follow-up, not committed: skipping the build when `image/` is unchanged
  (content-addressed builds) would remove the spurious version bumps of a
  deploy that only meant to change a switch but forgot `-s`.
