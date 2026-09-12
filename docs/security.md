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

Workspace storage is owner-scoped: each IAM principal gets its own S3 prefix,
and one user's agent cannot reach another user's state. The optional IAM
workspace registry adds an authenticated control plane so teams share
workspace names without sharing data —
[`owner-scoped-workspace-storage.md`](specs/security/owner-scoped-workspace-storage.md),
[`iam-workspace-registry.md`](specs/security/iam-workspace-registry.md).

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
