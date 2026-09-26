# MCP tooling, the Bedrock provider and the execution role

The AWS-native tooling baked into the image (MCP servers, AWS CLI), how the harnesses reach Amazon Bedrock, and what the runtime execution role allows. Normative behavior: [`docs/specs/platform/runtime-image.md`](specs/platform/runtime-image.md); shaping the role: [Runtime capability tuning](runtime-capability-tuning.md).

## MCP Tooling and Bedrock Provider

Per [`docs/specs/platform/runtime-image.md`](specs/platform/runtime-image.md), the image includes
AWS-native tooling ready to use, with no manual configuration.

> **Scope**: everything about MCP *servers* in this section applies to the
> `opencode` and `claude` harnesses only. **`pi` does not support MCP** (upstream
> design choice), so an `--harness pi` workspace has no `aws-docs`, `aws-mcp` or
> `context7` — see "Pi harness" → the MCP note under Multi-harness for what
> replaces them (the real AWS CLI in the agent's `bash` tool, same IAM boundary).
> The AWS CLI v2 bullet below and the execution-role permissions apply to every
> harness.

- **`aws-docs`** (`awslabs.aws-documentation-mcp-server`, pinned `1.1.30`): search/read
  AWS documentation. No credentials required.
- **`aws-mcp`** (managed AWS MCP Server via `mcp-proxy-for-aws-cli`, pinned `1.6.5`):
  execute AWS API operations using the execution role credentials (same credential chain
  already used for Bedrock). The proxy (local stdio) signs requests with SigV4 and
  forwards them to the managed endpoint (`https://aws-mcp.eu-central-1.api.aws/mcp`,
  operation region passed per-boot as `--metadata AWS_REGION`). **Read-only**:
  `--read-only` at proxy level (UX — clean rejection) *and* `ReadOnlyAccess` at IAM
  level on the execution role (real security boundary, see below; refinable with the
  `aws:ViaAWSMCPService` / `aws:CalledViaAWSMCP` condition keys the managed server
  attaches). This replaces `awslabs.aws-api-mcp-server` (pinned `1.3.46` until TASK-25),
  which AWS marked superseded — end of development 2026-07-15, removal 2027-07-15.
- **AWS CLI v2 as a system binary** (pinned via `ARG AWS_CLI_VERSION`, installed from the
  official versioned installer into `/usr/local/aws-cli` — local disk, not shadowed by the
  mount; `AWS_PAGER=""` so it never opens `less` in a PTY session). Added by
  `add-codebuild-image-rebuild` (prerequisite of `sch-build-image`, and useful on its own
  for ad-hoc AWS work in a shell). **Security note**: a shell-reachable `aws` **bypasses**
  the `--read-only` flag of the `aws-mcp` proxy. That flag is now a UX
  affordance only — the sole enforcement is IAM on the execution role. This scenario was
  already specified ([`docs/specs/platform/runtime-provisioning.md`](specs/platform/runtime-provisioning.md): "a mutation bypassing
  the MCP gate is denied by IAM"), but it moved from a test-only path to an ordinary one.
- aws-docs (self-hosted) and the proxy installed at build time with `uv tool install` (isolated venvs under `/opt/uv-tools`,
  entrypoint in `/usr/local/bin` — local disk, not shadowed by the mount) — no download
  at runtime, deterministic versions. Config seeded by `init-workspace.sh` (only if
  absent): `mcp` section with both `enabled: true`, and explicit `provider.amazon-bedrock`
  block (see below). Verified live on deployed runtime: `opencode mcp list` shows
  both `✓ connected`.
- **`context7`** (`@upstash/context7-mcp`, pinned `3.2.3`): built-in MCP server for
  up-to-date, version-specific library documentation (resolves a library name to a
  Context7-compatible ID, then fetches docs for that ID). Installed via `npm install -g`
  alongside `@opencode/cli` / `@anthropic-ai/claude-code`; entrypoint `context7-mcp` lands
  in `/usr/local/bin/` (local disk, not shadowed by the mount). **Disabled by default**
  on both harnesses — OpenCode uses the official `mcp.context7.enabled = false` flag
  (https://opencode.ai/docs/mcp-servers/), Claude Code ships the entry in `.mcp.json` but
  blocks it via the official `disabledMcpjsonServers: ["context7"]` in `settings.json`
  (anthropics/claude-code#4879, code.claude.com/docs/en/mcp). To enable:
  - **OpenCode**: edit `/mnt/workspace/state/config/opencode/opencode.json` and set
    `mcp.context7.enabled` to `true` (or delete the field). Restart the harness.
  - **Claude Code**: edit `/home/sch/.claude/settings.json` and remove `"context7"` from
    `disabledMcpjsonServers` (or move it to `enabledMcpjsonServers`). Restart the harness.
  Optional env var `CONTEXT7_API_KEY` (never seeded) raises the rate limit beyond the
  shared anonymous tier; pass it via `SCH_SESSION_ENV` / `agentcore` env injection.
- **Native web search/code search** (`websearch`/`codesearch`, OpenCode's built-in tools
  via Exa — not a separate MCP server): enabled with `OPENCODE_ENABLE_EXA=1`, required
  because they are disabled by default on providers other than the hosted `opencode` (here the
  default is `amazon-bedrock`). No API key needed: requests go through the OpenCode backend.
  Verified live: test prompt → the agent invokes `websearch` and responds with up-to-date
  information. To skip the permission confirmation prompt on first use, optional in
  `opencode.json`: `"permission": {"websearch": "allow", "codesearch": "allow"}`.
- **Development toolchain** (`uv` pinned, `make`/`gcc`/`gcc-c++`/`python3.11-devel`,
  `jq`) plus the uv Python policy (`UV_PYTHON=python3.11`,
  `UV_PYTHON_DOWNLOADS=never`) — see
  [Development environments](harnesses.md#development-environments-add-dev-env-autonomy).

### Provider `amazon-bedrock`: why it is now explicit in config

**Diagnosis (task 1.2/1.3, bump v6→v7)**: the `amazon-bedrock` provider was not
visible in OpenCode's `/models` picker despite `AWS_PROFILE=default` already being
baked into the image. Initial diagnosis (via **non-interactive** `agentcore exec`, without
`--it`): `AWS_PROFILE`/`AWS_REGION`/`XDG_*` were correctly inherited and
`opencode providers list` already detected `Amazon Bedrock` in "Environment" — it appeared
that the upstream autoload gate was already working (contrary to the hypothesis related to
anomalyco/opencode#35798, #8559).

**Correct diagnosis (v8, after a second bug report)**: that first verification was
**incomplete** — it did not reproduce the actual path used by `sch shell`. With a real
interactive session (`agentcore exec --it`, real pty), the login shell (`bash --login`)
spawned by AgentCore **inherits almost no container ENV variables**: only
`HOME`, `PATH`, `SHELL`, `TERM`, `USER` survive; `XDG_CONFIG_HOME`, `XDG_DATA_HOME`,
`OPENCODE_DB`, `SCH_WORKSPACE_ROOT`, `AWS_PROFILE`, `AWS_REGION` were **absent**
before the fix. This is specific behavior of the `agentcore exec --it` mechanism
(a plain `docker exec bash --login` locally, by comparison, inherits ENV correctly) —
not reproducible by `test-local.sh`, which uses `docker exec`. Real consequence: OpenCode
started in an interactive shell read config from `$HOME/.config/opencode/opencode.json`
(never seeded) instead of `/mnt/workspace/state/config/opencode/opencode.json` — hence
"no MCP tools visible" despite the config and MCP server connections being correct
(verifiable with `opencode mcp list` launched non-interactively).

**Fix** (v8, extended in v9 with `OPENCODE_ENABLE_EXA`, generalized in
sch-multi-harness to `scripts/harness-wrapper.sh`):
`scripts/harness-wrapper.sh` — which intercepts every `opencode` AND `claude`
invocation, regardless of the shell that launches it — now explicitly exports
`XDG_DATA_HOME`/`XDG_CONFIG_HOME`/`OPENCODE_DB`/`SCH_WORKSPACE_ROOT`/`AWS_PROFILE`/
`AWS_REGION`/`AWS_DEFAULT_REGION`/`OPENCODE_ENABLE_EXA` (with `:-` fallback, so it does
not override already-correct values) right before the `exec` of the real binary;
for `harness=claude` it additionally exports `CLAUDE_CONFIG_DIR`/
`CLAUDE_CODE_USE_BEDROCK`/`ANTHROPIC_DEFAULT_*_MODEL`.
`/etc/profile.d/sch-env.sh` was extended with the same set of variables, to also cover
other commands launched in the interactive shell (not just `opencode`). Verified with a
real `agentcore exec --it` session (via `expect`): `printenv` shows all the correct
variables, the OpenCode log loads config from `/mnt/workspace/state/config/opencode/...`,
`opencode mcp list` shows `aws-docs`/`aws-mcp` connected, and a test prompt correctly
invokes the `websearch` (Exa) tool.

The seeded config still declares `provider.amazon-bedrock` **explicitly**
(design D3), independently of this fix: it makes the behavior deterministic even
if the upstream autoload gate changes in the future, and it is the method recommended by
the OpenCode documentation.

**Troubleshooting — `/models` or MCP tools are not visible**:
1. Check the image version in use: `printenv SCH_IMAGE_VERSION` (must be ≥ `v8`
   to have the ENV fix in the interactive shell).
2. Seeded config: `cat /mnt/workspace/state/config/opencode/opencode.json` must
   contain `provider.amazon-bedrock.options.region` and the `mcp` section.
3. Shell signals: `printenv | grep -E 'XDG|OPENCODE_DB|AWS_PROFILE|AWS_REGION'` — must
   all be set (via `/etc/profile.d/sch-env.sh` + `~/.bashrc` by `sch`).
4. `opencode mcp list` and `opencode providers list` for connection/autoload status.
5. OpenCode log: `grep loading $XDG_DATA_HOME/opencode/log/opencode.log` — must show
   `/mnt/workspace/state/config/opencode/...`, not `$HOME/.config/opencode/...`.

### Execution Role Permissions

The role (`infra/agent_runtime.yaml`) receives the AWS managed policy
`arn:aws:iam::aws:policy/ReadOnlyAccess` in addition to the existing inline policies
(Bedrock invoke, ECR pull, CloudWatch logs — unchanged). The stack update is a
non-destructive `Modify` (no `Replacement`, verified via test changeset).

`bedrock:InvokeModel`/`bedrock:InvokeModelWithResponseStream` are granted on
**all** inference profiles (`inference-profile/*`, `application-inference-profile/*`)
and **all** foundation models (`foundation-model/*`), in any region — not only the
two pinned default profiles (`MainInferenceProfileId`/`SmallInferenceProfileId`,
kept as informational defaults for README/seeded config). Necessary because OpenCode
allows selecting any Bedrock model from the `/models` picker (not just the two
pinned ones), and a different model than the explicit ones failed with `AccessDenied` IAM.

OpenAI-family models (e.g. GPT-5.5) are **not** served by the classic
`bedrock-runtime` plane above. Amazon Bedrock routes them through **Project
Mantle**, its OpenAI-compatible engine (`bedrock-mantle.<region>.api.aws`),
authorized by a separate action namespace (`bedrock-mantle:*`) on a `project`
resource. The role therefore also grants `bedrock-mantle:CreateInference` (plus
`Get`/`Cancel`/`DeleteInference`, `ListModels`, `GetModel`) on `project/default`
in the deploy region — without it, selecting GPT-5.5 in the `/models` picker
fails with `AccessDenied` on `bedrock-mantle:CreateInference` while Claude/etc.
on the classic plane keep working. Note `ReadOnlyAccess` does **not** cover this
(`CreateInference` is a write action); if your account applies an IAM
permissions boundary, make sure it permits this action — the identity-policy
grant alone is then sufficient.

> **Security note**: `ReadOnlyAccess` is a broad policy (covers data reads too,
> e.g. `s3:GetObject`, not just metadata) — it exposes potentially sensitive account
> data to anyone using the agent. Accepted on a development account;
> for later phases consider a scoped policy or a dedicated account.
> `--read-only` (formerly `READ_OPERATIONS_ONLY`) on the MCP server side **is not a security boundary** (it relies on
> the server's own operation classification, bypassable by anyone with direct shell access
> to the credentials) — the real boundary is always IAM. Similarly, the open Bedrock
> invocation to all models/regions widens the cost surface (no per-model spend
> limit) — acceptable for now, to be revisited with cost guardrails in later
> phases. Headless tasks amplify this surface because they can run unattended for
> up to 7 hours with auto-approved permissions.

### Operational Note: `opencode run` non-interactive with both MCP servers enabled

Discovered during end-to-end verification (task 6.6, re-running `bin/verify-persistence.sh`
on image `v7`): an `opencode run` (non-interactive mode, single prompt) invoked
**in foreground** via `agentcore exec` **without an interactive pty** (no `--it` option)
can hang indefinitely when both `aws-docs` and `aws-mcp` are enabled
simultaneously. Isolated empirically: it works in background (`nohup ... &`),
works with only one MCP server enabled, works with both if stdin is redirected
from `/dev/null`. Cause: stdin/pty interaction between non-interactive `agentcore exec`
and OpenCode's two local MCP subprocesses — not a credentials/IAM issue.
**Does NOT affect normal interactive use** (`sch shell` → OpenCode TUI uses a real pty
via `--it`). Workaround for scripts/automation that invoke `opencode run` non-
interactively via `agentcore exec`: redirect stdin from `/dev/null`
(`opencode run ... < /dev/null`) — applied in `bin/verify-persistence.sh`.
