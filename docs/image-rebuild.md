# Rebuilding the runtime image from a session

An opt-in capability that lets an agent inside a microVM rebuild the container image through CodeBuild. Off by default; enabling it is the only thing that grants the execution role a build permission. Normative behavior: [`docs/specs/platform/session-image-rebuild.md`](specs/platform/session-image-rebuild.md).

## Image Rebuild from a Session (CodeBuild)

Per [`docs/specs/platform/session-image-rebuild.md`](specs/platform/session-image-rebuild.md), in-session image rebuild is
**Disabled by default**, and independent of how a deploy builds the image: the
deploy-time build project lives in the bootstrap stack, so enabling this
capability is the only thing that grants the runtime execution role permission
to start a build.

A microVM cannot build container images: the process runs as `sch` (uid 1000),
there is no Docker daemon and no privileges to run one (namespaces, mounts,
cgroups), and container-in-container is not a documented AgentCore Runtime
capability. Rootless daemonless builders (`buildah`, `kaniko`, `buildkit`) need
user namespaces / setuid `newuidmap`, also unavailable. So the image is not
built in the session at all — the build is **delegated to AWS CodeBuild**:

```
session                                     AWS
───────                                     ───
sch-build-image
  1. zip image/ contents      ─────────────▶ s3://<checkpoint-bucket>/builds/<workspace>/source.zip
  2. codebuild start-build    ─────────────▶ CodeBuild (ARM64, privileged, ephemeral)
                                                 │ docker build --platform linux/arm64
                                                 │ docker push
                                                 ▼
  3. poll phases, print URI   ◀───────────── ECR  <project>-<env>:sessionbuild-<ts>-<n>
```

Enable and use:

```sh
ENABLE_SESSION_IMAGE_REBUILD=true ./infra/deploy.sh   # deploy the capability
# then, inside a session:
sch-build-image                 # package, upload, build, wait, report
sch-build-image --follow        # same, streaming the build log
sch-build-image --timeout 900   # bound the wait (default 2700s)
```

What it builds and where it lands:

- **Only this project's `image/` build context**, from `/mnt/workspace/repo/image`,
  including uncommitted edits (that is the point — the operator's laptop is not
  involved). Not a generic application-image builder.
- Same ECR repository as the runtime (`<project>-<env>`), on a **reserved tag
  namespace** `sessionbuild-<utc-timestamp>-<build-number>`. The tag is computed
  by the buildspec, which lives in `infra/agent_runtime.yaml` — **not** in the
  zipped source — so no caller input decides it and the deployed
  `ApplicationVersion` tag (e.g. `v1`) is never touched by the normal flow.
- ECR lifecycle keeps the last 5 `sessionbuild-*` images in a rule of their own,
  so session builds cannot evict deploy-pushed images (`infra/bootstrap.yaml`).
- Build sources under `builds/` expire after `BuildSourceRetentionDays` (7).

**The produced image is not deployed.** Nothing about the running runtime
changes. Promoting it means redeploying with `ApplicationVersion` set to that
tag, which **resets the session storage of every workspace** (L2 checkpoints
restore the workspaces, see above). `sch-build-image` prints this warning on
every successful build.

Permissions (`infra/agent_runtime.yaml`):

| Principal | Gets | Notably does NOT get |
|---|---|---|
| Execution role (session) | `s3:PutObject` on `builds/*`, `codebuild:StartBuild`/`BatchGetBuilds` on **that one project**, read of its log group | any ECR write, `codebuild:Create/Update/DeleteProject`, `iam:PassRole` |
| `<project>-<env>-image-rebuild-role` (CodeBuild) | ECR auth + push to **that one repository**, `s3:GetObject` on `builds/*`, own log stream | anything under `checkpoints/*`, Bedrock, IAM, any S3 write |

**Security limits, stated plainly (design R1/R2):** `codebuild:StartBuild`
accepts `buildspecOverride` and `environmentVariablesOverride`, and IAM has no
condition keys to forbid them. Whoever can start this project can therefore run
arbitrary commands with the CodeBuild role's credentials — including a push to
any tag of that repository. The reserved tag namespace protects against
*mistakes*, not against hostile input; the real containment is the CodeBuild
role's minimal perimeter. The repository is intentionally left
`ImageTagMutability: MUTABLE` and this residual risk is accepted for now; to close it,
reconsider `IMMUTABLE_WITH_EXCLUSION` (excluding `sessionbuild-*`), which forces
a new tag on every deploy. Keep the capability disabled anywhere the agent is
not inside your trust boundary.

Costs: minutes of `BUILD_GENERAL1_LARGE` ARM per build (this image downloads a
~440 MB model plus npm/uv packages, so expect single-digit minutes; local Docker
layer caching is enabled), plus S3 storage for the throwaway sources.
