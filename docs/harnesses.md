# Harnesses: OpenCode, Claude Code and Pi

One image, three coding-agent harnesses chosen per workspace. What each one supports, how the choice is persisted, the custom agents the image seeds, and the development-environment contract unattended runs follow. Normative behavior: [`docs/specs/platform/harness-selection.md`](specs/platform/harness-selection.md) and [`docs/specs/platform/runtime-image.md`](specs/platform/runtime-image.md).

## Multi-harness

Per [`docs/specs/platform/harness-selection.md`](specs/platform/harness-selection.md), `sch` supports a
**per-workspace persistent choice of coding agent harness** — `opencode`,
`claude` or `pi` — with mutual
exclusivity (a workspace is exactly one harness) and the same durability and
headless guarantees for all three. The harness
choice is workspace identity, not per-invocation: it is persisted next to the
`runtimeSessionId` in `~/.config/sch/workspaces/<ws>` and propagated in every
shim-bound payload so a fresh microVM (idle timeout, `MaxLifetime`, version
update) restores it from the mount marker / L2 manifest without
re-specification.

**The three harnesses are deliberately NOT at feature parity.** The contract
admits different levels per harness, and each one degrades explicitly rather
than silently:

| capability                            | `opencode` | `claude`            | `pi`                       |
| ------------------------------------- | ---------- | ------------------- | -------------------------- |
| interactive TUI (`sch shell`/`run`)   | ✅         | ✅                  | ✅                         |
| headless detached task (`sch task`)   | ✅         | ✅                  | ✅                         |
| `--continue` session resume           | ✅         | ✅                  | ✅                         |
| `--model` per invocation              | ✅         | ✅                  | ✅ (provider/model pair)   |
| L2 checkpoint/restore of agent state  | ✅         | ✅                  | ✅ (`pi.tar.gz`)           |
| MCP servers (aws-docs/aws-mcp/ctx7)   | ✅         | ✅                  | ❌ **not supported by Pi** |
| `sch web`                             | ✅         | ❌                  | ❌                         |
| `sch attach`                          | ✅         | ❌                  | ❌                         |
| `sch acp` (editor integration)        | ✅ native  | ✅ adapter          | ❌ no ACP agent            |
| `sch handoff`                         | ✅         | ❌                  | ❌                         |
| Telegram milestones                   | ✅         | ✅                  | ✅ end-of-turn only        |
| Telegram remote tool approval         | ✅ detached only | ✅ detached only   | ❌ native no-prompt       |
| Telegram free-text injection          | ✅         | ❌                  | ❌                         |
| native permission prompt in the TUI   | ✅         | ✅                  | ❌ **none, by design**     |

The `❌` cells are **refused with an explicit, actionable error and without any
runtime call** — never a hang, never a silent no-op. `sch web`/`sch attach` are
refused because those harnesses have no client/server split; `sch acp` because
no ACP agent exists for them in the microVM; `sch handoff` because it transfers
an OpenCode session specifically.

**On MCP for `pi`**: Pi does not support MCP at all (by design, upstream). The
practical impact is smaller than the row suggests — the Pi agent still has the
real **AWS CLI v2** in its `bash` tool, which is the same power as the `aws-mcp`
server behind the same IAM boundary, plus native web fetch. What is genuinely
lost is `aws-docs` and `context7`. This is an accepted limit; an
extension-wrapper bridging MCP into Pi is a possible future change.

### OpenSpec workflows

The image generates the complete OpenSpec workflow set from its pinned
`OPENSPEC_VERSION` for the two harnesses the CLI can generate for (`opencode`
and `claude` — see the `pi` note at the end of this section). In an OpenCode
workspace, `/opsx-*`
commands and their `openspec-*` skills are seeded into the persistent global
OpenCode config under `${XDG_CONFIG_HOME}/opencode`, so they are available even
when the workspace was created without `--sync` and the repository has no
`.opencode/` directory. The runtime never runs `openspec init` during bootstrap
and never creates OpenSpec integration files in the worktree.

Repository-scoped `.opencode/commands` and `.opencode/skills` continue to use
OpenCode's native project-over-global precedence and are never modified by the
seed. Global files are copied individually only when absent: operator edits
remain unchanged across boots, while deleting a generated global command or
`SKILL.md` causes just that file to be restored from the image on the next boot.

**`pi` gets no generated workflow artifacts** — a declared gap, not a Pi
limitation: Pi does read global prompt templates (`$PI_CODING_AGENT_DIR/prompts/`)
and global skills (`$PI_CODING_AGENT_DIR/skills/`), but the pinned OpenSpec CLI
has no `--tools pi` generator, so `add-pi-harness` deliberately seeds none. On a
pi workspace the workflows are driven by invoking the `openspec` CLI (on `PATH`
in every microVM regardless of harness) from the agent's `bash` tool. Porting
the workflow bodies to Pi prompt templates is a possible follow-up change.

### Choosing and switching harness

- **New workspace, default**: `sch shell brand-new-ws` (no `--harness`) →
  `opencode` (override with `SCH_DEFAULT_HARNESS`). The optional central
  registry uses the **same** default, so a new workspace gets the same harness
  whichever component registers it first.
- **New workspace, explicit**: `sch shell brand-new-ws --harness opencode`
  (or `--harness claude`, or `--harness pi`).
- **Existing workspace**: the persisted harness is reused; `--harness` is
  optional and only accepted when congruent with the persisted value.
  `sch shell myws --harness <other>` is rejected with an explicit error and
  no state mutation (client-side guard before any shim call; the shim
  enforces it again on the `task` payload against the mount marker).
- **Switching harness on an existing workspace** requires a new workspace
  name (or the parked "reset for harness switch" path, not yet implemented).
  This is by design: mutual exclusivity prevents two harnesses from writing
  to the same session's state concurrently.
- **Binary availability follows the binding**: inside the microVM the
  dispatcher (`image/scripts/harness-wrapper.sh`) refuses to launch the
  binary of any non-bound harness (exit 127 with an explicit message), so a
  `--harness opencode` workspace has no working `claude` or `pi` command, and
  likewise for the other two. Escape hatch: an explicitly exported
  `SCH_HARNESS` (precedence rule 1, also used by the shim's headless
  subprocess) is honored when it matches the invoked binary.

### Legacy workspace reconcile (`upgrade_reconcile`)

Workspaces whose index was written before the multi-harness change (no
`harness` field) are reconciled to `harness=opencode` on first access after
the deploy. The reconcile is a non-destructive write of the
`harness` field on the existing index file; the `runtimeSessionId` is never
touched. Passing `--harness claude` (or `--harness pi`) on that very first
access is an explicit upgrade handoff. **Adding `pi` did not change this
default**: a legacy index still reconciles to `opencode`.

### Per-harness headless argv (auto-approval, argv-only)

| harness   | headless argv (built by the shim)                                                                                  | session-resume flag  | auto-approval flag (argv-only)   |
| --------- | ------------------------------------------------------------------------------------------------------------------ | -------------------- | -------------------------------- |
| opencode  | `opencode run [--session <id>] [--model <id>] [--variant <v>] --agent remote-auto --auto <prompt>`                           | `--session <id>`     | `--auto`                         |
| claude    | `claude -p [--resume <id>] --agent remote-auto --dangerously-skip-permissions <prompt>`                            | `--resume <id>`      | `--dangerously-skip-permissions` |
| pi        | `pi -p [--session <path>] [--provider amazon-bedrock --model <id>] --append-system-prompt <role file> <prompt>`     | `--session <path>`   | **none — by design**             |

- Auto-approval is **only on the headless argv**; the TUI path
  (`sch shell`/`sch open` → `opencode`/`claude` in the interactive shell)
  launches the binary with no auto-approval flag, as the original interactive design required.
- **`pi` has no auto-approval flag because it has no permission prompt at
  all** — not in headless, not in the TUI. The requirement "a headless task
  never suspends waiting for a confirmation nobody is there to give" is
  therefore satisfied *by construction*, and the empty cell above is
  conformance, not a gap. SCH does not add approval gating to Pi in any mode.
- **Agent selection is argv-only too**: headless OpenCode and Claude runs
  select the seeded `remote-auto` custom agent (unattended-execution
  contract: never wait for input, verify, commit, compact final report), while
  each interactive path keeps its seeded `remote-interactive` default.
  Override both of them with `SCH_TASK_AGENT` (empty string = use the
  harness default). Unknown names retain native behavior: OpenCode warns and
  falls back, while Claude reports an error. On `pi` the same variable acts as
  an on/off switch rather than a name (there is no agent to name): an empty
  `SCH_TASK_AGENT` also suppresses the `--append-system-prompt` role file.
  **Pi has no agent files or subagents**, so the same two role contracts are
  rendered as *system prompts*: the shim passes
  `--append-system-prompt <PI_CODING_AGENT_DIR>/roles/remote-auto.md` on the
  headless path and the `sch run` autostart passes `roles/remote-interactive.md`
  on the interactive one. Declared limit: unlike OpenCode (`edit: ask`, deny
  rules) the read-only posture of `remote-interactive` on pi is **not
  enforceable** — it is a prompt contract, and the enforcing boundary stays
  IAM (the same position already taken for claude, where per-command deny
  rules cannot be expressed either).
- All three harnesses run headless with `stdin < /dev/null` (D7 default-on; see
  "Operational Note" below and the multi-harness open questions in the
  change's `design.md`).

### Per-harness `--continue` handoff

`sch task <ws> --continue "..."` resumes the **latest session of the
workspace's persisted harness**:

- **opencode**: queries the `session` table in `opencode.db` for the row
  with the latest `time_updated` in the worktree's `directory`, then passes
  `--session <id>` to `opencode run`. Without an explicit `--model`, the
  resumed session's own stored model and reasoning-effort variant are
  forwarded as `--model <id>`/`--variant <v>`, so a headless `--continue`
  keeps the model and effort selected in the TUI (a model-less prompt would
  otherwise resolve to the `remote-auto` agent's configured model and
  default effort). An explicit `--model` wins; an unreadable session row
  degrades to the harness default.
- **claude**: `ls -t $CLAUDE_CONFIG_DIR/projects/<encoded-cwd>/*.jsonl | head -1`,
  basename-strip the `.jsonl` to get the resume handle, then pass
  `--resume <id>` to `claude -p`. `<encoded-cwd>` is Claude's path encoding
  (slashes → hyphens), derived from the worktree's absolute path. An empty
  match degrades to a fresh session (same contract as OpenCode).
  The resumed process receives `--agent remote-auto`: conversation context is
  preserved, while new turns use the unattended agent contract.
- **pi**: the most recent `*.jsonl` under
  `$PI_CODING_AGENT_DIR/sessions/<cwd-encoded>/`, whose first line is validated
  as a `{"type":"session",...}` header carrying an `id`, then passed as
  `--session <absolute path>` (Pi accepts a path or a partial UUID; the path
  needs no id extraction). `<cwd-encoded>` is Pi's own encoding of the worktree
  path — leading separator dropped, `/`, `\` and `:` mapped to `-`, wrapped in
  `--` — so `/mnt/workspace/repo` becomes `--mnt-workspace-repo--`.
  Any mismatch (no directory, no files, malformed or foreign header) degrades
  to a fresh session, same contract as the other two.
  Resuming **appends** to the same session file rather than forking it.

  > **Pi is pre-1.0 and this on-disk layout is treated as unstable.** It is
  > verified against the pinned `PI_VERSION` and must be re-checked at every
  > bump. If it starts churning, the documented fallback is to drop the resolver
  > and pass Pi's native `-c` (resume the latest session for the cwd) instead:
  > same behavior, minus the observability of the resolved session id in the
  > task status.

### Claude Code state on local disk + L2 replica (no fcntl hazard)

Claude Code stores session transcripts as
`~/.claude/projects/<encoded-cwd>/<session-id>.jsonl` and per-project config
in `~/.claude`. The session-storage mount does NOT support POSIX `fcntl`
locks (`ENOLCK`) — the same hazard already documented for `opencode.db` —
so Claude state follows the **same "live on local disk, mirrored to the
mount via the L2 loop" pattern** as `opencode.db`:

- `CLAUDE_CONFIG_DIR` points at the local-disk root (`$HOME/.claude`); the
  `claude` binary writes JSONL transcripts there.
- The shim's periodic checkpoint loop mirrors `$HOME/.claude` →
  `/mnt/workspace/state/claude` (rsync-style, mtime-preserving) at every
  tick and on the forced checkpoint at task terminal state.
- On empty-L1 boot, the shim downloads `claude.tar.gz` from S3, extracts
  it to `/mnt/workspace/state/claude`, then un-mirrors it back to
  `$HOME/.claude` BEFORE writing the `.ready` marker — so the `claude`
  binary finds the restored transcripts on first launch.
- The `claude` wrapper dispatcher gates on `/home/sch/.claude/.ready`,
  written only after the seed/restore verify loop confirms the project MCP
  file and all three user-scoped Claude templates.

### Bedrock mode (no OAuth)

Claude Code is wired exclusively to Bedrock via `CLAUDE_CODE_USE_BEDROCK=1`
(image ENV, override-compatible) **unless the user running the session has an
Anthropic API key in `~/.sch/env`** — see "Provider API keys (per-user)"
above: with `ANTHROPIC_API_KEY` configured, the dispatcher switches the `claude`
harness to the direct Anthropic API for that user's sessions and everything below stops applying. It rides the same execution-role + IMDSv2
credential chain as OpenCode's `amazon-bedrock` provider — no OAuth token,
no Anthropic Console login. This explicitly sidesteps the known
OAuth-token-refresh-in-headless failure (anthropics/claude-code#28827): a
7-hour headless task under Bedrock mode has no OAuth token to expire.
All Claude Code model aliases are pinned to Bedrock inference profiles
(with `_NAME` display labels for the `/model` picker):

| `/model` entry     | alias env                        | inference profile                              |
| ------------------ | -------------------------------- | ---------------------------------------------- |
| Fable 5 (Global)   | `ANTHROPIC_DEFAULT_FABLE_MODEL`  | `global.anthropic.claude-fable-5`              |
| Opus 5 (EU)        | `ANTHROPIC_DEFAULT_OPUS_MODEL`   | `eu.anthropic.claude-opus-5`                   |
| Sonnet 5 (Global)  | `ANTHROPIC_DEFAULT_SONNET_MODEL` | `global.anthropic.claude-sonnet-5`             |
| Haiku 4.5 (EU)     | `ANTHROPIC_DEFAULT_HAIKU_MODEL`  | `eu.anthropic.claude-haiku-4-5-20251001-v1:0`  |
| Opus 5 (Global)    | `ANTHROPIC_CUSTOM_MODEL_OPTION`  | `global.anthropic.claude-opus-5`               |

Any other profile in the account can be selected with
`/model <inference-profile-id>` (on Bedrock the string is passed through
unchecked). NOTE: the `/model` picker lineup is baked per Claude Code
binary version; the image pins `CLAUDE_CODE_VERSION=2.1.210`;
keeping the picker current over time means bumping that pin. The execution
role's Bedrock grant already covers all inference profiles and foundation
models, so no new `bedrock:InvokeModel` grant is needed.

### Pi harness (`--harness pi`)

Pi (`@earendil-works/pi-coding-agent`, MIT, **pre-1.0**) is installed at a
pinned `PI_VERSION` with `npm install -g --ignore-scripts`. Because it is
pre-1.0, everything the SCH contract depends on is asserted at **build** time —
the version pin, the presence of `-p`, `--session`, `--append-system-prompt`,
`--provider`, `--model`, `--mode`, `-e`, and that the image's Node satisfies the
package's own `engines` (read from the installed `package.json`, not hardcoded).
A version bump that breaks any of them fails the image build instead of
producing a runtime where headless tasks or role prompts silently do not work.

#### Route note: `settings.json` is a functional requirement, not a default

> ⚠️ **Pi's documented default for `--provider` is `google`, and without seeded
> settings a fresh Pi in this microVM lands on an unusable model.** The seeding
> below is what makes the harness work at all — treat it as part of the install,
> not as a cosmetic preference.

The exact failure mode is worth recording, because it is *not* the obvious one
and it is what a future `PI_VERSION` bump has to be re-checked against. Pi
resolves the model to use in this order (`core/model-resolver.js`,
`findInitialModel`): explicit `--provider`/`--model` → `--model` scope patterns
→ **the `defaultProvider`/`defaultModel` in `settings.json`, but only if that
provider has configured auth** → otherwise *the first entry of its own
hardcoded `defaultModelPerProvider` table that happens to be authenticated* →
otherwise the first available model at all.

Measured on the pinned version (0.84.2) inside a real SCH microVM, with an empty
`PI_CODING_AGENT_DIR` and no flags:

- `pi auth check --provider amazon-bedrock` → `ready`, because the execution
  role's IMDSv2 credentials satisfy Bedrock through the existing
  `AWS_PROFILE=default` bridge. `--provider google` → `not_ready`. So the
  documented `google` default never actually gets selected here; step 4 of the
  chain skips every unauthenticated provider.
- Instead, `amazon-bedrock` being the first authenticated provider in Pi's own
  table, the fallback picks that table's Bedrock entry:
  **`us.anthropic.claude-opus-4-6-v1`** — a `us.` cross-region inference
  profile, invoked from an `eu-west-1` deployment.
- The turn therefore fails with Bedrock's
  `Validation error: The provided model identifier is invalid.` — an opaque
  error at the *end* of a turn, not a startup refusal, and with an exit status
  that does not obviously say "misconfigured".

With the template seeded (`defaultProvider: amazon-bedrock`,
`defaultModel: eu.anthropic.claude-sonnet-4-6`) the very same invocation
resolves to `amazon-bedrock/eu.anthropic.claude-sonnet-4-6` and completes
normally. Two consequences the seeding is designed around:

1. **The geography prefix must match the deployment region**, because Pi's
   `settings.json` carries a bare model id with no provider-options block to
   attach a region to (unlike `opencode.json`). `init-workspace.sh` therefore
   *derives* the prefix from the deploy region (`eu-*` → `eu.`, `us-*` → `us.`,
   `ap-southeast-2/4` → `au.`, `ap-northeast-1/3` → `jp.`, anything else →
   `global.`, which is invokable everywhere) instead of hardcoding one.
   Override per deployment with `SCH_PI_DEFAULT_MODEL` (taken as an opaque
   fully-qualified Pi model id, no rebuild needed); `infra/deploy.sh` maps it
   to the `PiDefaultModel` stack parameter.
2. **A missing/emptied `settings.json` is a functional regression, not a
   cosmetic one** — which is exactly why `settings.json` is the seed-verify
   canary that gates `/home/sch/.pi/agent/.ready`, and why deleting the file
   re-seeds it on the next boot.

`init-workspace.sh` seeds four artifacts into `PI_CODING_AGENT_DIR`
(`/home/sch/.pi/agent`, local disk):

| artifact                    | purpose                                                                                     |
| --------------------------- | ------------------------------------------------------------------------------------------- |
| `settings.json`             | `defaultProvider: amazon-bedrock` + region-appropriate `defaultModel` inference profile, `defaultProjectTrust: always`, telemetry off |
| `AGENTS.md`                 | the microVM environment brief — Pi reads `AGENTS.md`/`CLAUDE.md` natively                    |
| `roles/remote-{auto,interactive}.md` | the two role contracts, as `--append-system-prompt` inputs (Pi has no agent files)  |
| `extensions/sch-pi.ts`      | the SCH extension: Telegram milestones + busy keep-alive                                     |

Seeding follows the same ownership rules as the other harnesses: **per-file,
never overwriting** for settings/brief/roles (an operator edit survives every
boot; deleting a file re-seeds the current image version at the next one), and
**sha256-sidecar refresh** for the extension, so existing workspaces are not
pinned forever to the extension version that first seeded them while an
operator's own extension file is left untouched.

`defaultProjectTrust: always` plus an additive pre-seeded `trust.json` entry for
the canonical worktree means **no non-interactive path can ever park on Pi's
project-trust prompt**. A deliberate `false` decision by the operator is
preserved.

Bedrock needs no configuration beyond this: Pi's native `amazon-bedrock`
provider rides the same `AWS_PROFILE=default` → `credential_process` → IMDSv2
bridge as the other harnesses. Verified empirically in a live microVM — no
static keys, and `auth.json` stays free of AWS credentials.

#### Pi state on local disk + L2 replica

Identical pattern to Claude Code (the mount does not support POSIX `fcntl`
locks): the live write path is `PI_CODING_AGENT_DIR` on local disk, the shim's
checkpoint loop mirrors it to `/mnt/workspace/state/pi` at every tick and on the
forced terminal checkpoint, and `pi.tar.gz` is uploaded as a dedicated manifest
artifact. On empty-L1 boot the artifact is extracted and un-mirrored back to
local disk **before** `/home/sch/.pi/agent/.ready` is written, so the `pi`
binary finds its restored JSONL sessions on first launch. Pi stores only JSONL
(no SQLite), so no WAL/VFS special-casing is needed. The `pi.tar.gz` artifact is
present **only** on `harness=pi` manifests, and `claude.tar.gz` is absent from
them — symmetrically.

#### Security posture: no permission prompt at all

> ⚠️ **Pi has no permission prompt, in headless *or* in the TUI.** Unlike
> OpenCode (which can `ask` per tool and express deny rules) and Claude Code
> (which prompts interactively), a Pi session executes `bash`, `write` and
> `edit` immediately, with no confirmation step to bypass — because there is
> nothing to bypass.

This is a deliberate upstream design choice, not a misconfiguration, and it
means the interactive risk on a `pi` workspace is **higher** than on the other
two. The project's position is unchanged and is what makes this acceptable: the
**enforcing boundary is IAM and the microVM**, never the harness UI (the same
reasoning already documented for the shell-reachable AWS CLI, which likewise
bypasses the `aws-mcp` server's read-only flag). Two mitigations, both partial:

1. the seeded `remote-interactive` role prompt states the read-only default and
   says so explicitly — it is a contract the agent keeps, not a gate;
2. use OpenCode or Claude when per-tool confirmation is required; SCH does not
   add an approval layer to Pi.

#### Telegram: milestones, no remote approval

The seeded `sch-pi.ts` extension covers two duties and never touches the
network itself (local file drops only, exactly like the OpenCode plugin and the
Claude hooks):

- **Milestones**: end-of-turn text (accumulated across `turn_end`, published
  once on `agent_settled`). Pi has no todo tool and no "waiting for input"
  hook, so those two milestone categories **degrade silently** — the guaranteed
  minimum for pi is end-of-turn.
- **Busy keep-alive**: activity markers in `SCH_ACTIVITY_DIR`, same contract as
  the OpenCode plugin, refreshed during an active turn.

Pi never emits Telegram approval requests. `bash`, `write` and `edit` retain
Pi's native no-prompt behavior in headless, connected TUI and detached TUI.
Telegram configuration does not alter tool execution; IAM and the microVM are
the enforcing boundaries.

Free-text messages sent to a workspace with an active interactive Pi session
are **not injected** (same accepted limit as claude): the topic answers with the
limit and the alternatives (reconnect with `sch run`, or wait for the turn to
end and send it as a `task --continue` follow-up). Remote tool approval is not
available for Pi.

### Verification

- `./bin/verify-headless-tasks.sh <ws> --harness <opencode|claude|pi>` runs the
  full headless flow (submit → detached completion → offline status →
  checkpoint capture → concurrency rejection → orphan reconciliation →
  brainstorm handoff → application timeout) for a single harness. The
  OpenCode path is byte-identical to the pre-multi-harness flow when
  `--harness opencode` is passed.
- `./bin/verify-multi-harness.sh <oc-ws> <cl-ws> [<pi-ws>]` runs the harness
  paths end-to-end plus the multi-harness-specific assertions:
  mutual-exclusivity rejection (a task with a divergent `--harness` is
  rejected, for every ordered pair) and upgrade-reconcile (a legacy workspace
  index without `harness` is reconciled to `opencode` on first access;
  `--harness claude`/`--harness pi` on that first access persists that
  harness). With a third argument it also covers the pi path and the explicit
  refusals of `sch web`/`attach`/`acp`/`handoff` on a pi workspace.
- `./bin/verify-pi-interactive.sh <pi-ws> [--model <id>]` guides the live Pi
  TUI check that cannot be automated without an operator: Bedrock response,
  per-run model selection, detach, reattach, and retained conversation context.

## OpenCode custom agents (microVM-tuned, sch-remote-agents)

The image ships two custom **primary** OpenCode agents plus a global
`AGENTS.md`, all templated under `image/opencode-templates/` and seeded
per-file (never overwriting operator edits) by `init-workspace.sh` into
`/mnt/workspace/state/config/opencode/`:

| file                          | role                                                                                                                                                              |
| ----------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `AGENTS.md`                   | microVM environment brief (non-root, 8h lifetime, `/mnt/workspace` persistence, IMDS/IAM boundary, read-only `aws-mcp`). Appended to EVERY agent's system prompt. |
| `agents/remote-interactive.md`| interactive brainstorming/requirements sessions: read-only posture (`edit: ask`, read-only bash/AWS allowed, everything else asks). Seeded `default_agent`.       |
| `agents/remote-auto.md`       | unattended headless execution: never waits for input (`question` tool off), verifies + commits, force-push denied. Selected by the shim via `--agent remote-auto`. |

Design properties:

- The agent prompts carry only the **behavioral contract**; environment
  facts live once in `AGENTS.md` (OpenCode concatenates agent prompt +
  baseline + instructions, so nothing replaces the built-in coding prompt).
- Seeding is per-file and independent of `opencode.json` existing, so
  pre-existing workspaces pick the agents up on their next boot. The one
  gap: an existing `opencode.json` is never touched, so `default_agent`
  stays unset there — select `remote-interactive` in the TUI (tab) or add
  the field manually.
- The interactive/asynchronous split mirrors the recommended workflow above:
  brainstorm with `remote-interactive` in `sch shell`/`sch run`, then hand
  the written outcome to `sch task` which runs `remote-auto` with `--auto`.
- Explicit `deny` rules (e.g. `git push --force*`) are enforced even under
  `--auto`; the real security boundary remains the execution role's IAM.

## Claude Code custom agents

The image also ships `image/claude-templates/CLAUDE.md` and two user-scoped
main agents. `init-workspace.sh` seeds missing files into
`$CLAUDE_CONFIG_DIR` without replacing operator edits and atomically adds
`"agent": "remote-interactive"` to parseable settings only when absent.

| file | role |
| --- | --- |
| `CLAUDE.md` | Global microVM brief shared by both main agents. |
| `agents/remote-interactive.md` | Evidence-based exploration with a read-only default posture and normal interactive permission prompts. |
| `agents/remote-auto.md` | Unattended implementation, verification, commits, explicit assumptions, and compact reporting; `AskUserQuestion` is unavailable because its explicit tool list omits it. |

Interactive `sch run`/`sch shell` launch Claude without `--agent` or
`--dangerously-skip-permissions`, so the preserved settings default applies.
Detached `sch task`, including `--continue`, explicitly passes
`--agent remote-auto --dangerously-skip-permissions`; stdin remains closed.
Unlike OpenCode's command-pattern permission map, Claude agent frontmatter
cannot express equivalent per-command Bash denies. The prompt forbids
force-push, while IAM remains the actual security boundary.

## Development environments (`add-dev-env-autonomy`)

Detached runs used to **stop** when a task needed a project environment that was
not materialized yet — a real occurrence: a `remote-auto` run halted because
`tunnel/node_modules` was absent and `boto3` was not installed, even though the
image already shipped Node 22/npm, Python 3.11 and `uv`. The harness had the
tools but no *mandate* to use them, and the image lacked a few pieces such work
predictably hits. Both halves are now fixed.

### The contract in the seeded templates

The global briefs (`image/opencode-templates/AGENTS.md`,
`image/claude-templates/CLAUDE.md`, `image/pi-templates/AGENTS.md` — appended to
**every** agent's system prompt on their harness) carry a
`## Development environments` section with the same
normative content on all three harnesses (each one build-asserted):

- **Python**: `uv sync` when there is a `pyproject.toml`/`uv.lock`, otherwise a
  project-local `uv venv .venv` + `uv pip install`. **Never** install into the
  system interpreter.
- **Node/TypeScript**: lockfile first — `npm ci` when a lockfile exists,
  `npm install` only when it does not; respect the package manager the project
  declares.
- **Reproducibility**: commit lockfiles; add a one-command bootstrap when a
  project lacks one. The microVM can be recycled at any time, so an environment
  must be recreatable from the repository alone.
- **Project-local and gitignored**: `.venv/`, `node_modules/` inside the
  worktree; never depend on state outside the repository.

The per-agent templates carry only the behavioral delta: `remote-auto` treats a
missing local dependency as **a bootstrap to run, not a blocker to report**,
while `remote-interactive` keeps its read-only posture (`bash: ask`) and
bootstraps environments **only on explicit request**.

Seeding follows the usual never-overwrite rule: fresh workspaces (and workspaces
missing a template file) get the new text; **pre-existing workspaces keep their
current briefs**. Deleting a seeded template file re-seeds the current version at
the next boot.

### The toolchain baked into the image

| tool | why |
| --- | --- |
| `make`, `gcc`, `gcc-c++`, `python3.11-devel` | Python packages without an aarch64 wheel (C extensions) and `node-gyp` native modules compile in-place. Deliberately not `dnf groupinstall "Development Tools"` (rpm-build/autotools/bison, against the image's minimalism). |
| `jq` | JSON handling in agent shell scripts. |
| `uv`, **pinned** via `ARG UV_VERSION` (`0.12.5`) | Until v29 this was the only unpinned tool in the image, so environment-resolution behavior could silently change between rebuilds. The build asserts `uv --version` equals the pin, the same discipline as the AWS CLI / OpenCode / Claude Code / openspec / backlog pins. |

Like every other dnf package in the image (git, nodejs22, python3.11), the
toolchain packages follow the AL2023 repo snapshot rather than explicit rpm
version pins.

**uv Python policy.** Two image ENV vars make the runtime behavior explicit:

- `UV_PYTHON_DOWNLOADS=never` — uv must never download a managed CPython. It
  would land under `XDG_DATA_HOME`, i.e. on the **checkpointed** mount (tens of
  MB per interpreter), and build-time verification had already found uv-managed
  interpreters unusable across users (root-owned, unreadable by `sch`).
- `UV_PYTHON=python3.11` — required for the intended behavior to actually
  happen: AL2023's default `python3` is **3.9**, so a bare `uv venv` would
  otherwise quietly produce a 3.9 environment.

Both are also exported by `/etc/profile.d/sch-env.sh` and
`scripts/harness-wrapper.sh`, because `agentcore exec --it` login shells do not
inherit the container ENV (see the v8 note in `image/Dockerfile`) — otherwise the
policy would not hold in exactly the sessions where an agent runs the bootstrap.

The Dockerfile asserts all of this at build time, as the runtime user and after
the canonical ENV block: toolchain and `jq` on `sch`'s `PATH`, `Python.h`
present, `uv --version` == pin, and a bare `uv venv` resolving to
`/usr/bin/python3.11` with no managed CPython under `XDG_DATA_HOME`. The heavier
container-level checks (source-building a C extension, `npm ci` driving node-gyp,
seeded-template contents on both harnesses) live in `image/test-local.sh`
(section 11, plus sections 9 and 10b).

**Known trade-off (follow-up):** project-local `.venv/` and `node_modules/` live
on `/mnt/workspace`, so they inflate the L2 checkpoint tar (`_create_archive`)
and the `sch acp` fs-sync mirror. Accepted here and left as an explicit
follow-up change; the lockfile-first guidance keeps those directories disposable
(delete them and `uv sync`/`npm ci` recreates them).
