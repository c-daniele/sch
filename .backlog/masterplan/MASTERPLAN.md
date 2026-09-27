# SCH Masterplan

> **Always-current plan.** Start here — human or agent — to see where the project is.
> Updated after every completed task. Work in flight is tracked in
> [Backlog](../tasks/) (`backlog task list --plain`); durable decisions live in
> [Decisions](../decisions/) (`backlog decision list --plain`); history lives in the
> [journal](../docs/journal/) (`backlog doc list --plain`).

## Where we are

SCH is an operational serverless coding harness running AI coding agents (OpenCode, Claude Code, Pi) in disposable, checkpointed cloud microVMs on AWS Bedrock AgentCore. The core loop, S3 checkpoint durability, tunnel transports (`attach`, `acp`, `web`, `sync`), Telegram supervision, IAM workspace registries, git-native parallel sessions, linear bootstrap installation, and two-command teardown are implemented and specified. `sch handoff` accepts an opencode-only `--harness` flag. `sch task --handoff` combines local-session export, `--branch` seeding, remote import, and headless submission with implied `--continue` in one command. `sch run --continue` resumes the harness's latest session in the TUI (all harnesses, degrade-to-fresh). Both `sch task --continue` and `sch run --continue` resolve the session only after the workspace is ready, so they resume the pre-stop session even on a cold boot; `sch status` shows whether a task resumed a prior session. The runtime references its image by digest, so every deploy that rebuilds the image is a new runtime version running that build; `sch deploy -s` deploys stack changes in place. `sch list` and `sch status` show the mirror-sync local binding path alongside the git-native branch. On opencode `sch task --continue` without `--model`, the shim forwards the resumed session's stored model and reasoning-effort variant, so a headless continuation keeps the TUI selection (spec: headless-task-execution R8a). Provider availability for that forwarding counts stored OpenCode credentials and staged API keys, not just the seeded config, so OAuth- and key-backed models are preserved rather than overridden. An explicit per-invocation effort is available headlessly via `sch task --variant <name>` (opencode only); `sch run --variant` is rejected because the TUI defines no such flag.

The image runs **OpenCode 2** (2.0.18, npm package `@opencode/cli`), Pi 0.87.1 and Claude Code 2.1.282 (TASK-7). OpenCode 2 is a client/server release; SCH keeps its 1.x process topology (decision-10): TUI and headless tasks run `--standalone`, and the shim supervises one authenticated `opencode serve` on :4096 for `sch attach`, `sch web` and Telegram injection, with a per-microVM password minted by the shim and handed to clients out of band of the argv. The headless argv is `opencode run --standalone [--session] [--model provider/model#variant] [--agent] --auto -- <prompt>`; `sch run --model` reaches the TUI through `OPENCODE_CONFIG_CONTENT`; the Telegram plugin uses the 2.x plugin API. Sessions live in `session_v2`, credentials in the `credential` table; 1.x sessions are not migrated (fresh installation). Operators need OpenCode 2 locally for `sch attach`/`sch handoff`. OpenCode 2 sends Bedrock no output cap unless configured (Bedrock then caps Claude at 4096 tokens), so the seeded `opencode.json` declares `providers.amazon-bedrock` in the native V2 shape (never mixed with the V1 `provider` block, which OpenCode would then drop) with a per-model `inferenceConfig.maxTokens` for the default model and the main global/EU Claude profiles (TASK-9, decision-12; fresh workspaces only).

The first-boot clone keeps `SCH_REPO_TOKEN` out of the workspace (TASK-10, runtime-image R22): the token reaches git through an environment-reading credential helper, `origin` is token-free, and each boot scrubs embedded passwords from the `origin` of worktrees cloned by earlier images. Checkpoints taken before the fix still hold the token, so the guides tell affected users to rotate it.

The post-restore environment rebuild is lockfile-only and never changes the repository (TASK-21, decision-13, workspace-checkpointing R21): `npm ci`, `uv sync --frozen`, or a project-local `.venv` for `requirements.txt`; projects without a lockfile are skipped with `no-lockfile`, and a guard reverts any stray repo change. Optional extras and custom install commands are not reproduced. The outcome is in shim `info` (`checkpoint.env_rebuild`) and `sch status --live`.

This repository was published as a fresh history: the specifications under `docs/specs/` are the normative description of current behavior, the guides under `docs/` explain how to use and operate it, and this plan starts empty. New work begins with a Backlog task; completed work is recorded in the journal below and linked here.

## Active work

TASK-20 (per-user workspace isolation, runtimes provisioned at deploy time) is in progress on `feat/task-20`, one headless subtask at a time. TASK-20.1 (design) is done: decision-14 and the Proposed spec [`per-principal-isolation.md`](../../docs/specs/security/per-principal-isolation.md), which is the plan of record for TASK-20.2 (storage and shim), TASK-20.3 (plane stacks and deploy), TASK-20.4 (registry and CLI) and TASK-20.5 (verify scripts and docs). TASK-20.2 is done: a runtime with `SCH_OWNER_PREFIX` writes every object under its owner segment, rejects payloads claiming another owner, writes nothing into the root before the boot restore, and `sch-build-image` and the task watchdog handle the nested layout; runtimes without the variable are unchanged, and task prompts are now kept out of the runtime logs in every mode. TASK-23 tracks the same early-checkpoint write on isolation-off runtimes. TASK-20.3 is done: `ISOLATED_PRINCIPALS` makes `infra/deploy.sh` resolve each listed principal with IAM reads (preflight `infra/isolation_plan.py`, before any stack changes) and deploy one plane stack per principal (`infra/user_plane.yaml`: locked runtime, bounded execution role, read-only access role, plane-mapping parameter); the runtime stack's shared permissions are now customer managed policies in every mode, and with isolation on it adds the tag-based bucket policy and locks the shared runtime; `sch destroy` removes planes first. Access Analyzer and simulator evidence are reproducible with `infra/validate_isolation_policies.py` and `infra/simulate_isolation.py` (reports under `docs/history/`). Until TASK-20.4 (registry mapping, CLI) lands, the planes exist but no `sch` command uses them, so registry mode still separates names only and a deployment is one trust domain (the guides say so); do not enable `ISOLATED_PRINCIPALS` on a live stack before TASK-20.4, because it locks the shared runtime. The first deploy of the managed-policy template may give running sessions a few seconds of AccessDenied while the inline policy is replaced. The live two-principal check is operator-side. TASK-7 (harness pins: OpenCode 2.0.18, Pi 0.87.1, Claude
Code 2.1.282) is done and recorded below; its live-AWS follow-ups — first
deploy of the OpenCode 2 image, Bedrock through the execution role on 2.x,
the Telegram plugin end to end, `bin/verify-remote-ui-tunnel.sh` /
`verify-handoff.sh` / `verify-headless-tasks.sh`, and the memory profile of
`opencode serve` under AgentCore billing — are operator-side and named in the
task summary. TASK-9 (seeded Bedrock output caps) is done; its image build with
`image/test-local.sh` and a live Bedrock call on a listed model join those
operator-side follow-ups. TASK-10 (clone token kept out of the workspace)
is done; its in-image run (`image/test-local.sh`) and a live clone of a real
private repository are operator-side too. TASK-21 (repo-neutral post-restore env rebuild) is done; a cold restore of a real Node/Python workspace in a live microVM is its operator-side check. TASK-22 tracks seven image-side
tests that fail when the suite runs outside the container. Earlier follow-ups from TASK-1 (billed-peak re-run, caps-on
comparison, fresh bootstrap, full `verify-l2.sh` cycle, alarm threshold)
remain operator-side too. `backlog task list --plain` shows the completed tasks.

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
- [`decision-10`](../decisions/decision-10%20-%20Keep-the-V1-process-topology-on-OpenCode-2-standalone-TUI-and-tasks-one-supervised-authenticated-serve-for-attach-web-and-injection.md): Keep the V1 process topology on OpenCode 2: standalone TUI and tasks, one supervised authenticated serve for attach, web and injection
- [`decision-12`](../decisions/decision-12%20-%20Seed-an-explicit-Bedrock-output-cap-per-Claude-model-in-the-native-OpenCode-2-provider-shape.md): Seed an explicit Bedrock output cap per Claude model in the native OpenCode 2 provider shape
- [`decision-13`](../decisions/decision-13%20-%20Post-restore-env-rebuild-is-lockfile-only-and-never-changes-the-repository.md): Post-restore env rebuild is lockfile-only and never changes the repository
- [`decision-14`](../decisions/decision-14%20-%20Per-principal-isolation-planes-are-provisioned-at-deploy-time-one-CloudFormation-stack-per-listed-principal.md): Per-principal isolation planes are provisioned at deploy time, one CloudFormation stack per listed principal

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
- [2026-09-26 Upgrade harness pins: OpenCode 2.0.16, Pi 0.87.1, Claude Code 2.1.282](../docs/journal/doc-10%20-%202026-09-26-Upgrade-harness-pins-OpenCode-2.0.16-Pi-0.87.1-Claude-Code-2.1.282.md) — the OpenCode 2 port: new npm package, authenticated `serve`, `--server` client, `session_v2`/`credential` schema, V2 plugin API, V1 topology kept (TASK-7, decision-10).
- [2026-09-26 Bump the OpenCode pin to 2.0.18](../docs/journal/doc-11%20-%202026-09-26-Bump-the-OpenCode-pin-to-2.0.18.md) — the maintainer pointed at the newest 2.x release; the pin lands on 2.0.18 (surface-identical to 2.0.16, verified) and the `@opencode/ai` vs `@opencode/cli` package confusion is resolved (TASK-7).
- [2026-09-26 Bedrock Claude requests capped at 4096 output tokens on OpenCode 2](../docs/journal/doc-13%20-%202026-09-26-Bedrock-Claude-requests-capped-at-4096-output-tokens-on-OpenCode-2.md) — OpenCode 2 sends no Bedrock output cap by default; the seed now sets one per Claude model in the native V2 provider shape (TASK-9, decision-12).
- [2026-09-27 Keep the clone token out of the workspace](../docs/journal/doc-14%20-%202026-09-27-Keep-the-clone-token-out-of-the-workspace.md) — `SCH_REPO_TOKEN` no longer lands in the clone's `origin` URL or on a command line; earlier workspaces are scrubbed at boot, old checkpoints need token rotation (TASK-10).
- [2026-09-27 Post-restore env rebuild no longer changes the repository](../docs/journal/doc-15%20-%202026-09-27-Post-restore-env-rebuild-no-longer-changes-the-repository.md) — lockfile-only rebuild into project-local envs, `no-lockfile` skips, a worktree guard, and a reported per-env outcome (TASK-21, decision-13).
- [2026-09-27 Isolation design: decision record and normative spec](../docs/journal/doc-16%20-%202026-09-27-Isolation-design-decision-record-and-normative-spec.md) — defaults of TASK-20 fixed, per-principal isolation spec written, early Access Analyzer and simulator checks of the lock and bucket policies (TASK-20.1, decision-14).
- [2026-09-27 Isolation: owner-scoped storage and shim](../docs/journal/doc-17%20-%202026-09-27-Isolation-owner-scoped-storage-and-shim.md) — owner prefix only from the runtime environment, owner-segment keys, no root writes before the restore on plane runtimes, prompts out of the logs, watchdog on both layouts; follow-up TASK-23 (TASK-20.2).
- [2026-09-27 Isolation: per-principal plane stacks and deploy](../docs/journal/doc-18%20-%202026-09-27-Isolation-per-principal-plane-stacks-and-deploy.md) — managed shared policies, plane template, preflight and per-principal stacks in deploy.sh, plane teardown, Access Analyzer and simulator evidence (TASK-20.3).
