# SCH — Serverless Coding Harness

**Run AI coding agents in disposable, checkpointed cloud sandboxes — with no
servers to manage and near-zero idle cost.**

SCH turns [Amazon Bedrock AgentCore Runtime](https://aws.amazon.com/bedrock/agentcore/)
into a personal (or team-wide) fleet of remote coding-agent workspaces. Each
workspace is an isolated **microVM** running your choice of coding harness —
[OpenCode](https://opencode.ai), [Claude Code](https://code.claude.com), or
[Pi](https://github.com/earendil-works/pi-coding-agent) — with durable storage,
a hardened and version-pinned container image, and an IAM-scoped security
boundary. You interact with it from a tiny cross-platform CLI (`sch`), from
your editor (Zed and other ACP editors), from a browser tab, or from Telegram
on your phone.

> **Status: pre-1.0, deployed and verified daily by its maintainer, not yet
> battle-tested by others.** It creates billable AWS resources and runs
> autonomous agents with auto-approved permissions inside your account. Read
> [Before you deploy](#before-you-deploy-costs-and-security) first.

## Why SCH?

Agentic coding workflows quickly outgrow the laptop:

- **You want to fire off long, unattended tasks** ("implement this change, run
  the tests, fix the failures") and close the lid — not baby-sit a terminal
  for two hours.
- **You want more than one agent working in parallel** on the same repository
  without them trampling each other's files.
- **You want a sandbox**: an agent with auto-approved permissions should run
  inside a disposable, isolated environment with a well-defined blast radius —
  not on your workstation with your SSH keys.
- **You do not want to run infrastructure** for any of this: no bastion EC2
  boxes, no Kubernetes, no long-lived VMs to patch and pay for.

SCH addresses exactly this gap with a serverless design: microVMs are created
on demand per workspace session, billed while they run, and evaporate on idle
timeout — while workspace state (repo, agent sessions, config) survives in
Amazon S3 checkpoints and is restored transparently on the next start. Idle
cost is approximately zero.

**Who is it for?** Individual developers who want detached tasks, phone
notifications, parallel sessions and editor integration without operating
anything; and small teams that want to hand developers a centrally controlled
coding-agent environment — one audited image, one IAM execution role as the
real permission boundary, per-user isolated workspaces, no provider API keys
on laptops (inference rides Amazon Bedrock through the runtime's role by
default). The [MANIFESTO](MANIFESTO.md) is the project's constitution.

## What you get

| Capability | In short | Guide |
| --- | --- | --- |
| **Three harnesses** | OpenCode, Claude Code, or Pi — chosen per workspace, with the same durability guarantees | [Harnesses](docs/harnesses.md) |
| **Interactive remote TUI** | `sch run my-project` opens the agent's TUI inside the microVM; `Ctrl+]` detaches and leaves it running | [CLI](docs/cli.md) |
| **Detached headless tasks** | `sch task my-project "build and test"` returns immediately; progress and outcome are observable offline via `sch status` | [Headless tasks](docs/headless-tasks.md) |
| **Durable workspaces** | Periodic + terminal S3 checkpoints; automatic restore after idle expiry, version updates, or session loss | [Workspaces](docs/workspaces.md) |
| **Git-native parallel sessions** | N agents on N branches from the same local HEAD, no shared writable state; deliver with `sch fetch` | [Headless tasks](docs/headless-tasks.md#git-native-sessions---branch--sch-fetch) |
| **Editor integration** | `sch acp` exposes a workspace as an ACP agent for Zed, with a synced local file mirror | [Remote access](docs/remote-access.md#editor-integration-zed) |
| **Web + local TUI access** | `sch web` opens the remote OpenCode UI in a browser; `sch attach` connects a local OpenCode TUI to the remote backend | [Remote access](docs/remote-access.md) |
| **Phone-based supervision** | Optional Telegram integration: milestone notifications, remote tool approval, follow-up prompts | [Telegram](docs/telegram.md) |
| **Local sync** | `--sync` mirrors your working directory into the workspace and back | [CLI](docs/cli.md#local-workspace-sync) |
| **Session handoff** | `sch handoff` pushes a local OpenCode conversation into a remote workspace to continue there | [CLI](docs/cli.md#opencode-session-handoff) |
| **Sandboxed by design** | IAM execution role as the enforcing boundary, read-only AWS posture, pinned tool versions, no git or provider credentials inside the microVM | [Security](docs/security.md) |
| **Cross-platform CLI** | Python-stdlib-only `sch` with full command parity on macOS, Linux, and Windows | [CLI](docs/cli.md#windows-support) |

## A five-minute tour

The tour assumes SCH is already deployed — for a new AWS account start from
[Getting started](docs/getting-started.md).

```sh
# Open an interactive agent TUI in a fresh remote workspace, mirroring this folder.
sch run my-project --sync .

# ... brainstorm with the agent, then detach with Ctrl+] and hand off:
sch task my-project --continue "implement what we agreed and run the tests"
#> a1b2c3...          # returns immediately; laptop can go offline

# Any time later — reads S3, does not wake the microVM:
sch status my-project
#> state        : succeeded
#> checkpoint   : confirmed

# Pull the results back into your local working directory and review:
sch run my-project --sync .
```

Prefer branch-isolated autonomous work? Run parallel sessions with
`--branch`/`sch fetch` instead of `--sync`:

```sh
sch task ws-a --branch change/feature-a "implement change A per the spec"
sch task ws-b --branch change/feature-b "implement change B per the spec"
sch fetch ws-a && sch fetch ws-b   # local branches, review at your pace
```

## Before you deploy: costs and security

SCH is infrastructure in **your** AWS account. Know these five things before
`sch deploy`; the details are in [Security posture](docs/security.md).

1. **It costs money.** The runtime stack, the image in ECR, the checkpoint
   bucket, the registry table and every running session are billable. Idle
   cost is approximately zero because microVMs are destroyed on idle timeout,
   but a **shell left open keeps the microVM (and the meter) running** — run
   `sch stop <workspace>` when you are done. Tear everything down with
   `sch destroy` ([Deploying and operating](docs/deploy.md#uninstall)).
2. **Agents run unattended with auto-approved permissions** inside the
   microVM, for up to the 8-hour session lifetime. The container is the
   convenience boundary; **IAM is the real one**: whatever the runtime
   execution role allows, an agent can do.
3. **The default execution role is broad.** It carries the AWS managed
   `ReadOnlyAccess` policy (data reads included, e.g. `s3:GetObject`) and may
   invoke every Bedrock model in every region. This is the right default for a
   personal development account and the wrong one for an account holding data
   you would not show the agent. Narrow it at deploy time with
   [Runtime capability tuning](docs/runtime-capability-tuning.md), or use a
   dedicated account.
4. **Credentials stay out of the sandbox by default.** No git credentials, no
   provider API keys and no long-lived AWS keys enter the microVM; `sch fetch`
   delivers work with *local* credentials only. Per-user provider keys and
   GitHub access are opt-in, staged on tmpfs for the session and never
   checkpointed ([Provider API keys](docs/cli.md#provider-api-keys-per-user-anthropic-opencode-zengo-openrouter-kilo-bedrock)).
5. **Preview APIs and a single-account trust model.** AgentCore Runtime
   session storage is a preview feature with documented limits (1 GB per
   session, 14-day inactivity reset, reset on every runtime version); SCH's
   S3 checkpoint layer makes those non-events, but read
   [Workspaces](docs/workspaces.md) before trusting it with something
   irreplaceable. Everything assumes one AWS account you control.

## Installation

The `sch` client is a stdlib-only Python package (no dependencies, no
compilation, Python 3.8+) installed straight from the git repository:

```sh
pipx install git+https://github.com/c-daniele/sch.git        # or: uv tool install / pip install
sch setup                 # check prerequisites, obtain the support repo, report runtime-stack status
sch deploy                # first time only: bootstrap stack -> image build -> runtime stack
sch run my-workspace --sync .
```

Pin a release tag once tags exist (`...REPO.git@v0.1.0`). From a git checkout,
`./bin/sch` works without installing anything. Prerequisites for the client
are **Python 3.8+**, **AWS CLI v2**, **Node.js** (for the `agentcore` CLI and
the tunnel helpers) and, for the deploy, credentials with rights on
CloudFormation, ECR, S3, CodeBuild, IAM and Bedrock AgentCore, plus Bedrock
model access for Anthropic models in the target region (a one-time use-case
form; the most common cause of a failed first agent turn).

The full sequence — prerequisites, the one-time Bedrock form, the optional
features you can switch on and what you get by skipping them — is in
[Getting started](docs/getting-started.md). Uninstalling is two commands,
`sch destroy` (AWS side) and `sch uninstall` (local side):
[Deploying and operating](docs/deploy.md#uninstall).

## Documentation

Start at [`docs/README.md`](docs/README.md) for the full map. The most useful
entry points:

| I want to… | Read |
| --- | --- |
| Install and deploy for the first time | [Getting started](docs/getting-started.md) |
| Understand what a deploy does, feature switches, costs, teardown | [Deploying and operating](docs/deploy.md) |
| Learn the commands | [Using the `sch` CLI](docs/cli.md) |
| Run long unattended tasks and parallel branches | [Headless tasks](docs/headless-tasks.md) |
| Use the browser UI, a local TUI, or Zed | [Remote access](docs/remote-access.md) |
| Pick or switch between OpenCode, Claude Code and Pi | [Harnesses](docs/harnesses.md) |
| Get notifications and approve tools from my phone | [Telegram](docs/telegram.md) · [Telegram setup](docs/telegram-setup.md) |
| Understand where my data lives and how it survives | [Workspaces](docs/workspaces.md) |
| Shape what the agent may do in my account | [Security posture](docs/security.md) · [Runtime capability tuning](docs/runtime-capability-tuning.md) |
| See how it is built | [Architecture](docs/architecture/README.md) · [Specifications](docs/specs/README.md) |

The specifications under [`docs/specs/`](docs/specs/README.md) are normative:
when a guide and a spec disagree, the spec wins and the disagreement is a bug.
The always-current plan lives in
[`.backlog/masterplan/MASTERPLAN.md`](.backlog/masterplan/MASTERPLAN.md).

## Repository layout

```
cli/sch/        the `sch` client (Python, stdlib-only); tests in cli/tests/
bin/            sch / sch.ps1 shims (no logic) and bin/verify-*.sh end-to-end capability checks
infra/          CloudFormation (bootstrap.yaml, agent_runtime.yaml), Lambda handlers, deploy.sh
image/          the runtime container image: Dockerfile, AgentCore shim, workspace bootstrap, harness templates
tunnel/         local end of the attach/web/acp/sync transports (Node.js, invoked by sch)
docs/           guides, normative specs (docs/specs/), architecture, coding standards
.backlog/       working system: masterplan, task board, journal, decisions, brainstorming
pyproject.toml  package definition for the `sch` client
```

## Contributing, support and security

- Contributions are welcome — read [CONTRIBUTING.md](CONTRIBUTING.md) first
  (issue-first workflow, definition of done, and the policy on AI-assisted
  contributions).
- Questions and bugs: [SUPPORT.md](SUPPORT.md). Please redact account IDs and
  tokens; issues are public.
- Vulnerabilities: privately, as described in [SECURITY.md](SECURITY.md). Never
  in a public issue.
- Everyone participating agrees to the [Code of Conduct](CODE_OF_CONDUCT.md).

## License

[Apache License 2.0](LICENSE).
