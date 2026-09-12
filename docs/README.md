# SCH documentation

Three kinds of documents live here. **Guides** explain how to use and operate
SCH, by topic. **Specifications** (`specs/`) are normative: they define required
behavior, and when a guide disagrees with a spec the spec wins and the
disagreement is a bug. **History** keeps point-in-time findings for context and
is not normative.

New here? Read the [README](../README.md), then [Getting started](getting-started.md).

## Guides

| Guide | What it covers |
| --- | --- |
| [Getting started](getting-started.md) | Installing the client, prerequisites (AWS permissions, Bedrock model access, tools), the from-scratch sequence, the optional features and what skipping them means. |
| [Deploying and operating](deploy.md) | What `sch deploy` does, what creates a new runtime version, deploy-time feature switches, platform limits, lifecycle and costs, teardown with `sch destroy` / `sch uninstall`. |
| [Using the `sch` CLI](cli.md) | Command reference by example, per-user provider API keys, OpenCode session handoff, local workspace sync, Windows support. |
| [Headless tasks](headless-tasks.md) | Detached unattended tasks, choosing interactive vs headless, per-task model override, git-native parallel sessions with `--branch` and `sch fetch`. |
| [Remote access](remote-access.md) | The dashboard, `sch web` (browser UI), `sch attach` (local TUI on the remote backend), and ACP editor integration (Zed). |
| [Harnesses](harnesses.md) | OpenCode, Claude Code and Pi: capability matrix, choosing and switching, headless argv, `--continue`, Bedrock mode, seeded custom agents, development-environment contract. |
| [Workspaces](workspaces.md) | The IAM workspace registry, storage backends (`s3` / `session`), L2 durability through S3 checkpoints, persistence verification, deletion. |
| [Telegram](telegram.md) | What the notification channel does and when it fires; what remote interaction lets you do from the phone; security and rollback. |
| [Telegram setup](telegram-setup.md) | Step-by-step: bot creation, chat id, deploy variables, verification, troubleshooting. |
| [Security posture](security.md) | The boundaries, the defaults you accept, what never enters the sandbox, and how to narrow the execution role. |
| [Runtime capability tuning](runtime-capability-tuning.md) | Deploy-time shaping of the execution role: extra AI services, Bedrock model allow-list, removing `ReadOnlyAccess`, escape hatch. |
| [MCP tooling and Bedrock](mcp-and-bedrock.md) | The MCP servers and AWS CLI inside the image, the Bedrock provider configuration, execution role permissions. |
| [Image rebuild](image-rebuild.md) | Opt-in rebuild of the runtime image from inside a session through CodeBuild. |
| [Architecture](architecture/README.md) | One diagram: operator plane, runtime plane, durability plane; the session lifecycle in one line. |

## Specifications

[`specs/README.md`](specs/README.md) indexes every spec by domain (platform,
security, workspace lifecycle, access surfaces, sync and git, providers and
models) with its verification status.

## For contributors

- [Coding standards](coding-standards.md) — per-language rules and repository hygiene.
- [CONTRIBUTING.md](../CONTRIBUTING.md), [AGENTS.md](../AGENTS.md) (development workflow), the [masterplan](../.backlog/masterplan/MASTERPLAN.md).

## History

- [Implementation findings and early decisions](history/implementation-findings.md) — proof-of-concept era notes: runtime findings, open-question outcomes, preflight decisions (region, CLI, model access). Not normative.
