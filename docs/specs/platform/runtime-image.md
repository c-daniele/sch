# Runtime image

> Domain: [Platform](../README.md) · Status: Implemented · Source: rationalized from openspec specs (2026-08-28)

## Purpose

The container image for AgentCore Runtime: harnesses (OpenCode, Claude Code, Pi) at pinned versions, the entrypoint shim (health/ping on :8080, checkpoint cycle, headless tasks, notifications), canonical paths (`/mnt/workspace/repo`, `/mnt/workspace/state`), pre-installed AWS MCP tooling and workflow CLIs, and an `amazon-bedrock` provider usable out of the box. The interactive path (TUI via `sch open`) stays unchanged across extensions of the shim.

## Scope

In scope:
- Image contents and pinned versions, security posture, canonical paths, harness dispatcher, workspace initialization/seeding, shim actions and behavior, notifications and remote interaction.

Out of scope:
- IaC for the runtime and its role ([runtime-provisioning](runtime-provisioning.md)).
- In-session image rebuild flow ([session-image-rebuild](session-image-rebuild.md)).
- How harness is chosen per workspace ([harness-selection](harness-selection.md)).
- Full task lifecycle and `sch task` CLI semantics (the `headless-task-execution` capability).

## Requirements

### Runtime contract and canonical paths

**R1.** The image SHALL be built for `linux/arm64` and SHALL expose an HTTP shim on port 8080 conforming to the AgentCore Runtime contract (`BedrockAgentCoreApp`), responding to ping/health requests and to a `noop`/`info` invocation entrypoint usable to force microVM provisioning. A `noop`/`info` invocation via `InvokeAgentRuntime` on a new `runtimeSessionId` SHALL start the microVM and return an informational response (e.g. image version, workspace state) without starting persistent harness processes.

**R2.** The image SHALL define `XDG_DATA_HOME=/mnt/workspace/state/data` and `XDG_CONFIG_HOME=/mnt/workspace/state/config`, so all mutable OpenCode state (database, snapshots, config) resides under the session storage mount. The canonical working worktree path SHALL be `/mnt/workspace/repo`. OpenCode's project identity (hash of the absolute path) SHALL be identical across successive starts of the same runtime session.

### Pinned tooling in the image

**R3.** The image SHALL include AWS CLI v2 at a fixed version declared as an `ARG` (`AWS_CLI_VERSION`), available as `aws` in interactive and login shells, installed on local disk outside `/mnt/workspace`. The installer MUST use a versioned URL; the CLI MUST disable the pager and use the existing credential chain without static credentials.

**R4.** The image SHALL include OpenCode at a fixed version declared as an `ARG` (`OPENCODE_VERSION`) with its runtime dependencies (Node.js/bun) and basic tooling (`git`, `tar`, `unzip`, shell utils). It MUST NOT use floating tags (`latest`) for OpenCode. Two builds with the same `ARG` SHALL contain the same OpenCode version.

**R5.** The image SHALL include Claude Code at a fixed version declared as an `ARG` (`CLAUDE_CODE_VERSION`), alongside OpenCode, using the same Node 22 toolchain (npm-22, symlinked to `npm`); the binary SHALL be at `/usr/local/bin/claude`. No floating tags.

**R6.** The image SHALL include the development toolchain for autonomous bootstrap of project environments for the common stacks (Python, TypeScript/JavaScript, web): native build tools (`make`, `gcc`, `gcc-c++`, `python3.11-devel`) for Python packages without arm64 wheels and `node-gyp` native modules, and `jq` for JSON in shell. `uv` SHALL be installed at a fixed version declared as `ARG UV_VERSION` with a build-time assertion that the installed version matches the pin; no unpinned `uv`. The image SHALL set `UV_PYTHON_DOWNLOADS=never` as a container environment variable so `uv` uses exclusively the system `python3.11` and never downloads a managed CPython (which would land in `XDG_DATA_HOME`, on the checkpointed mount).

**R7.** The image SHALL include the MCP servers `awslabs.aws-documentation-mcp-server` (aws-docs, self-hosted) and `mcp-proxy-for-aws-cli` (aws-mcp: SigV4 proxy to the managed AWS MCP Server) at fixed versions declared as `ARG`s (`AWS_DOCS_MCP_VERSION`, `MCP_PROXY_VERSION`) plus the managed endpoint as `ARG AWS_MCP_ENDPOINT`, in isolated Python environments on the image's local disk (not under paths shadowed by the session storage mount, e.g. `/opt/uv-tools`), entrypoints in the `PATH`. Starting the servers MUST NOT require network downloads at runtime (the proxy reaches the managed endpoint over the network at tool-call time; its code is fully installed at build time).

**R8.** The image SHALL include the MCP server `@upstash/context7-mcp` (context7) at a fixed version declared as `ARG CONTEXT7_MCP_VERSION`, via `npm install -g` (same toolchain as `opencode-ai` and `@anthropic-ai/claude-code`), with the `context7-mcp` entrypoint in the `PATH` from `/usr/local/bin/` and the version exported as image `ENV` `CONTEXT7_MCP_VERSION`. Self-contained and independent of the session storage mount; no runtime npm downloads.

**R9.** The image SHALL include the OpenSpec (`@fission-ai/openspec`) and Backlog.md (`backlog.md`) CLIs at fixed versions declared as `ARG OPENSPEC_VERSION` and `ARG BACKLOG_MD_VERSION`; `openspec` and `backlog` SHALL be in the `PATH`, executable by the non-root user `sch`, installed on local disk, versions exported as image `ENV`.

### Security boundary for AWS access

**R10.** The general AWS CLI in the image SHALL be treated as a shift of the security boundary: the `aws-mcp` MCP server's `--read-only` flag MUST NOT be considered a barrier against mutations — the only enforcement is IAM (see [runtime-provisioning](runtime-provisioning.md) R10), optionally refined with the `aws:ViaAWSMCPService` / `aws:CalledViaAWSMCP` condition keys the managed server attaches.

### Bedrock provider and Claude Code environment

**R11.** With the default seeded configuration, the `amazon-bedrock` provider SHALL be registered in OpenCode (visible in the `/models` picker) and usable through the execution role credential chain with no manual configuration. The interactive shell environment MUST expose `AWS_PROFILE` and `AWS_REGION` even when the container process environment is not inherited.

**R12.** The image environment SHALL export `CLAUDE_CODE_USE_BEDROCK=1` so Claude Code uses the execution role credential chain (IMDSv2) instead of the OAuth/Anthropic Console path. Default Claude model environment variables (sonnet, opus, haiku, plus the custom model option) SHALL use the same inference profiles pinned in `image/Dockerfile` (currently: sonnet `global.anthropic.claude-sonnet-5`, opus `eu.anthropic.claude-opus-5`, haiku `eu.anthropic.claude-haiku-4-5-20251001-v1:0`), overridable via the environment. No OAuth API key or Anthropic Console token is persisted in the image.

**R13.** Bedrock mode is the default ONLY in the absence of `SCH_ANTHROPIC_API_KEY`: when present, the dispatcher switches Claude Code to the direct Anthropic API per the `provider-api-keys` capability.

**R14.** The image SHALL configure Claude Code so the "hot" write path of JSONL transcripts and user config resides on local disk (`$HOME/.claude`, env `CLAUDE_CONFIG_DIR`), NOT on the session storage mount (sidestepping the `fcntl`/`ENOLCK` hazard and the 30–60s asynchronous restore window documented for `opencode.db`). The L2 cycle ([workspace-checkpointing](../workspace-lifecycle/workspace-checkpointing.md)) SHALL replicate this state to `/mnt/workspace/state/claude` (mirror sync, read-after-write) on the periodic tick and on forced checkpoints, and restore it at boot when the session storage is empty. The Claude harness readiness marker SHALL be written only after verified replication/restore.

### Harness dispatcher

**R15.** The image SHALL expose a single dispatcher (wrapping `opencode`, `claude`, `pi`; selected via `SCH_HARNESS` from the workspace marker) that: applies the common ENV bridge (`XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `AWS_PROFILE`, `AWS_REGION`, `AWS_DEFAULT_REGION`) and harness-specific signals (`OPENCODE_DB`/`OPENCODE_ENABLE_EXA` for opencode; `CLAUDE_CONFIG_DIR`/`CLAUDE_CODE_USE_BEDROCK` for claude); applies the per-harness `SCH_*` provider-key mapping (all of them for opencode; only Anthropic for claude, with Bedrock→API switching per R13; `SCH_GITHUB_TOKEN` onto `GH_TOKEN` and `GITHUB_TOKEN` on every harness, additive with no switchover — see [provider API keys](../providers-models/provider-api-keys.md)); gates on the harness-specific readiness marker; and finally execs the real binary.

**R16.** The dispatcher MUST apply the bridge on all paths that launch a harness binary (interactive shell of any tool, headless run by the shim, init script): the minimal `bash --login` shell of `agentcore exec --it` can never launch a harness without the bridge. When the interactive login shell does not inherit the `SCH_*` variables, the dispatcher SHALL recover only the provider keys from the shim process environment, without persisting them.

**R17.** The dispatcher SHALL set `SCH_EXECUTION_MODE=interactive` for TUI launches, preserve the explicit `headless` value provided by the shim, and expose to hooks only non-sensitive paths and markers needed for spool and presence. It MUST NOT recover or propagate Telegram tokens or chat ids to harness processes.

**R18.** The shim SHALL run the selected harness headless by reusing the single dispatcher, wiring the known workaround for non-interactive execution with both MCP servers active: `stdin` redirected from `/dev/null` and harness-specific auto-approval flags on the headless argv (`--auto` for opencode, `--dangerously-skip-permissions` for claude). `SCH_EXECUTION_MODE=headless` MUST be set in the task subprocess environment only. The interactive TUI argv MUST remain unchanged (confirmation prompts preserved) and auto-approval flags MUST appear only on the headless argv, never via global env. Headless milestones (origin `headless`) SHALL bypass the presence gate.

### Workspace initialization and seeding

**R19.** The image SHALL include an `init-workspace.sh` script, executed by the shim at startup, that creates the canonical directories (`/mnt/workspace/repo`, `/mnt/workspace/state/data`, `/mnt/workspace/state/config`, `/mnt/workspace/state/claude`) if absent and seeds the selected harness's configuration only when it does not already exist. Before a fresh seed, when the workspace identity is known, the bootstrap SHALL check for an S3 checkpoint and, if present, perform the L2 restore instead of the seed. The script MUST be idempotent: repeated executions never overwrite existing state or config; the L2 restore MUST honor the same property (never overwrite pre-existing L1 content). The verify-and-retry logic (`FRESH_SETTLE_WAIT` + `seed_verified`) SHALL extend to `${REPO_DIR}/.mcp.json` as the seeded canary when harness=claude (equivalent of `opencode.json` for opencode).

**R20.** Seeded OpenCode configuration (`opencode.json`) SHALL include: the default models of the `amazon-bedrock` provider, an explicit `provider.amazon-bedrock` block (with region), the `mcp` section with `aws-docs`/`aws-mcp` enabled (`aws-mcp`: `type: local`, command `mcp-proxy-for-aws-cli` with the managed endpoint, `--metadata AWS_REGION=<seed region>`, `--read-only`, no `environment` block and no static keys), `mcp.context7` disabled (R24), and `default_agent=remote-interactive`.

**R21.** Seeded Claude Code configuration SHALL include: Bedrock linkage via `CLAUDE_CODE_USE_BEDROCK=1`; a `.mcp.json` in `${REPO_DIR}` (`/mnt/workspace/repo/.mcp.json`, excluded from versioning via `.git/info/exclude`, since Claude Code does not read `.mcp.json` from `$CLAUDE_CONFIG_DIR`) with `aws-docs`/`aws-mcp` enabled (`aws-mcp`: command `mcp-proxy-for-aws-cli` with the managed endpoint in `args`, `--metadata AWS_REGION=<seed region>`, `--read-only`, empty `env`, no static credentials) and `mcpServers.context7` disabled (R25); context7 blocked via `disabledMcpjsonServers` in `~/.claude/settings.json`; and the optional Claude Code `settings.json` in its local config root, seeded via idempotent JSON merge preserving pre-existing fields.

**R22.** Repository bootstrap SHALL support two modes for `/mnt/workspace/repo`: empty repo (`git init`) or HTTPS clone of a URL specified via a session environment variable (optional token). Robust private-credential handling is out of scope. An already-present worktree SHALL be left intact (no init, no clone).

**R23.** For every harness, the image SHALL include templates for two primary agents (`remote-interactive`, `remote-auto`) and a global brief (`AGENTS.md` for OpenCode, `CLAUDE.md` for Claude Code, Pi's global context file), seeded idempotently and per-file (never overwriting operator files) by `init-workspace.sh`. The brief SHALL describe: non-root user, absence of a Docker daemon and system privileges, maximum microVM lifetime, persistence limited to `/mnt/workspace`, IMDSv2 credentials via the execution role, IAM as the effective authorization boundary, and the read-only nature of the `aws-mcp` MCP. It SHALL also state the autonomy contract for development environments: bootstrap with image tools (`uv` for Python — `uv sync` when `pyproject.toml`/`uv.lock` are present, otherwise a project-local venv with the system interpreter, never installing into the system interpreter; `npm ci`/lockfile-first for Node/TypeScript), reproducible from the repository alone (committed lockfiles, one-command bootstrap) and project-local (`.venv/`, `node_modules/` gitignored in the worktree).

**R24.** `remote-interactive` SHALL be a primary agent for brainstorming and requirements definition with a read-only posture by default (`edit: ask`; analysis/AWS read-only commands allowed); bootstrapping development environments only on explicit operator request. `remote-auto` SHALL be the primary agent for unattended headless tasks: the `question` tool disabled, no waiting for input, result verified, assumptions recorded, Git force-push explicitly forbidden, and a missing local dependency (absent `node_modules`, uninstalled Python package) treated as bootstrap work to perform, not a blocker to report. The fresh OpenCode config SHALL select `remote-interactive` as `default_agent`. OpenCode templates SHALL be copied to `${XDG_CONFIG_HOME}/opencode/` (`AGENTS.md`, `agents/remote-interactive.md`, `agents/remote-auto.md`); seeding MUST be independent of the existence of `opencode.json` (pre-feature workspaces receive the files at next boot) and MUST NOT overwrite any existing template or `opencode.json` — even one lacking `default_agent`.

**R25.** For harness=claude, `remote-interactive` runs the main thread with an exploration/brainstorming contract, read-only posture by default and normal interactive authorization prompts; `remote-auto` runs unattended tasks WITHOUT the `AskUserQuestion` tool, requiring end-to-end execution, verification, explicit assumptions and a compact final report, and treating missing local dependencies as bootstrap work. Templates SHALL be copied to `${CLAUDE_CONFIG_DIR}` (`CLAUDE.md`, `agents/remote-interactive.md`, `agents/remote-auto.md`). On a fresh workspace, `${CLAUDE_CONFIG_DIR}/settings.json` SHALL select `remote-interactive` via the `agent` setting; on pre-existing parseable settings, `agent=remote-interactive` SHALL be added only if the `agent` key is absent. Unparseable settings and already-present templates MUST NOT be overwritten (unparseable: byte-for-byte unchanged, problem reported, bootstrap continues). Seed and merge SHALL happen before the Claude readiness marker is created, and the resulting files SHALL ride the existing Claude replication/checkpoint mechanism.

**R26.** For harness=pi, initialization SHALL seed idempotently and per-file: Pi's global settings (Bedrock provider as default with the deployment region, telemetry and version-check disabled, project trust pre-decided so no non-interactive path blocks on a trust prompt); the global context file with the microVM brief (R23); the `remote-auto`/`remote-interactive` role templates as system prompts; and the SCH extension (R27). Deleting a seeded file SHALL cause re-seeding at the next initialization; operator-modified files SHALL never be rewritten (only absent files re-seeded).

**R27.** The image SHALL provide a Pi extension (seeded into Pi's global extensions directory) covering: (1) Telegram milestones — at end of turn and for mappable events it writes an event file into the notifier's local spool, never touching the network or blocking the turn; (2) activity markers — busy keep-alive markers under the same contract as the other harnesses, so the shim watcher keeps `HealthyBusy` during interactive Pi turns. The extension MUST NOT intercept tool calls or add approval prompts (Pi keeps native behavior in every mode); its absence MUST degrade to lifecycle events only, without errors.

### OpenSpec workflow integration

**R28.** The image SHALL contain in `/app/claude-templates/` the OpenSpec artifacts for Claude Code — slash commands `commands/opsx/*.md` and skills `skills/openspec-*/SKILL.md` — generated at build time by the `openspec` CLI pinned by `ARG OPENSPEC_VERSION` (`openspec init --tools claude`) against a throwaway directory with a temporary global profile selecting ALL available workflows (12 in v1.9.0: propose, explore, new, continue, apply, update, ff, sync, archive, bulk-archive, verify, onboard). Generation MUST require no network beyond the npm install of the CLI, and the build SHALL fail explicitly if the expected set (12 commands and 12 skills) is absent. The artifacts MUST NOT be pre-copied into `/home/sch/.claude` at build time: that directory must remain empty so the L2 restore of Claude state (`_restore_claude_state`) keeps working.

**R29.** The image SHALL contain in `/app/opencode-templates/` the OpenSpec commands and skills for OpenCode, generated at build time by the same pinned CLI (`openspec init --tools opencode`) with the same complete workflow profile. The build MUST fail on a missing workflow or unexpected layout. OpenCode and Claude templates SHALL derive from the same version and workflow selection, without manual vendoring.

**R30.** For harness=claude, `init-workspace.sh` SHALL copy the templates from `/app/claude-templates/` into `${CLAUDE_CONFIG_DIR}/commands/opsx/` (exposed as `/opsx:<name>`) and `${CLAUDE_CONFIG_DIR}/skills/`, with per-file idempotency (existing files never overwritten). Seeding MUST NOT be tied to first boot and MUST NOT happen on opencode workspaces. The absence of the template directory (script run outside the image) SHALL degrade to a warning.

**R31.** For every opencode workspace, `init-workspace.sh` SHALL copy the image-baked OpenSpec commands and skills into the global scope `${XDG_CONFIG_HOME}/opencode/commands/` and `${XDG_CONFIG_HOME}/opencode/skills/`, per-file and non-destructive (only missing files created). The seed MUST happen at every bootstrap, independent of `opencode.json`, and MUST NOT mutate the worktree: no `openspec init` in the repository and no changes under `${REPO_DIR}/openspec` or `${REPO_DIR}/.opencode`; project-scoped synced artifacts (`${REPO_DIR}/.opencode/`) keep OpenCode's project-scope precedence. No OpenCode seeding on claude workspaces, and no Claude seeding on opencode workspaces.

**R32.** Workspace initialization SHALL seed, only if absent and for both harnesses, the global OpenSpec config `${XDG_CONFIG_HOME}/openspec/config.json` with `profile: custom`, the complete list of the 12 workflows, and `telemetry.noticeSeen: true` (the first-run banner must not interrupt headless flows; telemetry opt-out remains an operator choice). An existing config MUST NOT be modified in any field.

### context7 disabled by default

**R33.** With the default seeded OpenCode configuration, `mcp.context7` SHALL be present in `opencode.json` with `enabled: false`, `type: "local"`, and `command` invoking the `context7-mcp` binary directly (no `npx`). The binary MUST be started by the harness only after the user sets `enabled: true` (or removes the field). A user change survives restarts (init-workspace.sh never overwrites an existing `opencode.json`).

**R34.** With the default seeded Claude configuration, `mcpServers.context7` SHALL be present in `${REPO_DIR}/.mcp.json` with `command: "context7-mcp"` and `env: {}`, and `"context7"` SHALL be in the `disabledMcpjsonServers` list of `~/.claude/settings.json`. The Claude Code loader MUST NOT connect until the user removes `"context7"` from `disabledMcpjsonServers` (or moves it to `enabledMcpjsonServers`). Seeding MUST use exclusively the official `disabledMcpjsonServers` key (NOT a hypothetical per-server `"disabled": true` field in `.mcp.json`, which is not part of the schema). The merge into an existing `settings.json` SHALL add only `disabledMcpjsonServers: ["context7"]`, preserving all other fields; an entry already present is a no-op (no duplication, no reordering).

### Shim: presence, checkpointing, actions

**R35.** The shim SHALL maintain an in-process registry of presence leases received from `sch run` and `sch shell` clients, indexed at least by `shellId` and `attachmentId`. The update action SHALL be idempotent, validate identifiers and TTLs within bounded limits, and support attach, renewal, and selective detach (detaching one attachment keeps the others). Aggregate state SHALL be `attached` if at least one lease has not expired, `detached` otherwise. The registry MUST auto-expire stale leases, have bounded size and retention, and publish an atomic snapshot on the ephemeral filesystem, readable by hooks without network access and without Telegram credentials. Absent, corrupted, or expired state MUST be equivalent to `detached` (fail open, without blocking the harness turn). The `_INTERACTIVE_ACTIVE` toggle MUST remain semantically distinct from client presence.

**R36.** The shim SHALL integrate the L2 checkpoint cycle toward S3 (periodic and synchronous on request) into the existing process, reusing the `checkpoint` action channel via `/invocations`. Periodic and synchronous checkpoints MUST be serialized (never concurrent). Checkpointing state (last outcome, timestamp, workspace identity if known) SHALL be observable via the `info` action. With the checkpoint interval set to `0`, behavior SHALL degrade to Phase 0 (db backup → mount only, no S3 upload), without redeploying the infrastructure.

**R37.** The shim SHALL handle a `task` action via `/invocations` with a never-blocking entrypoint: it receives `{prompt, continue, timeout_s, harness, session_id_hint?}`, starts the selected harness headless (`opencode run` / `claude -p`) in a background thread, registers the task via the AgentCore SDK's `add_async_task` (so `/ping` responds `HealthyBusy` for the entire task duration), and returns immediately with a `task_id`. It MUST NOT block the HTTP response waiting for completion; on completion (any outcome) it invokes `complete_async_task` so `/ping` returns to `Healthy`. The task slot is single per microVM: a second submission while occupied is refused with `status: busy` and the in-progress `task_id`. The shim SHALL refuse with an explicit error a `task` action whose `harness` differs from the workspace marker (see [harness-selection](harness-selection.md)).

**R38.** The shim's `info` action SHALL report, besides checkpointing state, the current task: when active — `task_id`, `state=running`, `prompt`, `harness`, `started_utc`, `heartbeat_utc`; when the slot is free — the last terminated task (outcome, `finished_utc`, `exit_code`, `harness`) as an observability surface. `info` is optional for `sch status` (offline-first on S3): it enriches but MUST NOT be the only source of truth for the outcome.

**R39.** The shim SHALL expose an advisory, fire-and-forget `mark-interactive` action set by `sch open` (TUI opens) and `sch stop` (TUI closes). The `task` action SHALL consult it to emit a double-writer `WARNING` (see `headless-task-execution`) but MUST NOT block submission: the flag is advisory, its absence is not a guarantee of non-concurrency, and its loss (unrehydrated after restart) degrades to the pre-warning behavior.

**R40.** The shim SHALL handle a `session-import` action via `/invocations` that imports into the workspace's OpenCode session store a session previously uploaded to the bundle staging (`STATE_DIR/bundles/handoff.json`). The action SHALL: wait for workspace readiness with the same gate as other mutating actions; fail with an explicit error if the staged file is absent; import via the OpenCode CLI (`opencode import`) with cwd in the repo worktree and the standard harness environment, without writing session structures into the db itself (the only allowed exception is the recency bump below, limited to the timestamp); guarantee last-write-wins when the session id already exists (re-remove-and-reimport if the CLI does not update in place), reporting `reimported` in the response; guarantee the imported session is the workspace's most-recently-updated (explicit timestamp bump only if the import does not already produce it); complete the durable backup of `opencode.db` (db-only checkpoint path, including durable storage on the active backend) before responding ok; delete the staged file after a successful import and backup (on failure it SHALL remain in staging for diagnosis); and return the imported sessionID, the remote OpenCode version, an empty/uninitialized-worktree indicator, and the `reimported` flag. An older image without the action SHALL produce the usual "unknown action" error listing supported actions, on which the client applies its readable fail-fast.

### Notifications and remote interaction

**R41.** The shim SHALL include a notifier component that, when the Telegram configuration is present, collects events from two sources — shim-known lifecycle transitions (submit, heartbeat, terminal state, shutdown) and milestone events deposited by harness hooks in a local spool dir — and publishes to the Telegram API from a dedicated thread. Before enqueueing an interactive milestone, the notifier SHALL evaluate its origin and timestamp against the bounded presence history: if at least one lease was valid at emission time it MUST delete the file without enqueueing; otherwise enqueue normally. The decision MUST be final and independent of subsequent attaches/detaches. Task lifecycle, headless milestones, inbound replies, actionable errors and administrative events MUST bypass the gate. The notifier MUST follow the checkpoint-loop pattern (daemon thread, errors logged, never propagated to primary flows) and SHALL attempt a best-effort final flush of pending terminal notifications on orderly shutdown.

**R42.** `init-workspace.sh` SHALL seed the harnesses' milestone mechanisms from the same image template dirs used for the agents: for claude, hooks (`Stop`, `Notification`, `PostToolUse` limited to the todo list, `PreToolUse`) in the `settings.json` of `CLAUDE_CONFIG_DIR` via additive merge; for opencode, a plugin in the seeded config's plugins directory; for pi, the SCH extension (R27). Seeding MUST be idempotent and non-destructive (operator hooks/plugins/extensions untouched, no pre-existing keys lost). The hooks SHALL enable themselves via a non-sensitive local marker created by the shim when the notifier is configured, MUST include in events at least origin (`interactive|headless`), a timestamp, and the harness source, and MUST write only to the local spool dir. They MUST be silent no-ops when the marker is absent and MUST NOT read, request, or propagate Telegram tokens and chat ids.

**R43.** The shim notifier SHALL poll the workspace's command queue at low frequency while an interactive session or task is active, applying each command per its type (approval decision, text injection, follow-up) and confirming consumption so it is not reapplied. Polling MUST be absent when the inbound channel is not configured, degrade without fatal errors when the queue is unreachable, and each consumption and its effects SHALL be traced in the runtime logs.

**R44.** The shim SHALL provide a local broker (rendez-vous) between the OpenCode/Claude permission hooks and remote decisions. The hook MUST consult the presence snapshot before registering: with at least one valid lease it SHALL immediately return control to the native prompt without creating broker files; when detached it SHALL register the request (unique id, tool description), wait with a configured maximum timeout, and receive exactly one of `approve`, `deny`, `timeout`. The notifier MUST re-check presence before publishing a request and resolve it as native fallback if a client attached in the meantime (atomically marking it resolved-elsewhere; a later Telegram decision is ignored). The broker MUST accept at most one decision per request. Hooks SHALL return `approve`/`deny` for valid remote decisions and, on timeout/fallback/reconnect, return control to native behavior without altering its decision. Pi MUST NOT use the broker.

**R45.** The shim SHALL be able to inject a user message into the most recent session of the `opencode serve` backend it already supervises, starting the backend if the workspace is opencode and it is inactive but required, and reporting an actionable error when injection is not possible. Injection MUST be confined to the opencode harness; for claude the shim MUST refuse with the outcome defined by the `telegram-interaction` contract.

**R46.** The image SHALL include the GitHub CLI (`gh`) at a fixed version declared as an `ARG` (`GH_VERSION`), installed from the versioned upstream release tarball with a build-time assertion that the installed version matches the pin; no floating downloads. Authentication comes exclusively from the per-session staged token (`GH_TOKEN`/`GITHUB_TOKEN`); no auth state is ever baked or persisted in the image.

**R47.** AgentCore bills memory on the session's peak second, so the image SHALL
bound transient peaks by default on every harness launch path (interactive
dispatcher, headless task subprocess, serve supervisor): `NODE_OPTIONS` SHALL
carry `--max-old-space-size=<SCH_NODE_HEAP_MB>` (default `1792`) and the build
parallelism SHALL default to `SCH_BUILD_JOBS=2`, fanned out to `MAKEFLAGS`,
`CMAKE_BUILD_PARALLEL_LEVEL`, `CARGO_BUILD_JOBS` and restated as
`SCH_BUILD_JOBS` for repo runners (single source of truth: `_apply_memory_caps`
in `image/app/main.py`, mirrored in `image/scripts/harness-wrapper.sh`).
Precedence: an operator value already present (e.g. a larger heap for a
known-big build) MUST win; `SCH_NODE_HEAP_MB=0` MUST disable the heap cap; an
unparseable value MUST fall back to the default, never to uncapped. Deploy-time
defaults SHALL be the `NodeHeapMb`/`BuildJobs` stack parameters (see
[runtime-provisioning](runtime-provisioning.md)); per-workspace overrides are
plain env and MUST NOT require a redeploy. A headless task that dies of memory
pressure (V8 heap message, or exit 134/137 with an OOM/killed signature) MUST
fail with the remediation naming both knobs, never as a bare non-zero exit.

## Behavior

Pinned versions (single source of truth: `image/Dockerfile` `ARG`s; current values at rationalization time):

| Tool | ARG | Pinned |
| --- | --- | --- |
| OpenCode | `OPENCODE_VERSION` | 1.18.26 |
| Claude Code | `CLAUDE_CODE_VERSION` | 2.1.258 |
| Pi | `PI_VERSION` | 0.84.4 |
| AWS CLI | `AWS_CLI_VERSION` | 2.36.8 |
| uv | `UV_VERSION` | 0.12.5 |
| aws-docs MCP | `AWS_DOCS_MCP_VERSION` | 1.1.30 |
| aws-mcp proxy | `MCP_PROXY_VERSION` | 1.6.5 |
| aws-mcp endpoint | `AWS_MCP_ENDPOINT` | `https://aws-mcp.eu-central-1.api.aws/mcp` |
| context7 MCP | `CONTEXT7_MCP_VERSION` | 3.2.3 |
| Claude ACP adapter | `CLAUDE_ACP_VERSION` | 0.59.0 |
| OpenSpec | `OPENSPEC_VERSION` | 1.9.0 |
| Backlog.md | `BACKLOG_MD_VERSION` | 1.50.1 |
| GitHub CLI | `GH_VERSION` | 2.100.0 |

Typical flows:
- `sch open myws` → shim warms up (`noop`), seed-or-restore runs, readiness marker written, TUI launches through the dispatcher with the ENV bridge and `SCH_EXECUTION_MODE=interactive`.
- `sch task myws --continue "..."` → `task` action returns a `task_id` sub-second, `/ping` is `HealthyBusy` until completion, `info` exposes the running task, Telegram receives the terminal event.
- A headless task killed by memory pressure → terminal state carries the OOM remediation (`SCH_NODE_HEAP_MB`/`SCH_BUILD_JOBS`); raising the caps re-prices the billed peak.
- MicroVM restart on empty session storage with an existing S3 checkpoint → L2 restore instead of fresh seed; restored config is the user's, not the default.
- Interactive milestone while a client is attached → suppressed at the spool; emitted detached → delivered even if a client reconnects before sending.

## Invariants

- **I1.** Two builds of the image with the same `ARG` values produce identical versions of every pinned tool (AWS CLI, OpenCode, Claude Code, Pi, uv, MCP servers, OpenSpec, Backlog.md, gh).
- **I2.** No harness binary can be launched in the microVM without the dispatcher ENV bridge; no harness process ever receives Telegram tokens or chat ids.
- **I3.** All mutable OpenCode state lives under `/mnt/workspace/state/*`; all Claude Code hot state lives on local disk and is replicated to `/mnt/workspace/state/claude` before the readiness marker.
- **I4.** Seeding is always per-file idempotent and non-destructive: operator-modified config, agent templates, hooks, and OpenSpec files survive every boot byte-for-byte; deleting a seeded file causes re-seeding.
- **I5.** The shim never blocks an HTTP response on a task's completion, never runs two checkpoint cycles concurrently, and never runs two harnesses or two tasks concurrently on one workspace/session.
- **I6.** `/ping` returns `HealthyBusy` for the entire duration of an active headless task and `Healthy` when the slot is free.
- **I7.** Absent/corrupted/expired presence state is treated as detached, never as a blocker.
- **I8.** Build-time artifacts land only in `/app/*-templates/`; `/home/sch/.claude` is empty at build time.
- **I9.** No harness, build, or headless task process in the microVM runs without the R47 caps unless the operator explicitly disabled or overrode them; caps never silently vanish (typo falls back to the default).

## Cross-references

- [runtime-provisioning](runtime-provisioning.md) — runtime, role, bucket and rebuild capability the image assumes.
- [session-image-rebuild](session-image-rebuild.md) — the rebuild command shipped in the image `PATH`.
- [harness-selection](harness-selection.md) — where `SCH_HARNESS` and the workspace marker come from.
- [workspace-checkpointing](../workspace-lifecycle/workspace-checkpointing.md) — L2 cycle, `state/claude` mirror, restore semantics.
- [headless-task-execution](../access-surfaces/headless-task-execution.md) — Headless task lifecycle and execution contract.
- [provider-api-keys](../providers-models/provider-api-keys.md) — Provider-key mapping and Anthropic API switching.
- [telegram-notifications](../access-surfaces/telegram-notifications.md) — Notification pipeline and milestone delivery.
- [telegram-interaction](../access-surfaces/telegram-interaction.md) — Inbound command queue and remote permission broker.
- [MANIFESTO](../../../MANIFESTO.md)
- Code: `image/Dockerfile`, `image/app/main.py` (shim), `image/app/telegram_notifier.py`, `image/scripts/init-workspace.sh`, `image/scripts/harness-wrapper.sh` (dispatcher), `image/scripts/sch-build-image.sh`, `image/claude-templates/`, `image/opencode-templates/`, `image/pi-templates/`, `bin/verify-multi-harness.sh`, `bin/verify-l2.sh`, `bin/verify-headless-tasks.sh`
- Memory-cost footprint: `image/app/test_memory_footprint.py`, `bin/mem-trace.sh`, [memory peak attribution](../../history/memory-peak-attribution.md)
