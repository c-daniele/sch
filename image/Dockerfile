# SCH Phase-0 runtime image — OpenCode inside Bedrock AgentCore Runtime.
#
# Build (arm64 is REQUIRED by AgentCore Runtime):
#   docker buildx build --platform linux/arm64 -t sch-runtime:dev image/
#
# OpenCode, and the AWS MCP servers below, are PINNED via ARG (see design D5):
# a runtime version update resets the session storage of every session, so
# upgrading any of them must be a deliberate choice, never a floating tag.
# v9: adds OPENCODE_ENABLE_EXA=1 (native websearch/codesearch tools, Exa-
# backed) — needed explicitly since this image's default provider is
# amazon-bedrock, not the hosted "opencode" provider.
# v8: fixes `agentcore exec --it` login shells not inheriting XDG_CONFIG_HOME/
# XDG_DATA_HOME/OPENCODE_DB (root cause of "no MCP tools" in the interactive
# shell — see scripts/opencode-wrapper.sh and /etc/profile.d/sch-env.sh).
# v9: OPENCODE_ENABLE_EXA=1 (native websearch/codesearch tools, Exa-backed).
# v10: fixes a mount-attach race in app/main.py — hint="fresh" used to skip
# ALL settling before seeding the workspace, so a fast interactive user could
# read /mnt/workspace/state/config/opencode/opencode.json (or repo/) before
# the real session-storage volume attached, seeing it as missing/empty even
# though init-workspace.sh had already reported success against whatever was
# mounted at that instant. Adds FRESH_SETTLE_WAIT + a verify-after-write
# retry loop (_verify_workspace_seeded) around init-workspace.sh.
# v11: fixes a timeout mismatch that reintroduced the same symptom for
# hint="resumed" sessions (sent by `sch shell` on any workspace name already
# known locally, e.g. a retry): the shim's mount-classification step alone
# can legitimately take the full RESUME_WAIT (180s) before declaring
# "resume-hinted-but-empty" and proceeding to seed — but
# scripts/opencode-wrapper.sh only waited SCH_OPENCODE_WAIT=90s for the
# readiness marker before giving up and starting OpenCode anyway (with an
# easy-to-miss stderr warning) against a not-yet-seeded workspace. Raised the
# wrapper's default wait to 220s, comfortably above RESUME_WAIT.
# v12: pre-downloads the embedding model used by awslabs.aws-api-mcp-server's
# knowledge base (BAAI/bge-base-en-v1.5, ~440MB) into a layer of the image.
# Without this, the FIRST `opencode` launch inside a freshly-provisioned
# microVM (i.e. a new runtimeSessionId / new `sch shell <name>`) blocks for
# minutes while the MCP server downloads the model from huggingface.co into
# the empty local-disk HF cache (~/.cache/huggingface, NOT on the session
# storage mount, so the cache is per-microVM and lost on every cold start).
# The download happens at BUILD time here so every microVM starts warm; the
# cost is ~440MB of image size. We pull only pytorch_model.bin + the small
# config/tokenizer files (ignore onnx/openvino/flax/safetensors variants)
# to keep the layer minimal — sentence-transformers uses pytorch_model.bin
# when present and does not re-fetch safetensors at runtime.
# v13: (1) harness/binary binding is now ENFORCED by the dispatcher — invoking
# the binary of the OTHER harness on a bound workspace fails fast with an
# explicit error instead of soft-gating (wait 220s, then launch anyway);
# (2) `sch run` support: /etc/profile.d/sch-run.sh consumes a run-once marker
# armed by the shim (action=prepare-run) and exec's the workspace's harness
# from /mnt/workspace/repo — quitting the harness closes the shell session;
# (3) ANTHROPIC_DEFAULT_OPUS_MODEL pinned so all Claude Code model aliases
# (opus/sonnet/haiku) resolve through Bedrock inference profiles.
# v14: Claude Code bumped 2.1.0 -> 2.1.210 — the /model picker entries are
# hardcoded per-binary-version (2.1.0 predates Opus 5 / Sonnet 5 / Fable 5),
# so env pins alone could not surface the current lineup. Model aliases now
# pin: fable -> global.anthropic.claude-fable-5, opus -> eu.anthropic.
# claude-opus-5, sonnet -> global.anthropic.claude-sonnet-5, haiku ->
# eu.anthropic.claude-haiku-4-5 (+ _NAME display labels), and Opus 5
# (Global) is added as the single ANTHROPIC_CUSTOM_MODEL_OPTION picker row
# (one alias slot per family; the second Opus variant rides the custom slot).
# v15: sch-context7-builtin — `@upstash/context7-mcp` is now installed in the
# image as a THIRD MCP server (`context7-mcp` binary in `/usr/local/bin/`,
# version pinned via ARG). Built-in but DISABLED by default on both
# harnesses: OpenCode uses the official flag `mcp.context7.enabled = false`
# (https://opencode.ai/docs/mcp-servers/), while Claude Code uses
# `disabledMcpjsonServers: ["context7"]` in `settings.json` (Claude Code's
# official key, NOT a hypothetical per-server `"disabled": true`). Enabling
# it is an explicit operator choice via a config edit; no implicit toggles,
# no cold-start network requests.
# v16: sch-context7-builtin follow-up fix (design D8, surfaced in task 6.3) —
# the Claude `.mcp.json` file used to be seeded at `${CLAUDE_CONFIG_DIR}/.mcp.json`
# (`~/.claude/.mcp.json`), a path Claude Code NEVER reads for project-scoped
# config (confirmed by code.claude.com/docs/en/mcp and by a live test:
# `claude mcp list` showed NO servers at all, not just missing context7).
# Fix: `.mcp.json` now lives at `${REPO_DIR}/.mcp.json` (project root, the only
# path Claude Code reads), seeded AFTER the repo bootstrap in
# init-workspace.sh, with an idempotent entry in `.git/info/exclude` to keep
# `git status` clean. `settings.json` stays in `CLAUDE_CONFIG_DIR`
# (unaffected — `disabledMcpjsonServers` works correctly there).
# Pre-existing bug from `sch-multi-harness`, not introduced by that change,
# but without the fix the context7 disable mechanism was not verifiable
# (no MCP server, disabled or not, was ever loaded by Claude Code).
# v17: sch-remote-ui-tunnel — adds `@agentclientprotocol/claude-agent-acp`
# (pinned, npm install -g, same shape as context7-mcp) so `sch acp` has an
# ACP adapter for harness=claude; adds a build-time assert that the pinned
# OpenCode exposes `serve`/`acp` with the expected CLI surface (verified at
# design time against 1.18.3: it does, plus `opencode attach <url>` for `sch
# attach`); adds a `@app.websocket` handler (app/main.py, app/tunnel_framing.py)
# implementing the remote byte-bridge end for both `sch attach` and `sch acp`
# over `InvokeAgentRuntimeWithWebSocketStream` (distinct from the interactive
# shell channel `sch shell`/`sch open`/`sch run` use — no shared concurrency
# budget, see design.md D9/"Nota di revisione"); adds the `serve-ensure` shim
# action. No new Python dependency: `bedrock-agentcore>=1.4.7,<2` (already
# pinned) already exposes `@app.websocket`. No changes to the dispatcher,
# init-workspace seeding, or L2 checkpoint — the tunnel reuses `opencode
# serve`/`opencode acp` through the existing harness-wrapper.sh dispatcher
# unchanged.
# v18: sch-remote-ui-tunnel live-verification fix — the tunnel `@app.websocket`
# handler used to send framed output with a single `send_text` per
# `reader.read()` (up to 64KB raw), but `send_data` base64-expands that into
# ~87KB of concatenated framing lines, exceeding AgentCore's 64KB
# per-WebSocket-frame limit; oversized frames are silently dropped end-to-end,
# so any response larger than ~48KB stalled (e.g. `opencode serve` /doc = 467KB,
# and the TUI's initial state load — `sch attach` hung on a blank screen). Fix:
# send ONE framing line per WebSocket message (each <64KB by construction), in
# both `_TunnelSession.send_or_buffer` and the pending-output flush on reconnect
# (app/main.py `_iter_ws_frames`); mirrored on the local bridge in
# tunnel/transport.js. No wire-format change (the receiver already reassembles
# regardless of message boundaries). See design.md D3 "Post-live-verification fix".
# v19: sch-remote-ui-tunnel live-verification fix #2 — the real AgentCore
# WebSocket per-frame limit is 32KB, NOT 64KB as originally assumed (confirmed
# via runtime-get-started-websocket.html: "stay below the 32KB frame size
# limit"; frames over it make AgentCore close the connection). MAX_CHUNK was
# 45000 raw -> ~60KB base64 per frame, well over 32KB, so large transfers
# triggered a 1006 close/reconnect storm that stalled `sch attach`'s TUI on a
# blank screen (the initial state load never completed). Fix: MAX_CHUNK 45000
# -> 20000 (~26.7KB base64/frame, safely under 32KB) in app/tunnel_framing.py,
# mirrored in tunnel/framing.js; combined with the v18 one-frame-per-message
# rule this keeps every WebSocket message under 32KB. See design.md D3.
# v20: sch-remote-ui-tunnel — send-side flow control in the tunnel reader_loop
# (do not read the target ahead of the client's acks beyond a 128KB window <
# the 256KB retain buffer) so a post-reconnect resend is always satisfiable
# instead of desyncing offsets on large transfers. See design.md D3.
# v21: sch-remote-ui-tunnel — root-caused the large-transfer 1006 reconnect
# storm to the **250 frames/sec per-connection** WebSocket rate limit
# (bedrock-agentcore-limits.html; the formal quota table confirms the frame
# SIZE limit is 64KB, so v19's 32KB assumption was over-conservative). Two
# changes: (1) MAX_CHUNK back to 45000 (~60KB/frame, <64KB) so bulk transfer
# uses the FEWEST frames; (2) token-bucket pacing of every outgoing WebSocket
# frame to ~200 frames/sec (main.py `_rate_limit`), comfortably under 250, so
# AgentCore no longer closes the connection mid-transfer. Flow control (v20)
# is kept. See design.md D3.
# v22: sch-acp-editor-integration — file-locality bridging for `sch acp`
# editors (Zed): the tunnel `@app.websocket` handler accepts a third mode
# `fs` (next to tcp/exec) that spawns the NEW /app/fs_sync_worker.py, a
# stdio JSON-line sync worker rooted at /mnt/workspace/repo (manifest scan
# with adaptive ~1s/5s polling, chunked file events < 64KB/frame, one-time
# .git snapshot on first hydration then .git excluded from live sync,
# last-writer-wins conflicts). The worker rides the exec subprocess
# plumbing with a FIXED server-side argv (never client-controlled) and the
# existing framing (offset/ack/resend) for loss-free reconnects. Local
# peer: tunnel/mirror.js; protocol doc in the worker's module docstring.
# No new Python dependency, no dispatcher/init/L2 changes; clients on
# older bridges are unaffected (mode fs is opt-in from `sch acp`, v21
# clients never send it; v22 shim keeps rejecting unknown modes).
# v23: the real AWS CLI v2 is now installed in the image (pinned via
# AWS_CLI_VERSION, official installer, arch-detected), so `aws ...` works from
# the harness' shell tool and from an operator's `sch shell`. Until now there
# was NO system `aws` binary by design (the `aws-api` MCP server bundles
# botocore in its own venv), which made every ad-hoc AWS call go through the
# MCP layer or hand-written boto3 snippets. SECURITY NOTE: a shell-reachable
# CLI bypasses the MCP server's READ_OPERATIONS_ONLY gate — the enforcing
# boundary remains IAM (execution role: ReadOnlyAccess + write only under the
# checkpoint prefix, see openspec/specs/runtime-provisioning/spec.md, which
# already specifies exactly this "mutation bypassing the MCP gate is denied by
# IAM" scenario). Credentials resolve through the same ~/.aws/config
# credential_process IMDSv2 bridge used by the JS SDK; AWS_PAGER="" keeps the
# CLI from ever handing output to `less` in a PTY session.
# v24: adds the pinned OpenSpec and Backlog.md CLIs as occasional-use base
# tools. Both use the existing Node.js 22/npm toolchain and are installed
# globally on the image's local disk, so `openspec` and `backlog` are available
# without runtime downloads and are not shadowed by the workspace mount.
# v26: add-claude-openspec-commands — the OpenSpec slash commands and skills
# for Claude Code are now GENERATED at build time from the pinned CLI
# (`openspec init --tools claude` with a throwaway all-12-workflows profile,
# so /opsx:ff, /opsx:new, /opsx:continue, /opsx:verify, /opsx:onboard and
# /opsx:bulk-archive are included — the CLI's default core profile emits only
# 6) into /app/claude-templates/, and seeded per-file/idempotently by
# init-workspace.sh into $CLAUDE_CONFIG_DIR/commands/opsx/ and
# $CLAUDE_CONFIG_DIR/skills/ on harness=claude workspaces. NEVER pre-baked
# into /home/sch/.claude: a non-empty local dir would permanently disable the
# L2 claude-state restore (_restore_claude_state's never-overwrite-L1 guard).
# init-workspace.sh also seeds the OpenSpec global config (on the mount,
# $XDG_CONFIG_HOME/openspec/config.json) only-if-absent with the same full
# 12-workflow profile, so `openspec init` run by the operator inside the
# microVM generates the complete artifact set too.
# v25: sch-fix-db-backup-wal — fixes SILENT session-history loss on microVM
# recycle. opencode >= 1.18 keeps opencode.db in WAL journal mode; the sqlite
# backup API copies the WAL header into the destination, and re-opening an
# existing WAL-headered file through the unix-dotfile VFS fails ("unable to
# open database file"), so the in-place _backup_db_once worked exactly once
# per file lifetime and then failed forever — the day-0 (empty) backup kept
# being re-uploaded and every next-day boot restored an empty session DB
# (/sessions blank) while repo/state restored fine. app/main.py now (1) backs
# up into a FRESH temp file + atomic rename (each pass equals the always-
# working first pass), (2) uses the default VFS when storage=s3 (backup path
# is local disk; the ENOLCK dotfile workaround is mount-only), (3) surfaces
# db-backup failures in the checkpoint status (partial, not ok), and (4)
# fails the boot closed on a real _restore_db error instead of going ready
# with an empty DB.
# v27: add-telegram-interaction — inbound Telegram channel: the shim's
# notifier thread polls the per-workspace DynamoDB command queue (opt-in via
# SCH_TELEGRAM_COMMANDS_TABLE/SCH_TELEGRAM_ROUTING_TABLE), a file-based
# approval broker under /tmp/sch-approval bridges the harness permission
# hooks (claude PreToolUse + opencode permission.ask, now DECISIONAL with a
# bounded wait and fail-safe timeout) to the Approve/Deny inline keyboard,
# free text is injected into the active opencode session or submitted as a
# `task --continue` follow-up (app/telegram_interaction.py; templates
# updated).
# v28: seed-opencode-openspec-workflows — the same pinned OpenSpec build step
# now generates the complete 12-workflow command/skill set for OpenCode and
# Claude. OpenCode artifacts are seeded per-file into the persistent global
# config; runtime bootstrap never invokes `openspec init` or writes harness
# integration artifacts into the repository.
# v29: add-dev-env-autonomy — headless runs used to STOP on a project
# environment that was not materialized yet (observed: absent
# `tunnel/node_modules`, missing `boto3`) even though the image already shipped
# Node 22/npm, Python 3.11 and uv: the harness had the tools but no mandate to
# use them, and the image lacked a few pieces such work predictably hits.
# What changes: (1) the seeded global briefs (opencode AGENTS.md / claude
# CLAUDE.md) gain a "Development environments" section making autonomous,
# lockfile-first, project-local environment bootstrap the agent's job, and the
# four agent templates carry the matching behavioral delta (remote-auto: a
# missing local dependency is a bootstrap to run, not a blocker to report;
# remote-interactive: bootstrap only on explicit request, per `bash: ask`);
# (2) the native build toolchain (`make`, `gcc`, `gcc-c++`, `python3.11-devel`)
# and `jq` are installed in the base tooling layer, so Python C extensions
# without an aarch64 wheel and `node-gyp` modules compile; (3) `uv` — until now
# the only unpinned tool in this image — is pinned via `ARG UV_VERSION` with a
# build-time version assert; (4) the runtime uv Python policy becomes explicit
# image ENV (`UV_PYTHON=python3.11`, `UV_PYTHON_DOWNLOADS=never`, mirrored in
# /etc/profile.d/sch-env.sh and harness-wrapper.sh because login shells do not
# inherit container ENV): project venvs use the system python3.11 and uv never
# downloads a managed CPython onto the checkpointed mount.
# Known risks: project-local `.venv/`/`node_modules/` live on
# /mnt/workspace and therefore inflate the L2 checkpoint tar and the `sch acp`
# fs-sync mirror — accepted here and declared an explicit FOLLOW-UP change
# (excluding them from `_create_archive` and the fs-sync manifest); the
# toolchain adds ~150-250MB to the image (one-off, no runtime cost); and per
# the never-overwrite seeding contract PRE-EXISTING workspaces keep their old
# briefs (deleting a seeded template file re-seeds the new version at next
# boot).
# v30: add-pi-harness — Pi (`@earendil-works/pi-coding-agent`, MIT, pre-1.0) is
# installed as the THIRD harness next to opencode and claude: pinned via
# ARG PI_VERSION, `npm install -g --ignore-scripts` (supply-chain hardening
# recommended upstream), with build-time asserts on the version, on the CLI
# capabilities the SCH contract depends on (`-p`, `--session`,
# `--append-system-prompt`, `--provider`, `--mode`, `-e`) and on the image's
# Node satisfying the package's `engines` (>= 22.19.0). A THIRD copy of the
# dispatcher lands at /usr/local/bin/pi with its own SCH_HARNESS_REAL, and the
# ENV bridge gains PI_CODING_AGENT_DIR (local disk, mirrored to
# /mnt/workspace/state/pi by the L2 loop like the claude state),
# PI_TELEMETRY=0 and PI_SKIP_VERSION_CHECK=1. New templates in
# /app/pi-templates/ (settings.json, AGENTS.md, roles/, extensions/sch-pi.ts)
# are seeded per-file by init-workspace.sh into PI_CODING_AGENT_DIR. Pi has NO
# permission prompt and NO MCP support by design: the headless argv therefore
# carries no auto-approve flag (conformant by construction) and the accepted
# limits (no MCP/web/attach/handoff/acp) are documented in the README.

# v32: memory-cost footprint (TASK-1, peak-memory billing) — AgentCore bills
# the session on its peak second, so the image carries conservative default
# caps: SCH_NODE_HEAP_MB=1792 (Node --max-old-space-size for harness, builds
# and headless tasks) and SCH_BUILD_JOBS=2 (fanned out to MAKEFLAGS,
# CMAKE_BUILD_PARALLEL_LEVEL, CARGO_BUILD_JOBS; also restated for repo
# runners). Both are plain env overrides per workspace/deploy — raising them
# re-prices the peak, see docs/deploy.md "Lifecycle and Costs". The L2
# checkpoint excludes regenerable dirs (node_modules/.venv/build outputs,
# see app/main.py REPO_CHECKPOINT_EXCLUDE_NAMES) and rebuilds the project env
# best-effort after a restore (SCH_REBUILD_ENV_ON_RESTORE=1); the v29
# follow-up debt note above is resolved by that change.

# Must stay in the GLOBAL ARG scope (before the first FROM): an ARG declared
# after a FROM belongs to that build stage and expands to "" in later FROM
# lines ("base name should not be blank").
ARG RUNTIME_BASE=standard-base

FROM public.ecr.aws/amazonlinux/amazonlinux:2023 AS standard-base

# Opt-in local-build base for TLS-intercepting corporate networks. The default
# build starts from standard-base and never copies or processes image/certs.
FROM standard-base AS corporate-ca
COPY certs/ /tmp/certs/
RUN if ls /tmp/certs/*.pem >/dev/null 2>&1; then \
        cat /tmp/certs/*.pem >> /etc/pki/tls/certs/ca-bundle.crt \
        && cp /tmp/certs/*.pem /etc/pki/ca-trust/source/anchors/; \
    fi \
    && rm -rf /tmp/certs

FROM ${RUNTIME_BASE} AS runtime

# 1.18.20 is the minimum safe headless version (anomalyco/opencode#43675):
# older `opencode run --auto` ignored permission requests from child sessions,
# leaving the parent task tool running forever when a subagent needed
# permission. Keep the default on the latest validated patch and enforce the
# floor after installation below.
ARG OPENCODE_VERSION=1.18.31
# Claude Code (sch-multi-harness, design D10): pinned via npm-22, binary lands
# at /usr/local/bin/claude. OQ-CLAUDE-VERSION (design Open Questions) calls for
# a pinned @anthropic-ai/claude-code version that honors BOTH
# CLAUDE_CODE_USE_BEDROCK=1 AND --dangerously-skip-permissions. v14 bumps the
# pin to 2.1.210: the /model picker lineup is baked per-binary-version, and
# the current Anthropic aliases require a recent binary.
# No `latest` tag (spec: runtime-image, "Claude Code installato a versione
# pinnata").
ARG CLAUDE_CODE_VERSION=2.1.272

# Pi (add-pi-harness, design D10): third harness, pinned like everything else in
# this image. 0.84.2 is the version whose behavior was verified empirically in a
# live SCH microVM (Bedrock via the execution role with zero extra config, print
# mode not hanging on stdin, session layout, extension hooks) — see the change's
# design.md Open Questions. Pi is PRE-1.0: expect breaking changes between bumps,
# which is exactly why the capability asserts below fail the BUILD (never degrade
# at runtime) and why the session-layout resolver keeps a documented `-c` fallback.
ARG PI_VERSION=0.85.1
ARG SCH_IMAGE_VERSION=dev
# v31: migrate-aws-api-to-managed-mcp — replaces the self-hosted
# `awslabs.aws-api-mcp-server` (pinned 1.3.46, entering end-of-development
# 2026-07-15, removed 2027-07-15; depends on AWS CLI v1, also entering
# maintenance mode) with the managed AWS MCP Server reached through the pinned
# `mcp-proxy-for-aws-cli` SigV4 proxy. What changes: (1) the uv tool install
# now pins MCP_PROXY_VERSION (no constraints.txt coupling — the proxy tracks
# current fastmcp/mcp); (2) the BAAI/bge-base-en-v1.5 embedding-model bake is
# gone (it served only the old server's suggest_aws_commands index, ~440MB of
# image); (3) the default endpoint is eu-central-1 (closest managed endpoint
# to the default SCH_AWS_REGION eu-west-1; us-east-1 is the fallback) with the
# operation region passed per-boot via --metadata AWS_REGION=${SEED_REGION};
# (4) read-only posture moves from READ_OPERATIONS_ONLY to the proxy
# --read-only flag, with IAM (execution role + aws:ViaAWSMCPService /
# aws:CalledViaAWSMCP condition keys) as the enforcing boundary per spec R10.
# aws-docs stays self-hosted at its pin.

# AWS MCP servers (design D1), pinned versions verified on PyPI (task 1.1):
#   awslabs.aws-documentation-mcp-server 1.1.30 (self-hosted, local stdio)
#   mcp-proxy-for-aws-cli                1.6.5  (SigV4 proxy to the managed
#     AWS MCP Server; replaces awslabs.aws-api-mcp-server, superseded by AWS —
#     end of development 2026-07-15, removal 2027-07-15 — see v31 note above)
# NOTE: aws-docs is pinned at >=1.1.30 on purpose. Releases <=1.1.29 declare
# `mcp>=1.23.0` with no upper bound, so `uv tool install` resolves the MCP SDK
# to 2.0.0, which removed `mcp.server.fastmcp` — the server then crashes at
# startup with ModuleNotFoundError. 1.1.30 caps the dependency at `mcp<2.0.0`.
ARG AWS_DOCS_MCP_VERSION=1.1.30
# Proxy to the managed AWS MCP Server (TASK-25). eu-central-1 is the managed
# endpoint closest to the default SCH_AWS_REGION (eu-west-1); override at build
# time to https://aws-mcp.us-east-1.api.aws/mcp if needed. The operation region
# is NOT baked here — init-workspace.sh passes --metadata AWS_REGION at seed.
ARG MCP_PROXY_VERSION=1.6.5
ARG AWS_MCP_ENDPOINT=https://aws-mcp.eu-central-1.api.aws/mcp

# context7 MCP server (sch-context7-builtin): Node.js package, installed via
# the same `npm install -g` toolchain as `opencode-ai` and `@anthropic-ai/
# claude-code` above. Binario `context7-mcp` lands at `/usr/local/bin/`; the
# seeded harness config references it directly (no `npx` at runtime). Built-in
# ma disabilitato di default su entrambi gli harness (vedi init-workspace.sh
# e v15 banner sopra). Anonymous access by default; optional CONTEXT7_API_KEY
# env var, mai seedata.
ARG CONTEXT7_MCP_VERSION=3.2.3

# ACP (Agent Client Protocol) adapter for Claude Code (sch-remote-ui-tunnel,
# design D6/D7): `sch acp` needs a stdio ACP agent for harness=claude, since
# Claude Code has no native `acp` subcommand (unlike OpenCode's `opencode
# acp`, already built-in). `@agentclientprotocol/claude-agent-acp` is the
# official adapter (exposes bin `claude-agent-acp`, wraps the `claude`
# binary already installed above). Verified on npm at design time: latest
# is 0.59.0, no `latest` tag used here (same pinning discipline as every
# other tool in this image).
ARG CLAUDE_ACP_VERSION=0.59.0

# Base workflow tools (v24), pinned like the other globally installed CLIs.
# OpenSpec exposes `openspec`; Backlog.md's npm package exposes `backlog` and
# pulls the matching native Linux package for the image architecture.
ARG OPENSPEC_VERSION=1.9.0
ARG BACKLOG_MD_VERSION=1.50.1

# GitHub CLI (TASK-26, opt-in GitHub access from the harness): pinned like
# every other tool in this image. Installed from the official versioned
# release tarball (never a floating download); the binary lands at
# /usr/local/bin/gh on local disk. Auth is NEVER baked in: the harness gets
# GH_TOKEN/GITHUB_TOKEN from the per-session staging only when the operator
# configured GITHUB_TOKEN (never `gh auth login` — its hosts file would land
# on the checkpointed mount and persist the secret).
ARG GH_VERSION=2.100.0

# uv (add-dev-env-autonomy, design D4). Verified latest on PyPI at implementation
# time (0.12.5). Pinned like every other tool in this image: a floating uv would
# silently change environment-resolution behavior between rebuilds. Bumping it is
# a one-line deliberate change.
ARG UV_VERSION=0.12.5

# AWS CLI v2 (v23), pinned like everything else in this image: the official
# installer is fetched from a VERSIONED url (awscli-exe-linux-<arch>-<ver>.zip),
# never the floating `awscli-exe-linux-<arch>.zip`, and never the AL2023
# `awscli-2` rpm (whose version drifts with the distro repo snapshot). Verified
# available at design time for both aarch64 and x86_64.
ARG AWS_CLI_VERSION=2.36.8

# Default AWS region baked into the image (design D3); overridden at runtime
# by AWS_REGION/AWS_DEFAULT_REGION when the platform injects them (task 1.3:
# AgentCore Runtime exposes AWS_REGION/AWS_DEFAULT_REGION to the container
# process even though this Dockerfile does not set them itself).
ARG SCH_AWS_REGION=eu-west-1

# --- Optional corporate CA certs (TLS interception on local builds) -----------
# certs/ is empty in git (only .gitkeep); drop *.pem there when building behind
# a TLS-intercepting proxy (e.g. Netskope). Harmless no-op otherwise.
COPY certs/ /tmp/certs/
RUN if ls /tmp/certs/*.pem >/dev/null 2>&1; then \
        cat /tmp/certs/*.pem >> /etc/pki/tls/certs/ca-bundle.crt \
        && cp /tmp/certs/*.pem /etc/pki/ca-trust/source/anchors/ 2>/dev/null || true; \
    fi \
    && rm -rf /tmp/certs

# --- Base tooling: git, archive utils, shell utils, Node.js, Python ---------
# add-dev-env-autonomy (v29) adds the pieces that autonomous project-environment
# bootstrap predictably needs: `jq` (JSON in agent shell scripts) and the native
# build toolchain `make`/`gcc`/`gcc-c++`/`python3.11-devel` — required by Python
# packages without an aarch64 wheel and by `node-gyp` native modules. Deliberately
# NOT `dnf groupinstall "Development Tools"` (pulls rpm-build/autotools/bison,
# against this image's minimalism). Like every other dnf package here, these
# follow the AL2023 repo snapshot rather than an explicit rpm version pin.
RUN dnf -y update \
    && dnf -y install \
        git tar unzip gzip zip which findutils procps-ng shadow-utils \
        util-linux ca-certificates less vim-minimal nano jq \
        make gcc gcc-c++ \
        nodejs22 nodejs22-npm \
        python3.11 python3.11-pip python3.11-devel \
    && update-ca-trust \
    && dnf clean all \
    && rm -rf /var/cache/dnf

# Make node/npm and pip honor the system CA bundle (needed when corp CAs are
# injected above; harmless otherwise — the bundle contains the standard roots).
# SSL_CERT_FILE is the one Python's ssl module / httpx / huggingface_hub
# actually respect (REQUESTS_CA_BUNDLE only covers the 'requests' library;
# httpx uses certifi by default and ignores REQUESTS_CA_BUNDLE) — needed for
# the BAAI/bge-base-en-v1.5 pre-download (v12, removed in v31) behind a TLS-intercepting proxy.
ENV NODE_EXTRA_CA_CERTS=/etc/pki/tls/certs/ca-bundle.crt \
    PIP_CERT=/etc/pki/tls/certs/ca-bundle.crt \
    REQUESTS_CA_BUNDLE=/etc/pki/tls/certs/ca-bundle.crt \
    AWS_CA_BUNDLE=/etc/pki/tls/certs/ca-bundle.crt \
    SSL_CERT_FILE=/etc/pki/tls/certs/ca-bundle.crt \
    GIT_SSL_CAINFO=/etc/pki/tls/certs/ca-bundle.crt

# AL2023 ships versioned node binaries (node-22 / npm-22); expose canonical names.
RUN if ! command -v node >/dev/null 2>&1; then ln -s "$(command -v node-22)" /usr/local/bin/node; fi \
    && if ! command -v npm >/dev/null 2>&1; then ln -s "$(command -v npm-22)" /usr/local/bin/npm; fi \
    && node --version && npm --version

# --- AWS CLI v2, pinned (v23) -------------------------------------------------
# Installed from the official versioned installer into /usr/local/aws-cli with
# symlinks in /usr/local/bin — LOCAL DISK, so it is never shadowed by the
# session-storage mount at /mnt/workspace (same rationale as the uv-installed
# MCP servers below) and it is available before the mount is restored.
# `unzip` and `curl` come from the base tooling layer above (AL2023 ships
# curl-minimal, which provides /usr/bin/curl); the CA bundle env set above
# makes the download work behind a TLS-intercepting corporate proxy too.
# Arch is detected instead of hardcoded: AgentCore Runtime requires arm64, but
# the same Dockerfile must still build for x86_64 local experiments.
# The trailing `test` is a build-time assertion that the installed CLI really
# is the pinned version (fails the build loudly on a silent installer change).
RUN case "$(uname -m)" in \
        aarch64|arm64) AWSCLI_ARCH=aarch64 ;; \
        x86_64|amd64)  AWSCLI_ARCH=x86_64 ;; \
        *) echo "unsupported architecture $(uname -m) for AWS CLI v2" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o /tmp/awscliv2.zip \
        "https://awscli.amazonaws.com/awscli-exe-linux-${AWSCLI_ARCH}-${AWS_CLI_VERSION}.zip" \
    && unzip -q /tmp/awscliv2.zip -d /tmp \
    && /tmp/aws/install --bin-dir /usr/local/bin --install-dir /usr/local/aws-cli \
    && rm -rf /tmp/awscliv2.zip /tmp/aws \
    && aws --version \
    && test "$(aws --version 2>&1 | awk '{print $1}')" = "aws-cli/${AWS_CLI_VERSION}"

# --- OpenCode, pinned ---------------------------------------------------------
RUN npm install -g "opencode-ai@${OPENCODE_VERSION}" \
    && test "$(opencode --version | tr -d '[:space:]')" = "${OPENCODE_VERSION}" \
    && node -e "const have=process.argv[1].split('.').map(Number), min=[1,18,20]; for(let i=0;i<3;i++){if((have[i]||0)>min[i])process.exit(0);if((have[i]||0)<min[i])process.exit(1)}" "${OPENCODE_VERSION}" \
    || { echo "OpenCode ${OPENCODE_VERSION} is unsafe for headless subagents; require >= 1.18.20" >&2; exit 1; }

# Build-time assert (sch-remote-ui-tunnel, design D7): the pinned OpenCode
# version MUST expose `serve` (sch attach's remote HTTP server) and `acp`
# (sch acp's stdio ACP agent for harness=opencode). Verified at design time
# against 1.18.4: both present, plus `opencode attach <url>` client-side
# (not checked here — that runs on the OPERATOR's laptop, not in this
# image). A future OPENCODE_VERSION bump that drops either subcommand fails
# the build loudly instead of producing an image where `sch attach`/`sch
# acp` silently do not work.
RUN opencode serve --help >/dev/null \
    && opencode acp --help >/dev/null

# --- Claude Code, pinned (sch-multi-harness, design D10) ----------------------
# Installed alongside OpenCode via the same npm-22 toolchain (already symlinked
# to `npm` above). The binary lands at /usr/local/bin/claude; the dispatcher
# below intercepts it just like OpenCode so no launch path exec's the real
# binary directly (design D3).
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && claude --version \
    && claude --help | grep -q -- '--agent'

# --- Pi, pinned (add-pi-harness, design D10) ----------------------------------
# `--ignore-scripts` is upstream's own recommendation for installing Pi (no
# postinstall step is needed: the package ships prebuilt `dist/`), and it is the
# supply-chain hardening this image applies to a pre-1.0 dependency. The binary
# lands at /usr/local/bin/pi and is shadowed by the dispatcher below, exactly
# like opencode and claude.
#
# Build-time asserts (spec: runtime-image, "Pi installato a versione pinnata"):
#   1. `pi --version` == the pin (a silent registry/installer change fails here);
#   2. every CLI capability the SCH contract depends on is present in --help:
#      `-p`/`--print` (headless print mode), `--session` (explicit resume),
#      `--append-system-prompt` (remote-auto/remote-interactive role contract —
#      Pi has no agent files), `--provider` + `--model` (Bedrock selection),
#      `--mode` (json/rpc, reserved for future observability), `-e`/`--extension`
#      (the seeded sch-pi.ts extension);
#   3. the image's Node satisfies the package's own `engines` constraint. Read
#      from the INSTALLED package.json rather than hardcoded, so a future PI
#      version that raises the floor above the image's Node fails the build
#      instead of crashing at runtime in the microVM.
# A failed assert fails the BUILD, never degrades at runtime.
RUN npm install -g --ignore-scripts --no-fund --no-audit \
        "@earendil-works/pi-coding-agent@${PI_VERSION}" \
    && test "$(pi --version | tr -d '[:space:]')" = "${PI_VERSION}" \
    && pi --help > /tmp/pi-help.txt \
    && for flag in ' -p' '--session' '--append-system-prompt' '--provider' '--model' '--mode' ' -e'; do \
        grep -q -- "${flag}" /tmp/pi-help.txt \
            || { echo "pi ${PI_VERSION} is missing required CLI flag '${flag}'" >&2; exit 1; }; \
    done \
    && rm -f /tmp/pi-help.txt \
    && PI_PKG_JSON="$(npm root -g)/@earendil-works/pi-coding-agent/package.json" \
    && test -f "${PI_PKG_JSON}" \
    && node -e "\
const pkg = require(process.argv[1]); \
const want = (pkg.engines && pkg.engines.node) || ''; \
if (!want) { console.error('pi package declares no engines.node'); process.exit(1); } \
const min = want.replace(/^[^0-9]*/, '').split('.').map(Number); \
const have = process.versions.node.split('.').map(Number); \
for (let i = 0; i < 3; i++) { \
  if ((have[i] || 0) > (min[i] || 0)) break; \
  if ((have[i] || 0) < (min[i] || 0)) { \
    console.error('image node ' + process.versions.node + ' does not satisfy pi engines.node ' + want); \
    process.exit(1); \
  } \
} \
console.log('pi engines.node ' + want + ' satisfied by node ' + process.versions.node);" \
        "${PI_PKG_JSON}"

# --- Base workflow tools, pinned (v24) ----------------------------------------
RUN npm install -g --no-fund --no-audit \
        "@fission-ai/openspec@${OPENSPEC_VERSION}" \
        "backlog.md@${BACKLOG_MD_VERSION}" \
    && test -x "$(command -v openspec)" \
    && test -x "$(command -v backlog)" \
    && test "$(openspec --version | tr -d '[:space:]')" = "${OPENSPEC_VERSION}" \
    && test "$(backlog --version | tr -d '[:space:]')" = "${BACKLOG_MD_VERSION}"

# --- OpenSpec harness templates, generated at build time (v28) -----------------
# Generated (not vendored) so the artifact set can never drift from the pinned
# OPENSPEC_VERSION: the command bodies embed CLI invocation contracts that
# change across versions, and a version bump regenerates them for free.
# The CLI's default "core" profile emits only 6 workflows — a throwaway global
# config (HOME/XDG_CONFIG_HOME on a tmp dir; `openspec config path` resolves
# through XDG) selects ALL 12 (ALL_WORKFLOWS in dist/core/profiles.js @1.9.0)
# so /opsx:ff, /opsx:new, /opsx:continue, /opsx:verify, /opsx:onboard and
# /opsx:bulk-archive are included. OPENSPEC_TELEMETRY=0 keeps the build
# offline-friendly. Claude and OpenCode use separate throwaway projects under
# the same temporary global config so tool detection and extend mode cannot
# make generation order-dependent. The explicit per-workflow assertions pin
# both layouts and fail loudly if a future CLI omits or renames an artifact.
# NOTE: nothing is written to /home/sch/.claude here — pre-baking it would
# permanently disable the L2 claude-state restore (see v26 banner / design D3).
RUN mkdir -p /tmp/opsx-home/.config/openspec /tmp/opsx-claude /tmp/opsx-opencode \
    && printf '%s\n' \
        '{' \
        '  "profile": "custom",' \
        '  "delivery": "both",' \
        '  "telemetry": { "noticeSeen": true },' \
        '  "workflows": ["propose", "explore", "new", "continue", "apply", "update", "ff", "sync", "archive", "bulk-archive", "verify", "onboard"]' \
        '}' \
        > /tmp/opsx-home/.config/openspec/config.json \
    && HOME=/tmp/opsx-home XDG_CONFIG_HOME=/tmp/opsx-home/.config OPENSPEC_TELEMETRY=0 \
        openspec init /tmp/opsx-claude --tools claude --no-animation < /dev/null \
    && HOME=/tmp/opsx-home XDG_CONFIG_HOME=/tmp/opsx-home/.config OPENSPEC_TELEMETRY=0 \
        openspec init /tmp/opsx-opencode --tools opencode --no-animation < /dev/null \
    && mkdir -p /app/claude-templates /app/opencode-templates \
    && cp -R /tmp/opsx-claude/.claude/commands /app/claude-templates/commands \
    && cp -R /tmp/opsx-claude/.claude/skills /app/claude-templates/skills \
    && cp -R /tmp/opsx-opencode/.opencode/commands /app/opencode-templates/commands \
    && cp -R /tmp/opsx-opencode/.opencode/skills /app/opencode-templates/skills \
    && rm -rf /tmp/opsx-home /tmp/opsx-claude /tmp/opsx-opencode \
    && for pair in \
        propose:openspec-propose explore:openspec-explore new:openspec-new-change \
        continue:openspec-continue-change apply:openspec-apply-change update:openspec-update-change \
        ff:openspec-ff-change sync:openspec-sync-specs archive:openspec-archive-change \
        bulk-archive:openspec-bulk-archive-change verify:openspec-verify-change onboard:openspec-onboard; do \
        workflow="${pair%%:*}"; skill="${pair#*:}"; \
        test -f "/app/claude-templates/commands/opsx/${workflow}.md"; \
        test -f "/app/claude-templates/skills/${skill}/SKILL.md"; \
        test -f "/app/opencode-templates/commands/opsx-${workflow}.md"; \
        test -f "/app/opencode-templates/skills/${skill}/SKILL.md"; \
    done \
    && test "$(find /app/claude-templates/commands/opsx -name '*.md' | wc -l)" = "12" \
    && test "$(find /app/claude-templates/skills -mindepth 2 -maxdepth 2 -name SKILL.md | wc -l)" = "12" \
    && test "$(find /app/opencode-templates/commands -maxdepth 1 -name 'opsx-*.md' | wc -l)" = "12" \
    && test "$(find /app/opencode-templates/skills -mindepth 2 -maxdepth 2 -name SKILL.md | wc -l)" = "12"

# --- context7 MCP, pinned (sch-context7-builtin) ------------------------------
# Installed via the same `npm install -g` toolchain as opencode-ai and
# @anthropic-ai/claude-code above. The binario `context7-mcp` lands wherever
# the system npm prefix puts global bins — on AL2023 with the `nodejs22-npm`
# package that is `/usr/bin/` (NOT `/usr/local/bin/`, which is reserved for
# the uv-installed Python MCP servers). Same shape as the existing opencode/
# claude installs (see the wrapper install below: it also uses `command -v`,
# not a hardcoded path). No `npx` at runtime: the seeded harness config
# references the binary by name (`context7-mcp`) and relies on PATH.
# --no-fund/--no-audit silence npm's interactive noise. The `command -v` +
# `test -x` is a build-time assertion that the upstream package still
# exposes `context7-mcp` (a future major could rename the bin; this build
# then fails loudly instead of producing a silent half-broken image).
RUN npm install -g --no-fund --no-audit "@upstash/context7-mcp@${CONTEXT7_MCP_VERSION}" \
    && CTX7_BIN="$(command -v context7-mcp)" \
    && test -n "${CTX7_BIN}" \
    && test -x "${CTX7_BIN}"

# --- claude-agent-acp, pinned (sch-remote-ui-tunnel, design D7) ---------------
# Same `npm install -g` shape as context7-mcp above. Package exposes bin
# `claude-agent-acp` (dist/index.js) — `sch acp` execs it by name, relying
# on PATH, exactly like the opencode/claude/context7-mcp binaries. No `npx`
# at runtime (cold-start/proxy-cache risk, same rationale as context7-mcp).
# The `command -v` + `test -x` pair is a build-time assertion that the
# upstream package still exposes this bin name.
RUN npm install -g --no-fund --no-audit "@agentclientprotocol/claude-agent-acp@${CLAUDE_ACP_VERSION}" \
    && ACP_BIN="$(command -v claude-agent-acp)" \
    && test -n "${ACP_BIN}" \
    && test -x "${ACP_BIN}"

# --- GitHub CLI, pinned (TASK-26, opt-in GitHub access) -----------------------
# Same versioned-installer discipline as the AWS CLI above: arch-detected
# (AgentCore requires arm64; x86_64 stays buildable for local experiments),
# exact-version tarball URL, and a build-time assertion that the installed
# binary really is the pin. `gh --version` prints "gh version <ver> (...)",
# hence the awk field.
RUN case "$(uname -m)" in \
        aarch64|arm64) GH_ARCH=arm64 ;; \
        x86_64|amd64)  GH_ARCH=amd64 ;; \
        *) echo "unsupported architecture $(uname -m) for gh" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o /tmp/gh.tar.gz \
        "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${GH_ARCH}.tar.gz" \
    && mkdir -p /tmp/gh-extract \
    && tar -xzf /tmp/gh.tar.gz -C /tmp/gh-extract \
    && install -m 0755 "/tmp/gh-extract/gh_${GH_VERSION}_linux_${GH_ARCH}/bin/gh" /usr/local/bin/gh \
    && rm -rf /tmp/gh.tar.gz /tmp/gh-extract \
    && gh --version \
    && test "$(gh --version 2>&1 | head -n 1 | awk '{print $3}')" = "${GH_VERSION}"

# Wrap the opencode, claude AND pi binaries with a single dispatcher
# (scripts/harness-wrapper.sh, design D3): wait for the shim's per-harness
# readiness marker before starting (prevents a fresh empty DB / empty JSONL
# dir from clobbering the restored checkpoint), apply the ENV bridge that
# `agentcore exec --it` login shells do not inherit, and branch on the
# workspace's persisted harness. Each wrapper script has its own
# `SCH_HARNESS_REAL` placeholder sed-substituted at build time with the
# real binary path discovered via `readlink -f $(command -v <bin>)` — all
# three real binaries are moved aside so the wrapper shadows them on PATH.
COPY scripts/harness-wrapper.sh /usr/local/bin/.harness-wrapper-src
RUN REAL_OC="$(readlink -f "$(command -v opencode)")" \
    && rm -f "$(command -v opencode)" \
    && sed "s|\${SCH_HARNESS_REAL:?SCH_HARNESS_REAL not set}|${REAL_OC}|" \
        /usr/local/bin/.harness-wrapper-src > /usr/local/bin/opencode \
    && chmod +x /usr/local/bin/opencode \
    && test -x "${REAL_OC}" \
    && REAL_CL="$(readlink -f "$(command -v claude)")" \
    && rm -f "$(command -v claude)" \
    && sed "s|\${SCH_HARNESS_REAL:?SCH_HARNESS_REAL not set}|${REAL_CL}|" \
        /usr/local/bin/.harness-wrapper-src > /usr/local/bin/claude \
    && chmod +x /usr/local/bin/claude \
    && test -x "${REAL_CL}" \
    && REAL_PI="$(readlink -f "$(command -v pi)")" \
    && rm -f "$(command -v pi)" \
    && sed "s|\${SCH_HARNESS_REAL:?SCH_HARNESS_REAL not set}|${REAL_PI}|" \
        /usr/local/bin/.harness-wrapper-src > /usr/local/bin/pi \
    && chmod +x /usr/local/bin/pi \
    && test -x "${REAL_PI}" \
    && rm /usr/local/bin/.harness-wrapper-src

# --- uv, pinned (dev convenience inside the microVM) + shim dependencies ------
# add-dev-env-autonomy (v29, design D4): `uv` used to be the ONLY tool in this
# image installed without a version pin (`pip3.11 install uv`), so env-resolution
# behavior could silently change between rebuilds. It is now pinned via
# ARG UV_VERSION with a build-time assert that the installed binary really is the
# pinned version — the same discipline as AWS CLI / OpenCode / openspec / backlog.
# `uv --version` prints "uv <version> (<hash> <date>)", hence the awk field.
COPY app/requirements.txt /app/requirements.txt
RUN pip3.11 install --no-cache-dir "uv==${UV_VERSION}" \
    && uv --version \
    && test "$(uv --version 2>&1 | awk '{print $2}')" = "${UV_VERSION}" \
    && pip3.11 install --no-cache-dir -r /app/requirements.txt

# --- AWS MCP servers, pinned, installed on local disk (design D1) -------------
# UV_TOOL_DIR/UV_TOOL_BIN_DIR are explicit on this RUN line (not exported as
# image ENV) so the install never follows uv's XDG_DATA_HOME default, which at
# runtime is shadowed by the session storage mount at /mnt/workspace: /opt and
# /usr/local/bin are local disk and always available, mount or no mount.
# --no-cache avoids leaving the download cache in the layer. UV_NATIVE_TLS=1:
# uv uses its own TLS stack (rustls) by default and ignores the system CA
# bundle populated above from image/certs/*.pem (corp TLS-intercepting
# proxies) — native-tls makes it use the OS trust store like pip/npm do.
# UV_PYTHON_DOWNLOADS=never + --python python3.11: without this, uv downloads
# its own managed CPython into $HOME (/root at this point in the build) and
# the venvs symlink to it — unreadable by the non-root "sch" user at runtime
# (discovered in build verification, task 2.5). The already-installed system
# python3.11 satisfies both servers' >=3.10 requirement, so reuse it instead.
# The proxy tracks current fastmcp/mcp releases, so no constraints file is
# needed (v31 removed image/constraints.txt with the old aws-api server).
RUN UV_TOOL_DIR=/opt/uv-tools UV_TOOL_BIN_DIR=/usr/local/bin UV_NATIVE_TLS=1 UV_PYTHON_DOWNLOADS=never \
        uv tool install --no-cache --python python3.11 "awslabs.aws-documentation-mcp-server==${AWS_DOCS_MCP_VERSION}" \
    && UV_TOOL_DIR=/opt/uv-tools UV_TOOL_BIN_DIR=/usr/local/bin UV_NATIVE_TLS=1 UV_PYTHON_DOWNLOADS=never \
        uv tool install --no-cache --python python3.11 \
            "mcp-proxy-for-aws-cli==${MCP_PROXY_VERSION}" \
    && test -x /usr/local/bin/awslabs.aws-documentation-mcp-server \
    && test -x /usr/local/bin/mcp-proxy-for-aws-cli

# --- Non-root user (uid/gid 1000, consistent with the session-storage mount) --
# /run/sch (add-user-provider-keys, design D3): the per-session staging dir for
# the user's provider API keys. Created HERE, at build time, because /run is
# root-owned 0755 in the running microVM and the shim runs as uid 1000 — it
# could not create it itself (verified in a live AgentCore microVM). 0700 and
# owned by sch: only the runtime user can read the staged keys. It lives outside
# /mnt/workspace and outside /home/sch, so no checkpoint (L2 tarballs, DB backup,
# claude/pi state mirrors) can ever pick it up, and its content dies with the
# microVM. Empty in the image: the file only ever exists while a session that
# received keys is alive.
RUN groupadd -g 1000 sch \
    && useradd -u 1000 -g 1000 -m -s /bin/bash sch \
    && mkdir -p /mnt/workspace /home/sch/.opencode /home/sch/.claude /home/sch/.pi /run/sch \
    && install -d -o 1000 -g 1000 /tmp/opencode \
    && chown -R sch:sch /mnt/workspace /app /home/sch /opt/uv-tools /run/sch \
    && chmod 700 /run/sch \
    && test "$(stat -c %U:%G:%a /run/sch)" = "sch:sch:700"

# Session storage reports files as root:root while the process runs as sch
# (permissions are stored but not enforced in the microVM) — whitelist the
# canonical paths so git does not fail with "dubious ownership".
RUN git config --system --add safe.directory /mnt/workspace/repo \
    && git config --system --add safe.directory '*'

# v31 (migrate-aws-api-to-managed-mcp) removed the baked BAAI/bge-base-en-v1.5
# embedding-model layer (v12): it served only the old aws-api server's
# suggest_aws_commands index. The managed AWS MCP Server needs no local model.

# --- Canonical paths (design D2): all mutable OpenCode state lives on the mount
# EXCEPT the SQLite DB: the session storage does not support POSIX fcntl locks
# (ENOLCK), so opencode.db lives on local disk and the shim backs it up to the
# mount periodically (restore at boot).
# sch-multi-harness (design D2): Claude Code state follows the same pattern —
# the live write path is $HOME/.claude (CLAUDE_CONFIG_DIR) on local disk, and
# the shim mirrors it to /mnt/workspace/state/claude at the L2 checkpoint tick
# (and on forced checkpoint), restoring it on empty-L1 boot. Both avoid the
# fcntl/async-restore hazards that rule out putting them directly on the mount.
# add-dev-env-autonomy (v29, design D2): the uv Python policy is now explicit
# image ENV rather than a per-RUN flag. UV_PYTHON_DOWNLOADS=never keeps uv from
# downloading a managed CPython at runtime — it would land under XDG_DATA_HOME,
# i.e. on the CHECKPOINTED mount (tens of MB per interpreter). UV_PYTHON=python3.11
# is required for the intended behavior to actually happen: AL2023's default
# `python3` is 3.9, so a bare `uv venv` would otherwise build a 3.9 environment.
# Together they mean project envs use the system python3.11, always.
ENV HOME=/home/sch \
    XDG_DATA_HOME=/mnt/workspace/state/data \
    XDG_CONFIG_HOME=/mnt/workspace/state/config \
    OPENCODE_DB=/home/sch/.opencode/opencode.db \
    CLAUDE_CONFIG_DIR=/home/sch/.claude \
    PI_CODING_AGENT_DIR=/home/sch/.pi/agent \
    PI_TELEMETRY=0 \
    PI_SKIP_VERSION_CHECK=1 \
    SCH_DB_BACKUP_INTERVAL=60 \
    SCH_NODE_HEAP_MB=1792 \
    SCH_BUILD_JOBS=2 \
    SCH_REBUILD_ENV_ON_RESTORE=1 \
    AWS_PROFILE=default \
    AWS_REGION=${SCH_AWS_REGION} \
    AWS_DEFAULT_REGION=${SCH_AWS_REGION} \
    SCH_AWS_REGION=${SCH_AWS_REGION} \
    AWS_PAGER= \
    SCH_WORKSPACE_ROOT=/mnt/workspace \
    OPENCODE_VERSION=${OPENCODE_VERSION} \
    CLAUDE_CODE_VERSION=${CLAUDE_CODE_VERSION} \
    PI_VERSION=${PI_VERSION} \
    SCH_IMAGE_VERSION=${SCH_IMAGE_VERSION} \
    AWS_DOCS_MCP_VERSION=${AWS_DOCS_MCP_VERSION} \
    MCP_PROXY_VERSION=${MCP_PROXY_VERSION} \
    AWS_MCP_ENDPOINT=${AWS_MCP_ENDPOINT} \
    SCH_AWS_CLI_VERSION=${AWS_CLI_VERSION} \
    CONTEXT7_MCP_VERSION=${CONTEXT7_MCP_VERSION} \
    CLAUDE_ACP_VERSION=${CLAUDE_ACP_VERSION} \
    OPENSPEC_VERSION=${OPENSPEC_VERSION} \
    BACKLOG_MD_VERSION=${BACKLOG_MD_VERSION} \
    GH_VERSION=${GH_VERSION} \
    UV_VERSION=${UV_VERSION} \
    UV_PYTHON=python3.11 \
    UV_PYTHON_DOWNLOADS=never \
    OPENCODE_ENABLE_EXA=1 \
    CLAUDE_CODE_USE_BEDROCK=1 \
    ANTHROPIC_DEFAULT_FABLE_MODEL=global.anthropic.claude-fable-5 \
    ANTHROPIC_DEFAULT_FABLE_MODEL_NAME="Fable 5 (Global)" \
    ANTHROPIC_DEFAULT_OPUS_MODEL=eu.anthropic.claude-opus-5 \
    ANTHROPIC_DEFAULT_OPUS_MODEL_NAME="Opus 5 (EU)" \
    ANTHROPIC_DEFAULT_SONNET_MODEL=global.anthropic.claude-sonnet-5 \
    ANTHROPIC_DEFAULT_SONNET_MODEL_NAME="Sonnet 5 (Global)" \
    ANTHROPIC_DEFAULT_HAIKU_MODEL=eu.anthropic.claude-haiku-4-5-20251001-v1:0 \
    ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME="Haiku 4.5 (EU)" \
    ANTHROPIC_CUSTOM_MODEL_OPTION=global.anthropic.claude-opus-5 \
    ANTHROPIC_CUSTOM_MODEL_OPTION_NAME="Opus 5 (Global)" \
    ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION="Opus 5 via the global cross-region inference profile" \
    AGENT_PORT=8080

# --- Shim + init script --------------------------------------------------------
COPY --chown=sch:sch app/main.py /app/main.py
COPY --chown=sch:sch app/tunnel_framing.py /app/tunnel_framing.py
COPY --chown=sch:sch app/fs_sync_lease.py /app/fs_sync_lease.py
COPY --chown=sch:sch app/fs_sync_worker.py /app/fs_sync_worker.py
# add-telegram-notifications: notifier module imported by main.py (stdlib-only).
COPY --chown=sch:sch app/telegram_notifier.py /app/telegram_notifier.py
# add-telegram-interaction: inbound command poll + approval broker (stdlib +
# the boto3 already required by main.py).
COPY --chown=sch:sch app/telegram_interaction.py /app/telegram_interaction.py
COPY --chown=sch:sch scripts/init-workspace.sh /app/init-workspace.sh
COPY --chown=sch:sch scripts/imds-credential-process.sh /app/imds-credential-process.sh
RUN chmod +x /app/init-workspace.sh /app/imds-credential-process.sh
# Build-time sanity: the fs sync worker (v22, sch-acp-editor-integration)
# must at least import and expose its CLI on the image's interpreter.
RUN python3.11 -c "import ast; ast.parse(open('/app/fs_sync_worker.py').read())" \
    && python3.11 /app/fs_sync_worker.py --help >/dev/null

# --- OpenCode global templates (agents, policy, plugins, OpenSpec workflows) ---
# Two custom primary agents tuned for the microVM harness (remote-interactive
# for TUI/brainstorming sessions, remote-auto for the headless `--auto` task
# path) plus a global AGENTS.md carrying the microVM environment brief that
# OpenCode appends to EVERY agent's system prompt. Seeded per-file and
# idempotently by init-workspace.sh into
# $XDG_CONFIG_HOME/opencode/{AGENTS.md,agents/}. The build-time asserts make
# "template missing at runtime" a can't-happen: the seeded opencode.json sets
# `default_agent: remote-interactive` and the shim's headless argv passes
# `--agent remote-auto`, both of which reference these files.
# The commands/ and skills/ subtrees were generated above from the pinned
# OpenSpec CLI. COPY merges the static source templates without replacing them.
COPY --chown=sch:sch opencode-templates/ /app/opencode-templates/
RUN test -f /app/opencode-templates/AGENTS.md \
    && test -f /app/opencode-templates/agents/remote-interactive.md \
    && test -f /app/opencode-templates/agents/remote-auto.md \
    && test -f /app/opencode-templates/plugin/sch-telegram.js \
    # add-dev-env-autonomy: the dev-environment autonomy contract is part of the
    # brief, and the remote-auto delta ("missing dependency = bootstrap") is what
    # actually unblocks headless runs. Assert both so a template edit can never
    # silently drop them.
    && grep -q '^## Development environments$' /app/opencode-templates/AGENTS.md \
    && grep -q 'bootstrap to run, not a blocker' /app/opencode-templates/agents/remote-auto.md

# Claude Code main-thread agents and global microVM instructions. Claude Code
# 2.1.210 is pinned above and build-checked for the --agent CLI surface. The
# files are seeded user-scoped under $CLAUDE_CONFIG_DIR by init-workspace.sh.
COPY --chown=sch:sch claude-templates/ /app/claude-templates/
RUN test -f /app/claude-templates/CLAUDE.md \
    && test -f /app/claude-templates/agents/remote-interactive.md \
    && test -f /app/claude-templates/agents/remote-auto.md \
    && grep -q '^name: remote-interactive$' /app/claude-templates/agents/remote-interactive.md \
    && grep -q '^name: remote-auto$' /app/claude-templates/agents/remote-auto.md \
    # add-dev-env-autonomy: same asserts as the OpenCode brief above — the
    # normative content must be present on BOTH harnesses.
    && grep -q '^## Development environments$' /app/claude-templates/CLAUDE.md \
    && grep -q 'bootstrap to run, not a blocker' /app/claude-templates/agents/remote-auto.md \
    # add-telegram-notifications: milestone hook (referenced by the seeded
    # settings.json as python3 /app/claude-templates/hooks/telegram-hook.py)
    # and the notifier module import, both must survive image assembly.
    && test -f /app/claude-templates/hooks/telegram-hook.py \
    && python3.11 -c "import ast; ast.parse(open('/app/claude-templates/hooks/telegram-hook.py').read())" \
    && python3.11 -c "import ast; ast.parse(open('/app/telegram_notifier.py').read())" \
    && python3.11 -c "import ast; ast.parse(open('/app/telegram_interaction.py').read())"

# --- Pi global templates (add-pi-harness, design D4/D6/D8) --------------------
# Pi has no agent files and no MCP, so the SCH contract is carried by four
# artifacts seeded per-file into PI_CODING_AGENT_DIR by init-workspace.sh:
#   settings.json          Bedrock provider/model defaults (Pi's own --provider
#                          default is `google`, so this seed is mandatory, not
#                          cosmetic), defaultProjectTrust=always (no
#                          non-interactive path may park on a trust prompt),
#                          telemetry off;
#   AGENTS.md              the microVM environment brief — Pi reads AGENTS.md
#                          natively from the global config dir;
#   roles/*.md             remote-auto / remote-interactive as SYSTEM PROMPTS
#                          (passed with --append-system-prompt), the only
#                          available rendering of the role contract on Pi;
#   extensions/sch-pi.ts   Telegram milestones + busy keep-alive,
#                          auto-discovered from the global
#                          extensions dir.
# The asserts mirror the opencode/claude template asserts above: a template
# edit can never silently drop the normative content the runtime depends on.
COPY --chown=sch:sch pi-templates/ /app/pi-templates/
RUN test -f /app/pi-templates/settings.json \
    && test -f /app/pi-templates/AGENTS.md \
    && test -f /app/pi-templates/roles/remote-auto.md \
    && test -f /app/pi-templates/roles/remote-interactive.md \
    && test -f /app/pi-templates/extensions/sch-pi.ts \
    && python3.11 -c "import json,sys; d=json.load(open('/app/pi-templates/settings.json')); \
sys.exit(0 if d.get('defaultProvider')=='amazon-bedrock' and d.get('defaultProjectTrust')=='always' else 1)" \
    # add-dev-env-autonomy: the same normative sections asserted on the other
    # two harnesses must be present on pi too.
    && grep -q '^## Development environments$' /app/pi-templates/AGENTS.md \
    && grep -q 'bootstrap to run, not a blocker' /app/pi-templates/roles/remote-auto.md \
    # Pi loads the extension through jiti at runtime. Node 22 strips TypeScript
    # types natively on import, so importing the template here is a real
    # build-time guard: it fails on a syntax error AND on a missing/renamed
    # default factory export (which pi would otherwise reject silently at
    # session start, degrading to lifecycle-only events). The extension must
    # therefore stay within erasable-TS syntax (type annotations only) — which
    # is also what jiti's transform expects.
    && node --input-type=module -e "\
const mod = await import('/app/pi-templates/extensions/sch-pi.ts'); \
if (typeof mod.default !== 'function') { \
  console.error('sch-pi.ts does not export a default factory function'); \
  process.exit(1); \
} \
console.log('sch-pi.ts loads and exports a factory');"

# --- Pi gateway models.json (extend-pi-gateway-keys, TASK-18) -----------------
# The gateway providers (OpenCode Zen, Kilo Gateway) reach pi through its
# ~/.pi/agent/models.json custom-provider mechanism, not through built-in
# catalog entries: pi has none consuming OPENCODE_API_KEY / KILO_API_KEY. The
# catalog is GENERATED AT BUILD from models.dev and baked into the template
# dir — the microVM must never make an unsolicited outbound call at harness
# startup, so runtime is not the place to refresh it (each image rebuild
# refreshes the snapshot instead). The apiKey entries are ENV VAR references
# ("$OPENCODE_API_KEY"): the key value never enters the file, and init-
# workspace.sh merges/withdraws the blocks per session per the staged keys.
#
# Failure policy lives IN THE GENERATOR, not in shell branching here:
#   models.dev unreachable -> --degrade-on-unreachable makes it exit 0 after
#         writing nothing (a third-party outage must not block the deploy of
#         everything else); init-workspace.sh then warns at session start when
#         a gateway key is staged but the catalog is absent.
#   catalog read but unusable -> non-zero exit, the build FAILS (real
#         regression: a silently empty gateway catalog is the failure class
#         this capability removes).
# Deliberately a SINGLE command with no `$`: `RUN` does no Dockerfile variable
# substitution, so a `\$rc` written for the shell arrives as a literal `$rc`
# and every comparison silently fails (that mistake failed one build here).
COPY scripts/gen-pi-gateway-models.py /tmp/gen-pi-gateway-models.py
RUN python3.11 /tmp/gen-pi-gateway-models.py \
        --output /app/pi-templates/gateway-models.json \
        --degrade-on-unreachable

# --- sch-build-image (v23, add-codebuild-image-rebuild) -----------------------
# Rebuilds this project's runtime image by delegating to CodeBuild: packages
# the workspace's image/ directory, uploads it to S3 and starts the build. No
# build engine runs in the microVM (there cannot be one: non-root, no daemon,
# no privileges). Everything it needs is already in the image: the AWS CLI v2
# installed above, `zip`, and python3.11 for reading the mount marker.
# Configuration comes exclusively from the runtime's environment
# (SCH_IMAGE_REBUILD_PROJECT, SCH_CHECKPOINT_BUCKET): no AWS identifier is
# baked into the image, and with the capability disabled the command exits
# immediately without making a single AWS call.
COPY scripts/sch-build-image.sh /usr/local/bin/sch-build-image
RUN chmod 755 /usr/local/bin/sch-build-image \
    && bash -n /usr/local/bin/sch-build-image \
    && SCH_IMAGE_REBUILD_PROJECT= /usr/local/bin/sch-build-image --help >/dev/null

# AWS config for the sch user: the JS SDK credential chain used by OpenCode
# hangs on the IMDSv2-only microVM (boto3 is fine); credential_process bridges
# it with auto-refresh. Local disk (~/.aws), not the mount: nothing secret is
# persisted and the file must exist before the mount is restored.
RUN mkdir -p /home/sch/.aws \
    && printf '[default]\ncredential_process = /app/imds-credential-process.sh\n' > /home/sch/.aws/config \
    && chown -R sch:sch /home/sch/.aws

# Belt-and-braces (design D3 + follow-up fix): `agentcore exec --it` spawns a
# LOGIN shell (`bash --login`) that does NOT inherit the container's Docker
# ENV at all — verified empirically: only HOME/PATH/SHELL/TERM/USER survive,
# none of XDG_CONFIG_HOME/XDG_DATA_HOME/OPENCODE_DB/SCH_WORKSPACE_ROOT/
# AWS_PROFILE/AWS_REGION. Without this, OpenCode (and any other tool relying
# on these paths) silently falls back to $HOME/.config, $HOME/.opencode —
# never seeing the seeded provider/mcp config (root cause of "no MCP tools
# in the interactive shell"). scripts/opencode-wrapper.sh additionally
# exports the same canonical vars right before exec-ing OpenCode, so it is
# correct even if this profile script is not sourced for some reason. Both
# /etc/profile.d (login shells) and sch's ~/.bashrc (interactive non-login
# shells) are covered so it fires regardless of how `agentcore exec` spawns
# the shell.
RUN printf '%s\n' \
        '# sch belt-and-braces ENV (design D3 + follow-up) — see image/Dockerfile' \
        'export HOME="${HOME:-/home/sch}"' \
        'if [ -f /home/sch/.sch-workspace.json ]; then' \
        '  _SCH_ACTIVE_ROOT="$(python3 -c '\''import json; print(json.load(open("/home/sch/.sch-workspace.json")).get("root") or "")'\'' 2>/dev/null || true)"' \
        'fi' \
        'export SCH_WORKSPACE_ROOT="${_SCH_ACTIVE_ROOT:-${SCH_WORKSPACE_ROOT:-/mnt/workspace}}"' \
        'export XDG_DATA_HOME="${SCH_WORKSPACE_ROOT}/state/data"' \
        'export XDG_CONFIG_HOME="${SCH_WORKSPACE_ROOT}/state/config"' \
        'export OPENCODE_DB="${OPENCODE_DB:-/home/sch/.opencode/opencode.db}"' \
        'export CLAUDE_CONFIG_DIR="${CLAUDE_CONFIG_DIR:-/home/sch/.claude}"' \
        '# add-pi-harness (v30): Pi config dir on LOCAL disk (mirrored to' \
        '# /mnt/workspace/state/pi by the L2 loop), telemetry and the pi.dev' \
        '# latest-version request off. Login shells do not inherit the container' \
        '# ENV, so a bare `pi` here would otherwise fall back to ~/.pi/agent' \
        '# resolved against a different HOME and miss the seeded settings.' \
        'export PI_CODING_AGENT_DIR="${PI_CODING_AGENT_DIR:-/home/sch/.pi/agent}"' \
        'export PI_TELEMETRY="${PI_TELEMETRY:-0}"' \
        'export PI_SKIP_VERSION_CHECK="${PI_SKIP_VERSION_CHECK:-1}"' \
        'unset _SCH_ACTIVE_ROOT' \
        'export AWS_PROFILE="${AWS_PROFILE:-default}"' \
        'export AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-'"${SCH_AWS_REGION}"'}}"' \
        'export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-${AWS_REGION}}"' \
        '# v23: AWS CLI v2 is installed in the image — keep it non-interactive' \
        '# (no `less` pager) in PTY sessions; `-` (not `:-`) so an operator who' \
        '# explicitly exports a pager keeps it.' \
        'export AWS_PAGER="${AWS_PAGER-}"' \
        '# add-dev-env-autonomy (v29): login shells do not inherit the container' \
        '# ENV (see above), so the uv Python policy must be re-exported here too,' \
        '# otherwise a bare `uv venv` in `sch shell` builds a python3.9 env.' \
        'export UV_PYTHON="${UV_PYTHON:-python3.11}"' \
        'export UV_PYTHON_DOWNLOADS="${UV_PYTHON_DOWNLOADS:-never}"' \
        'export OPENCODE_ENABLE_EXA="${OPENCODE_ENABLE_EXA:-1}"' \
        'export CLAUDE_CODE_USE_BEDROCK="${CLAUDE_CODE_USE_BEDROCK:-1}"' \
        'export ANTHROPIC_DEFAULT_FABLE_MODEL="${ANTHROPIC_DEFAULT_FABLE_MODEL:-global.anthropic.claude-fable-5}"' \
        'export ANTHROPIC_DEFAULT_FABLE_MODEL_NAME="${ANTHROPIC_DEFAULT_FABLE_MODEL_NAME:-Fable 5 (Global)}"' \
        'export ANTHROPIC_DEFAULT_OPUS_MODEL="${ANTHROPIC_DEFAULT_OPUS_MODEL:-eu.anthropic.claude-opus-5}"' \
        'export ANTHROPIC_DEFAULT_OPUS_MODEL_NAME="${ANTHROPIC_DEFAULT_OPUS_MODEL_NAME:-Opus 5 (EU)}"' \
        'export ANTHROPIC_DEFAULT_SONNET_MODEL="${ANTHROPIC_DEFAULT_SONNET_MODEL:-global.anthropic.claude-sonnet-5}"' \
        'export ANTHROPIC_DEFAULT_SONNET_MODEL_NAME="${ANTHROPIC_DEFAULT_SONNET_MODEL_NAME:-Sonnet 5 (Global)}"' \
        'export ANTHROPIC_DEFAULT_HAIKU_MODEL="${ANTHROPIC_DEFAULT_HAIKU_MODEL:-eu.anthropic.claude-haiku-4-5-20251001-v1:0}"' \
        'export ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME="${ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME:-Haiku 4.5 (EU)}"' \
        'export ANTHROPIC_CUSTOM_MODEL_OPTION="${ANTHROPIC_CUSTOM_MODEL_OPTION:-global.anthropic.claude-opus-5}"' \
        'export ANTHROPIC_CUSTOM_MODEL_OPTION_NAME="${ANTHROPIC_CUSTOM_MODEL_OPTION_NAME:-Opus 5 (Global)}"' \
        'export ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION="${ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION:-Opus 5 via the global cross-region inference profile}"' \
        > /etc/profile.d/sch-env.sh \
    && chmod 644 /etc/profile.d/sch-env.sh \
    && cat /etc/profile.d/sch-env.sh >> /home/sch/.bashrc \
    && chown sch:sch /home/sch/.bashrc

# Run-once harness autostart for `sch run` (v13): sourced by login shells
# AFTER sch-env.sh ('e' < 'r' in profile.d ordering). Consumes the marker
# written by the shim's prepare-run action, cd's into the active worktree and
# exec's the workspace's harness; quitting the harness closes the session.
# Deliberately NOT appended to ~/.bashrc: `agentcore exec --it` spawns a
# LOGIN shell (profile.d fires), and profile.d-only avoids a double
# consumption path in login shells that also source ~/.bashrc.
COPY scripts/sch-run-profile.sh /etc/profile.d/sch-run.sh
RUN chmod 644 /etc/profile.d/sch-run.sh

# NOTE (sch-remote-ui-tunnel): no profile.d script / PTY marker for `sch
# attach`/`sch acp` — they run over `InvokeAgentRuntimeWithWebSocketStream`,
# handled entirely by the `@app.websocket` handler in app/main.py (no login
# shell involved at all). See design.md "Nota di revisione".

# NOTE: workdir must NOT be under /mnt/workspace — the session storage mount
# shadows it at runtime with an (initially empty) filesystem; init-workspace.sh
# creates /mnt/workspace/repo on first start.
USER sch

# --- Dev-environment build-time asserts (v29, add-dev-env-autonomy) -----------
# Runs AS THE RUNTIME USER, after the canonical ENV block, so it verifies the
# contract the seeded briefs promise to the harness rather than a root-only,
# pre-ENV approximation. Cheap and offline by construction (no package downloads):
#   1. the native build toolchain and `jq` are on sch's PATH, and the python3.11
#      C headers are really installed (python3.11-devel, not just python3.11);
#   2. `uv venv` with NO explicit interpreter yields a python3.11 environment
#      (UV_PYTHON) and downloads no managed CPython (UV_PYTHON_DOWNLOADS=never),
#      which is exactly the runtime-image spec scenario "No uv-managed Python
#      interpreter at runtime". Asserted here because the failure mode is silent:
#      AL2023's default `python3` is 3.9, so a missing UV_PYTHON would quietly
#      produce 3.9 project envs.
# The heavier end-to-end checks that need a real container (source-building a C
# extension, `npm ci` on a node-gyp module, seeded-template contents) live in
# image/test-local.sh.
RUN set -eu \
    && command -v gcc && command -v g++ && command -v make && command -v jq \
    && test -f "$(python3.11 -c 'import sysconfig; print(sysconfig.get_paths()["include"])')/Python.h" \
    && test "$(uv --version | awk '{print $2}')" = "${UV_VERSION}" \
    && test "${UV_PYTHON_DOWNLOADS}" = "never" \
    && cd /tmp && rm -rf /tmp/uv-policy-check \
    && uv venv /tmp/uv-policy-check \
    && /tmp/uv-policy-check/bin/python -c "import sys; assert sys.version_info[:2] == (3, 11), sys.version" \
    && test "$(readlink -f /tmp/uv-policy-check/bin/python)" = "$(readlink -f /usr/bin/python3.11)" \
    && ! test -d "${XDG_DATA_HOME}/uv/python" \
    && rm -rf /tmp/uv-policy-check

# Default output. Set RUNTIME_BASE=corporate-ca only for local builds behind a
# TLS-intercepting proxy; remote builds retain the portable runtime base.
FROM runtime
WORKDIR /app

EXPOSE 8080

CMD ["python3.11", "/app/main.py"]
