# Security posture — what SCH does in your account and what it never does

This is the operator's view of SCH's security model: the boundaries that
protect you, the defaults you are accepting, and the switches that change
them. It links to the guide or spec that owns each detail rather than
repeating it. Vulnerability reports go through [SECURITY.md](../SECURITY.md).

## The model in one paragraph

An SCH workspace is a microVM in **your** AWS account running a coding agent
with auto-approved permissions. Two boundaries apply. The **container** keeps
the agent inside its workspace: non-root user, pinned tools, no Docker, no
privileges, a filesystem that is checkpointed to S3. The **IAM execution role**
decides what the agent can reach *outside* the microVM: every AWS API the role
allows is available to the agent through the AWS CLI and the MCP servers, and
every API it denies fails with `AccessDenied`. The container is a convenience
boundary; IAM is the real one ([MANIFESTO](../MANIFESTO.md), principle 6).
Nothing SCH ships weakens IAM: optional features add narrowly scoped grants and
removing them removes the grants.

## What you are accepting by default

| Default | Why it is the default | How to change it |
| --- | --- | --- |
| The execution role carries AWS managed `ReadOnlyAccess` (metadata *and* data reads, e.g. `s3:GetObject`) | Backs the read-only AWS MCP server so the agent can inspect the account it works in | `RUNTIME_AWS_API_READ=false` or a scoped data bucket via `RUNTIME_DATA_BUCKET_ARN` — [Runtime capability tuning](runtime-capability-tuning.md) |
| The role may invoke **every** Bedrock model in every region | OpenCode's `/models` picker lets users choose any Bedrock model; a fixed pair failed with `AccessDenied` | `RUNTIME_BEDROCK_MODEL_ALLOWLIST` — [Runtime capability tuning](runtime-capability-tuning.md) |
| Agents run **unattended with auto-approval** for up to 7 hours per headless task (8 h microVM lifetime) | Detached execution is the core loop; a task that waits for a human is not detached | Shorter `SCH_TASK_TIMEOUT_S` ([limits table](deploy.md#session-storage-preview-limits--observed-and-documented)); interactive modes keep approvals in the TUI or on Telegram — [Headless tasks](headless-tasks.md) |
| The `--read-only` flag of the AWS MCP proxy is a **UX guard, not a boundary** | A shell-reachable `aws` binary bypasses it by design | Nothing to change: enforcement is the role. Narrow the role, not the flag — [MCP and Bedrock](mcp-and-bedrock.md#execution-role-permissions) |
| Single AWS account, single trust domain | SCH is built for one developer or one team inside one account | Use a dedicated development account; the deploy is parameterized by region and environment — [Deploying and operating](deploy.md) |
| Bedrock inference through the role, **no provider API keys anywhere** | No secrets to rotate, nothing to leak from a laptop | Per-user keys are opt-in and stay per user — see below |

Accept these consciously. The recommended posture for an account holding data
you would not show the agent is: a dedicated account, or `ReadOnlyAccess`
removed and a model allow-list set.

## What never enters the sandbox

- **Git credentials.** The remote workspace never holds a credential for your
  remotes. Work reaches your repository through `sch fetch` (a git bundle
  transferred over the tunnel) and is pushed with *local* credentials only —
  [Headless tasks](headless-tasks.md#git-native-sessions---branch--sch-fetch).
  A `SCH_REPO_TOKEN` for the first-boot clone is used for that clone only
  and is not saved in the workspace; images before this fix saved it in the
  clone's `origin` URL, so rotate a token used with one
  ([Using `sch`](cli.md#using-sch)).
- **Long-lived AWS keys.** The microVM authenticates through its execution
  role via IMDS; there are no static keys to steal.
- **Provider API keys, unless you opt in per user.** Keys live in `~/.sch/env`
  on your machine (mode `0600`, `sch` warns otherwise), travel in the invoke
  payload and are staged on a tmpfs path outside every checkpointed directory,
  so no S3 object, DB backup or state mirror can contain them —
  [Provider API keys](cli.md#provider-api-keys-per-user-anthropic-opencode-zengo-openrouter-kilo-bedrock)
  and [`docs/specs/providers-models/`](specs/providers-models/).
- **GitHub tokens, unless you opt in.** The same transport stages an
  ephemeral `GITHUB_TOKEN` for `gh` in a tmpfs credential store; the default
  stays credential-less ([decision-7](../.backlog/decisions/decision-7%20-%20Opt-in-GitHub-access-via-provider-keys-transport-and-tmpfs-credential-store.md)).
- **Your `.env` files and secrets in synced folders.** `--sync` excludes
  secrets by design; check the ignore rules before mirroring a folder that
  holds credentials — [Local workspace sync](cli.md#local-workspace-sync).

## Isolation between users

**Default (isolation off).** With the optional IAM workspace registry, each
IAM principal gets its own workspace names and S3 prefixes, so teams can reuse
names without their SCH commands touching each other's state —
[`owner-scoped-workspace-storage.md`](specs/security/owner-scoped-workspace-storage.md),
[`iam-workspace-registry.md`](specs/security/iam-workspace-registry.md).
This is not an access-control boundary. Every workspace runs on one shared
runtime whose execution role can read every checkpoint, and a principal that
can invoke the runtime and learns a session ID can join that session. Treat
such a deployment as one trust domain.

**Opt-in per-principal isolation** (`ISOLATED_PRINCIPALS`, [how to enable
it](deploy.md#per-principal-isolation-isolated_principals), [what users
see](workspaces.md#per-principal-isolation), normative spec
[`per-principal-isolation.md`](specs/security/per-principal-isolation.md))
gives every listed user a plane created at deploy time:

- a runtime whose resource policies (on the runtime and on its endpoint)
  deny every data-plane call to everyone but the owner, so knowing another
  user's runtime ARN and session ID is not enough to join or stop the session;
  the shared runtime refuses every user;
- an execution role, the one the owner's agent runs with, confined by a
  permissions boundary and the checkpoint bucket policy to the owner's own
  objects: it cannot read other owners' checkpoints, the registry and Telegram
  tables, SCH log groups, SCH runtime and Lambda configuration or the plane
  parameters;
- a read-only access role that only the owner can assume, through which `sch`
  reads the owner's checkpoints; the bucket policy denies the owner trees to
  every other principal.

What backs these statements today: unit tests of the templates, the registry
and the CLI, IAM Access Analyzer without errors, and IAM policy simulator runs
with the expected decision in every case (reports:
[Access Analyzer](history/isolation-evidence-access-analyzer.md),
[simulator](history/isolation-evidence-simulator.md)). The simulator evaluates
one resource policy at a time, so it does not model AgentCore's joint
evaluation of the runtime and endpoint policies, and it fills `aws:userid`
from the caller, so Identity Center callers were simulated with explicit
context. The live two-principal check (`bin/verify-isolation.sh`) passed on
2026-09-29 on a deployed stack with two IAM users and one unlisted IAM user (37
checks): joining, stopping and opening a command on another user's session fail
with an explicit deny in a resource-based policy, and cross-owner reads fail
from the CLI and from inside the agent's microVM. Identity Center owners have
simulator evidence only. Run the script on your own stack before relying on
the boundary.

**Boundary administrators are trusted.** Principals that hold any of IAM
write, AgentCore control-plane write (for example `BedrockAgentCoreFullAccess`),
`s3:PutBucketPolicy`/`s3:DeleteBucketPolicy` on the checkpoint bucket,
CloudFormation deploy rights with `iam:PassRole`, Lambda updates of SCH
functions, `ssm:PutParameter` on the plane parameters, or write access to the
registry table can remove or redirect any lock. Do not give listed users these
rights; the operator who deploys SCH is one of them.

**`ReadOnlyAccess` with isolation on.** Plane execution roles carry
`ReadOnlyAccess` exactly when the shared role does. Their boundary denies the
SCH reads that would expose secrets or other users' data (SCH runtime and
Lambda configuration, SCH log groups, SCH tables, plane parameters), but an
agent can still read, among
others: names and ARNs of SCH resources; IAM trust policies and plane stack
parameters, which reveal the listed identities (unique IDs, Identity Center
usernames); CloudFormation templates; the shared image in ECR; the image
rebuild's CodeBuild logs. None of these is a secret or workspace content
(residual risk X1). Set `RUNTIME_AWS_API_READ=false` to remove
`ReadOnlyAccess` from every plane.

**Telegram** is refused on an isolated stack: it is a single-operator feature
of isolation-off stacks.

**Residual risks** (full list in the spec, X1–X9): the `ReadOnlyAccess` reads
above; a short window at plane creation before the locks are attached, while
the runtime ARN is not published yet; unverified `sso:` usernames (a typo
binds the plane to someone else or to nobody); the simulator limits above;
the in-session image rebuild, when enabled, shares one CodeBuild project and
image across owners; `RUNTIME_DATA_BUCKET_ARN` is shared by every owner; every
session of a `role:` entry is one owner; a removed principal's plane may still
be returned by the registry for up to 60 seconds; the managed-policy size caps
the Bedrock allow-list at about 20 exact model IDs.

## The image is part of the contract

All harnesses run in one hardened, version-pinned image: every tool version
is an explicit build argument, the runtime is referenced by **digest**, and a
rebuild is a new runtime version you deploy deliberately
([decision-9](../.backlog/decisions/decision-9%20-%20Reference-the-runtime-image-by-digest-every-image-building-deploy-is-a-new-runtime-version.md)).
The opt-in *in-session* rebuild is the only feature that grants the execution
role a build permission — [Image rebuild](image-rebuild.md).

## Optional inbound channels

Telegram interaction is the one feature that opens an inbound path into a
running session: a public webhook authenticated by a per-deploy secret, a
single accepted chat id, and a Lambda that can only enqueue commands. Every
deploy regenerates the webhook secret unless you pin it —
[Telegram](telegram.md#security) and [Telegram setup](telegram-setup.md).

## Cost is a security property here

A runaway or hijacked prompt cannot exceed the role, but it can spend: model
invocations, microVM hours, and a shell left open past the idle timeout keep
the meter running. Set the timeouts you need, stop workspaces you are done
with (`sch stop`), and watch `AWS/Bedrock-AgentCore` metrics —
[Lifecycle and costs](deploy.md#lifecycle-and-costs). Cost budgets and alarms
in the deploy are on the roadmap
([masterplan](../.backlog/masterplan/MASTERPLAN.md)).

## Reporting

Found a way through any of these boundaries? Report it privately:
[SECURITY.md](../SECURITY.md).
