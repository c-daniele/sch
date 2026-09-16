# SCH Masterplan

> **Always-current plan.** Start here — human or agent — to see where the project is.
> Updated after every completed task. Work in flight is tracked in
> [Backlog](../tasks/) (`backlog task list --plain`); durable decisions live in
> [Decisions](../decisions/) (`backlog decision list --plain`); history lives in the
> [journal](../docs/journal/) (`backlog doc list --plain`).

## Where we are

SCH is an operational serverless coding harness running AI coding agents (OpenCode, Claude Code, Pi) in disposable, checkpointed cloud microVMs on AWS Bedrock AgentCore. The core loop, S3 checkpoint durability, tunnel transports (`attach`, `acp`, `web`, `sync`), Telegram supervision, IAM workspace registries, git-native parallel sessions, linear bootstrap installation, and two-command teardown are implemented and specified. `sch handoff` accepts an opencode-only `--harness` flag. `sch task --handoff` combines local-session export, `--branch` seeding, remote import, and headless submission with implied `--continue` in one command. `sch run --continue` resumes the harness's latest session in the TUI (all harnesses, degrade-to-fresh). Both `sch task --continue` and `sch run --continue` resolve the session only after the workspace is ready, so they resume the pre-stop session even on a cold boot; `sch status` shows whether a task resumed a prior session. The runtime references its image by digest, so every deploy that rebuilds the image is a new runtime version running that build; `sch deploy -s` deploys stack changes in place. `sch list` and `sch status` show the mirror-sync local binding path alongside the git-native branch. On opencode `sch task --continue` without `--model`, the shim forwards the resumed session's stored model and reasoning-effort variant, so a headless continuation keeps the TUI selection (spec: headless-task-execution R8a). Provider availability for that forwarding counts persisted OpenCode credentials and staged API keys, not just the seeded config, so OAuth- and key-backed models are preserved rather than overridden. An explicit per-invocation effort is available headlessly via `sch task --variant <name>` (opencode only); `sch run --variant` is rejected because the pinned TUI defines no such flag.

This repository was published as a fresh history: the specifications under `docs/specs/` are the normative description of current behavior, the guides under `docs/` explain how to use and operate it, and this plan starts empty. New work begins with a Backlog task; completed work is recorded in the journal below and linked here.

## Active work

No active tasks. The TASK-1 memory-cost work (TASK-1 through TASK-1.4) is
done and recorded below; live-AWS follow-ups (billed-peak re-run, caps-on
comparison, fresh bootstrap, full `verify-l2.sh` cycle, alarm threshold)
are operator-side and named in each task summary. `backlog task list
--plain` shows the completed tasks.

## Durable decisions

Architecture decisions are recorded in [`.backlog/decisions/`](../decisions/) (`backlog decision list --plain`):

- [`decision-1`](../decisions/decision-1%20-%20Install-client-directly-from-git-repository.md): Install client directly from git repository
- [`decision-2`](../decisions/decision-2%20-%20Defer-PyPI-publication.md): Defer PyPI publication
- [`decision-3`](../decisions/decision-3%20-%20Make-CodeBuild-the-default-image-build-path.md): Make CodeBuild the default image build path
- [`decision-4`](../decisions/decision-4%20-%20Optional-features-are-deploy-time-switches-with-inert-defaults.md): Optional features are deploy-time switches with inert defaults
- [`decision-5`](../decisions/decision-5%20-%20Retire-OpenSpec-in-favor-of-domain-specs-under-docs-specs.md): Retire OpenSpec in favor of domain specs under docs/specs
- [`decision-6`](../decisions/decision-6%20-%20Use-managed-AWS-MCP-Server-via-pinned-proxy-instead-of-self-hosted-aws-api-server.md): Use managed AWS MCP Server via pinned proxy instead of self-hosted aws-api server
- [`decision-7`](../decisions/decision-7%20-%20Opt-in-GitHub-access-via-provider-keys-transport-and-tmpfs-credential-store.md): Opt-in GitHub access via provider-keys transport and tmpfs credential store
- [`decision-8`](../decisions/decision-8%20-%20Resolve-deferred-session-continuation-after-the-workspace-ready-gate-and-persist-the-outcome-in-task-status.md): Resolve deferred session continuation after the workspace-ready gate and persist the outcome in task status
- [`decision-9`](../decisions/decision-9%20-%20Reference-the-runtime-image-by-digest-every-image-building-deploy-is-a-new-runtime-version.md): Reference the runtime image by digest: every image-building deploy is a new runtime version

## Open questions

- **Disk cache invalidation**: `~/.config/sch/` runtime ARN and checkpoint bucket cache currently lacks account/region invalidation keys.
- **Error surface fidelity**: `runtime.invoke_verified` discards `stderr`, obscuring real AWS API errors.
- **Telegram webhook registration from restricted networks**: registering the webhook requires a network that reaches `api.telegram.org`; when registration fails, `deploy.sh` warns and the webhook must be re-registered from such a network, because each deploy regenerates the secret unless `TELEGRAM_WEBHOOK_SECRET` is pinned.

## Roadmap

2. **Operational hardening** — account/region cache invalidation, stderr diagnostic preservation in CLI invocation, cost budgets and alarms in deploy.
3. **Team features** — shared workspace registries across teams, multi-user onboarding, centralized image and audit management.
4. **Future extensions** — `llms.txt` for the repository, MCP server exposing SCH state, runtime module decomposition if maintenance burden requires it.

## Conventions this plan relies on

- Workflow: [AGENTS.md](../../AGENTS.md) (orient → plan → explore → implement → verify → journal + ADR + update this file).
- Specs: `docs/specs/` (start from `docs/specs/README.md`) — normative, by domain.
- Decisions: `.backlog/decisions/` (`backlog decision list --plain`).
- Exploration artifacts: `.backlog/brainstorming/YYYY-MM-DD.<IdeaTitle>/`.
- Journal: `.backlog/docs/journal/` (`backlog doc list --plain`).

## Journal

- [2026-09-14 Reduce AgentCore memory-cost footprint](../docs/journal/doc-7%20-%202026-09-14-Reduce-AgentCore-memory-cost-footprint.md) — peak-memory billing: attribution tool+report, transient-peak caps, checkpoint excludes with post-restore rebuild, lifecycle guardrails + alarm proposal (TASK-1 through TASK-1.4).

- [2026-09-14 Headless continue kept the conversation but lost the model](../docs/journal/doc-1%20-%202026-09-14-Headless-continue-kept-the-conversation-but-lost-the-model.md) — opencode `sch task --continue` now preserves the TUI-selected model and effort (TASK-2).
- [2026-09-14 Continue-model override ignored staged provider keys](../docs/journal/doc-5%20-%202026-09-14-Continue-model-override-ignored-staged-provider-keys.md) — the override now treats staged-key providers as available (TASK-3).
- [2026-09-14 Explicit reasoning effort via sch task --variant](../docs/journal/doc-6%20-%202026-09-14-Explicit-reasoning-effort-via-sch-task-variant.md) — per-invocation `--variant` on headless tasks, opencode only (TASK-4).
- [2026-09-16 Preserve authenticated OpenCode model on headless continue](../docs/journal/doc-8%20-%202026-09-16-Preserve-authenticated-OpenCode-model-on-headless-continue.md) — persisted OpenCode credentials now count when deciding whether a resumed model is available, preserving GitHub Copilot selections (TASK-5).
