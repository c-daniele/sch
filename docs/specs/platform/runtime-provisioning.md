# Runtime provisioning

> Domain: [Platform](../README.md) · Status: Implemented · Source: rationalized from openspec specs (2026-08-28)

## Purpose

Infrastructure-as-Code (CloudFormation) and a reproducible deploy script for everything SCH needs in an AWS account: a bootstrap stack (ECR repository, the CodeBuild project that builds the runtime image, and the bucket carrying the build source), the AgentCore Runtime with mounted session storage, lifecycle settings, the IAM execution role, the checkpoint S3 bucket, and (optionally) the CodeBuild resources for in-session image rebuilds.

## Scope

In scope:
- Bootstrap stack (ECR repository, deploy-time image build project, build-sources bucket), AgentCore Runtime, checkpoint bucket, execution role, session rebuild project (opt-in) as CloudFormation templates.
- Deploy script, deploy parameters (region, environment), rollback procedure.

Out of scope:
- Contents of the container image ([runtime-image](runtime-image.md)).
- Session-side rebuild flow and tagging rules ([session-image-rebuild](session-image-rebuild.md)).
- Checkpoint payload semantics and retention of workspace data beyond the bucket lifecycle settings ([workspace-checkpointing](../workspace-lifecycle/workspace-checkpointing.md)).

## Requirements

### ECR

**R1.** The provisioning SHALL include a bootstrap CloudFormation template, deployed FIRST, that creates every resource a deploy needs before the runtime exists, parameterized by `ProjectName` and `Environment`:

- the ECR repository for the runtime image, ready to receive the `linux/arm64` push;
- an `arm64` CodeBuild project that builds that image at deploy time, with the buildspec defined in the template (not supplied by the caller) and the destination tag taken from a per-build environment variable;
- a build-sources S3 bucket with public access blocked, at-rest encryption and an expiry lifecycle rule on the uploaded build contexts;
- a service role for that project limited to authentication and push on that one repository, reading that one bucket prefix, and writing its own log stream.

**R1a.** The deploy-time build project MUST NOT depend on the runtime stack, and the runtime stack MUST NOT own it: the runtime references an image tag that must already exist, so a first deploy on an empty account SHALL succeed in a single pass with no preparatory run, no flag, and no environment variable.

**R1b.** The build role MUST NOT be able to write to the build-sources bucket, read the checkpoint bucket, invoke Bedrock, or touch IAM.

### Runtime and session storage

**R2.** The provisioning SHALL create an AgentCore Runtime referencing the ECR image and configuring `filesystemConfigurations` (session storage, Preview) with mount point `/mnt/workspace`, so each runtime session has a filesystem that persists across stop/resume cycles.

**R3.** Session storage availability SHALL be deploy-parameterized: when unavailable in the primary target region (`eu-west-1`), the deploy SHALL succeed without template changes in a supported region (e.g. `us-west-2`).

**R4.** The runtime template SHALL expose `idleRuntimeSessionTimeout` and `maxLifetime` as parameters, with defaults of 900 seconds and 28800 seconds (8h) respectively. When a session remains without activity beyond the idle timeout, the microVM SHALL be stopped by AgentCore (compute cost ceases) while the session storage remains available for resume.

### Checkpoint bucket

**R5.** The runtime stack SHALL create an S3 bucket dedicated to checkpoints with: at-rest encryption (SSE-S3), full public access block, versioning enabled, a lifecycle policy deleting non-current versions after a parametric retention (default 30 days) and aborting incomplete multipart uploads after 1 day.

**R6.** The bucket SHALL have `DeletionPolicy: Retain` and `UpdateReplacePolicy: Retain`: stack deletion MUST NOT destroy the checkpoints.

**R7.** The bucket name SHALL be exposed to the runtime via the environment variable `SCH_CHECKPOINT_BUCKET`.

### Execution role

**R8.** The IAM execution role SHALL include: `bedrock:InvokeModel*` on the required inference profiles (Claude, cross-region), CloudWatch log write permissions, read-only access to the account's AWS resources via the `ReadOnlyAccess` managed policy (supporting the `aws-mcp` MCP server), and S3 write permissions limited to the checkpoint, generation, and writer prefixes (`s3:PutObject`, `s3:GetObject`, `s3:AbortMultipartUpload`, `s3:PutObjectTagging` on `<bucket>/checkpoints/*`, `<bucket>/checkpoint-generations/*`, and `<bucket>/workspace-writers/*`; `s3:ListBucket` on the bucket constrained to those prefixes).

**R9.** When, and only when, the image rebuild capability is enabled, the execution role SHALL additionally receive exclusively: `s3:PutObject` and `s3:AbortMultipartUpload` on `<bucket>/builds/*`, `codebuild:StartBuild` and `codebuild:BatchGetBuilds` on the ARN of the dedicated project only, and `logs:GetLogEvents`/`logs:FilterLogEvents` on its log group only.

**R10.** The execution role MUST NOT include: `s3:DeleteObject` on the checkpoint bucket, static credentials, ECR write permissions, `iam:PassRole`, `codebuild:CreateProject`/`UpdateProject`/`DeleteProject`, nor any write/mutation permissions beyond R8–R9. A mutating AWS CLI command executed with the role credentials SHALL fail with an IAM authorization error, even if it bypasses the MCP server's `--read-only` flag (see [runtime-image](runtime-image.md), security boundary).

**R11.** A direct ECR push with the execution role credentials SHALL fail with an IAM authorization error, even with the rebuild capability enabled.

### Opt-in image rebuild capability

**R12.** The in-session rebuild capability SHALL be disabled by default and activatable only via an explicit stack parameter, and SHALL be independent of how a DEPLOY builds the image: deploying with the default (CodeBuild) build path MUST NOT enable it, and MUST NOT grant the execution role any additional permission. With the capability disabled, the provisioning MUST NOT create the build project, its role, or its log group, and the execution role MUST NOT receive the R9 permissions. Redeploying with the parameter back to false SHALL remove the build resources and the additional permissions, while already-published images remain in ECR.

**R13.** When enabled, the provisioning SHALL create an `arm64` build project with a service role distinct from the execution role. The build role SHALL be the only principal authorized to publish images to the project's ECR repository, SHALL be limited to authentication and push on that repository only, reading the `builds/*` prefix only, and writing to its own logs. It MUST NOT have access to the checkpoint prefix, Bedrock, IAM, or any S3 write. The produced image SHALL be `linux/arm64`.

**R14.** The provisioning SHALL configure automatic expiration of objects under `builds/` with parameterized retention. The ECR lifecycle policy SHALL prevent session-produced images from evicting the images published by the deploy: when session builds exceed the retention threshold, only the oldest session images are expired.

### Deploy script

**R15.** The repository SHALL provide a deploy script (`infra/deploy.sh` or equivalent) executing in this order: bootstrap stack deploy, `linux/arm64` image build/push, runtime stack deploy. The script MUST be re-runnable (idempotent with respect to existing stacks) and parameterized by region and environment; a greenfield run SHALL produce a working AgentCore Runtime with no undocumented manual steps.

**R15a.** The image build SHALL run on the bootstrap stack's CodeBuild project by default, because AgentCore Runtime accepts `linux/arm64` only and that project builds it natively. A local Docker build SHALL remain available through an explicit flag, and SHALL be the only path that may consume operator-supplied corporate CA certificates (`image/certs/*.pem`), which MUST never be uploaded to CodeBuild.

**R15b.** The script SHALL be invocable through the client CLI (`sch deploy`) with pass-through options, so operating a deployment never requires locating the support repo checkout by hand.

### Runtime version

**R15c.** The runtime stack SHALL reference the image by digest (`<repo>@sha256:<digest>`), the digest being resolved by the deploy from the registry after the push (or, for an explicitly chosen tag without a build, from the existing image, failing fast when the tag does not exist); the tag stays in the `ApplicationVersion` parameter. The reference MUST NOT combine tag and digest (`<repo>:<tag>@sha256:<digest>`): the form passes CloudFormation validation but AgentCore fails to start the microVM (observed live: 502 on every invoke, no container log). Rationale: AgentCore resolves the reference once, when it creates a runtime version, so a tag-only reference never picks up a rebuild of the same tag. Consequently every image-building deploy SHALL produce a new runtime version running the freshly pushed image, whether or not the tag changed; a stack-only deploy without an explicit version SHALL reuse the deployed tag and digest and MUST NOT change the runtime version. The runtime environment SHALL expose the pinned digest (`SCH_IMAGE_DIGEST`) so a session can name the build it runs, and the deploy SHALL end by printing the runtime version and container URI reported by AgentCore. A stack deployed before this requirement keeps its tag-only reference until its next image-building deploy.

**R15d.** The client SHALL pin each workspace to the runtime version it last provisioned on and, on a provisioning command (`run`, `task`, `shell`, `attach`, `web`, `acp`) finding a different deployed version, SHALL rotate the workspace's runtime session (new session ID, `sessionEpoch + 1`, harness, storage backend and git-native binding preserved) after explaining on stderr what changed and that only work never checkpointed on the old session is lost; a workspace without a recorded version adopts the deployed one without rotating, and a failed version lookup proceeds on the existing session with a warning. Read-only commands (`status`, `list`, `fetch`) MUST NOT look the version up nor rotate. Epoch semantics: [selectable-workspace-storage](../security/selectable-workspace-storage.md) R3.

**R16.** The rollback procedure SHALL be a single command (`sch destroy`, specified in [`installation.md`](installation.md)) covering both stacks, the repository contents and every bucket of the deployment, and SHALL be documented with an explicit warning that `DeleteAgentRuntime` also deletes the associated session storage.

**R17.** The runtime stack SHALL expose the TASK-1 memory-cap defaults as
parameters (`NodeHeapMb`, default 1792, 0 disables; `BuildJobs`, default 2,
minimum 1), wired into the runtime environment as `SCH_NODE_HEAP_MB` /
`SCH_BUILD_JOBS` and honored per [runtime-image](runtime-image.md) R47.
`infra/deploy.sh` SHALL accept the same-named env overrides. Changing them
SHALL redeploy in place without touching the image; per-workspace env
overrides MUST NOT require even that.

## Behavior

- `infra/deploy.sh` (or `sch deploy`) deploys `infra/bootstrap.yaml`, builds and pushes the arm64 image from `image/Dockerfile` on the bootstrap stack's build project (or locally with `-l`), then deploys `infra/agent_runtime.yaml`. The runtime environment receives `SCH_CHECKPOINT_BUCKET` and (when the in-session capability is enabled) the rebuild project variable consumed by `image/scripts/sch-build-image.sh`.
- A deploy that rebuilds the image under an unchanged tag (`sch deploy` twice) yields a new runtime version whose container URI carries the new digest, visible as `SCH_IMAGE_DIGEST` in a fresh microVM; `sch deploy -s` afterwards leaves the runtime version untouched (R15c). Existing workspaces rotate their session on their next provisioning command (R15d).
- Idle sessions (900s default) are stopped by AgentCore; compute cost stops, session storage and checkpoints remain for resume.
- Model invocations from the microVM (OpenCode `amazon-bedrock` provider, Claude Code Bedrock mode) succeed via the execution role credential chain with no API key configured; `aws sts get-caller-identity`, `aws s3 ls` and other reads succeed via `ReadOnlyAccess`; writes succeed only under `checkpoints/<workspace>/`.
- End-to-end capability checks: `bin/verify-persistence.sh`, `bin/verify-session-image-rebuild.sh`.

## Invariants

- **I1.** Stack deletion never destroys the checkpoint bucket or its contents.
- **I2.** With the execution role credentials, any S3 write outside `<bucket>/checkpoints/*`, `<bucket>/checkpoint-generations/*`, and `<bucket>/workspace-writers/*` (plus `<bucket>/builds/*` only when the rebuild capability is enabled) fails with an IAM authorization error, and `s3:DeleteObject` on the checkpoint bucket always fails.
- **I3.** With the capability disabled, the execution role contains no `codebuild:*` permissions and no build resources exist.
- **I4.** The build role can never read or write `checkpoints/*`, invoke Bedrock, or access IAM.
- **I5.** Images published by the deploy are never expired by the ECR lifecycle policy in favor of session-built images.
- **I6.** A deploy performed with default options leaves the execution role byte-identical to a deploy performed with a local build: the build engine is not a runtime capability.

## Cross-references

- [runtime-image](runtime-image.md) — container contents, shim, security boundary shift to IAM.
- [session-image-rebuild](session-image-rebuild.md) — in-session rebuild flow built on R12–R14.
- [workspace-checkpointing](../workspace-lifecycle/workspace-checkpointing.md) — checkpoint semantics over the R5–R7 bucket.
- [runtime-capability-tuning](../security/runtime-capability-tuning.md) — deploy-time shaping of this execution role (capabilities, Bedrock allow-list, ReadOnlyAccess toggle, escape hatch).
- [MANIFESTO](../../../MANIFESTO.md)
- Code: `infra/bootstrap.yaml`, `infra/agent_runtime.yaml`, `infra/deploy.sh`, `cli/sch/commands/deploy.py`, `cli/sch/commands/destroy.py`, `cli/sch/awsteardown.py`, `cli/sch/harness.py` (runtime version rotation, R15d), `image/Dockerfile`, `image/scripts/sch-build-image.sh`, `bin/verify-session-image-rebuild.sh`; tests `infra/test_deploy_image_pin.py`, `cli/tests/test_runtime_version_rotation.py`
