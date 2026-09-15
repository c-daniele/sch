# Deploying and operating SCH

How `sch deploy` works, what changes the runtime version, the deploy-time feature switches, the operational limits of the platform, what a running system costs, and how to tear everything down again. Normative behavior: [`docs/specs/platform/runtime-provisioning.md`](specs/platform/runtime-provisioning.md).

## Deploy

One command, three phases, no preparation:

```sh
sch deploy                        # or ./infra/deploy.sh from the support repo
```

It deploys the **bootstrap stack** (ECR repository + the ARM CodeBuild project
that builds the image + the bucket carrying the build source), builds and
pushes the `linux/arm64` image on that project, then deploys the **runtime
stack** (AgentCore Runtime + session storage on `/mnt/workspace` + execution
role). A first run on an empty account needs no flags and no environment
variables; re-runs are safe, and what they do to the runtime is spelled out
under [What creates a new runtime
version](#what-creates-a-new-runtime-version). For the full first-install
sequence around it, see [From scratch on a new AWS
account](getting-started.md#from-scratch-on-a-new-aws-account).

Common options (identical for `sch deploy` and `./infra/deploy.sh`):

```sh
sch deploy -r us-west-2           # different region
sch deploy -e stg                 # different environment (default: dev)
sch deploy -v v7                  # tag the build v7 (a label: every rebuild reaches the runtime anyway)
sch deploy -s                     # only the CFN stacks, skipping the image build: runtime version unchanged
sch deploy -l                     # build locally with Docker instead of CodeBuild
sch deploy -h                     # every option and env-var switch
ENABLE_WORKSPACE_REGISTRY=true sch deploy -s          # optional IAM registry
ENABLE_SESSION_IMAGE_REBUILD=true sch deploy -s       # let SESSIONS rebuild the image
```

### What creates a new runtime version

AgentCore resolves the image reference once, when it creates a runtime version.
A reference made of a tag alone (`sch-dev:v1`) is never looked at again, so a
rebuild that keeps the tag would push an image no runtime version ever runs.
The deploy therefore pins the runtime to the **digest** of the image it just
pushed (`sch-dev@sha256:...`; the tag stays in the `ApplicationVersion` stack
parameter and in `SCH_IMAGE_VERSION` inside the image), and the runtime
version changes exactly when:

- the image is rebuilt — any deploy without `-s`. Container builds are not
  reproducible, so rebuilding unchanged sources is a new digest too: a deploy
  meant to change only a switch belongs with `-s`;
- `-s -v <tag>` points the runtime at another existing tag;
- the runtime environment changes (Telegram on/off, in-session image rebuild
  on/off, `SCH_PI_DEFAULT_MODEL`).

It does not change on `-s` without `-v` (the deployed tag and digest are
reused as they are) nor on capability tuning, workspace registry or watchdog
changes: those deploy in place. `-v` names a build; it is no longer what makes
it live.

A new runtime version **resets the session storage of every workspace** and
the platform retires the microVMs of the superseded version. Since L2
durability this is not data loss: each workspace is repopulated from its S3
checkpoint on first reopen ([L2 Durability](workspaces.md#l2-durability-s3-checkpoint));
work never checkpointed on the old session is the one thing not recoverable,
so `sch stop` the workspaces you care about before a deploy that rebuilds the
image. Existing workspaces notice the new version on their next provisioning
command and rotate their session (`runtime version changed: ...` on stderr).

The deploy ends by printing the runtime version and the container URI it runs,
straight from AgentCore. Inside a session, `printenv SCH_IMAGE_DIGEST` names
the build (the tag in `SCH_IMAGE_VERSION` no longer does once it has been
rebuilt); compare it with `aws ecr describe-images --repository-name
<project>-<env> --image-ids imageTag=<tag> --query
'imageDetails[0].imageDigest'`.

### Deploy-time switches (optional features)

Every optional feature is an environment variable read by this same deploy —
there is no separate installer, no second stack to add later. Two consequences
worth internalizing before you use them:

- **The switches are re-read on every deploy.** An omitted variable is deployed
  as *off*: the runtime environment variable disappears, the IAM grant narrows
  back, the webhook is deregistered. A deploy that silently disabled a feature
  looks exactly like a successful one, so keep the values somewhere persistent
  rather than in shell history.
- **`infra/setenv.sh` is that persistent place.** `deploy.sh` sources it itself
  at startup (no `source` needed on your side), it is git-ignored, and its
  assignments **win over the surrounding environment** — a value exported on the
  command line does not override a line in that file. It lives in the support
  repo checkout:

```sh
~/.local/share/sch/repo/infra/setenv.sh   # pipx/uv install (managed checkout)
$XDG_DATA_HOME/sch/repo/infra/setenv.sh   # when XDG_DATA_HOME is set
%LOCALAPPDATA%\sch\repo\infra\setenv.sh   # Windows
<your-clone>/infra/setenv.sh              # git checkout (also $SCH_REPO_ROOT)
```

`sch setup` prints the checkout it resolved (`support repo found at '<path>'`),
which is the reliable way to locate the file on your machine.

Use `infra/setenv.sh.example` as the template and keep its `export` form: the
deploy sources the file in its own shell, so a plain assignment would work too,
but `export` also hands the values to anything else you run from a shell that
sourced it.

| Feature | Variables | Guide |
| --- | --- | --- |
| Telegram notifications | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | [docs/telegram-setup.md](telegram-setup.md) |
| Telegram remote interaction | the two above + `ENABLE_TELEGRAM_INTERACTION` | [Telegram interaction](telegram.md#telegram-interaction) |
| Runtime IAM posture | `RUNTIME_CAPABILITIES`, `RUNTIME_BEDROCK_ACCESS`, `RUNTIME_BEDROCK_MODEL_ALLOWLIST`, `RUNTIME_AWS_API_READ`, `RUNTIME_DATA_BUCKET_ARN`, `RUNTIME_EXTRA_POLICY_JSON` | [docs/runtime-capability-tuning.md](runtime-capability-tuning.md) |
| IAM workspace registry | `ENABLE_WORKSPACE_REGISTRY` | [IAM Workspace Registry](workspaces.md#iam-workspace-registry) |
| In-session image rebuild | `ENABLE_SESSION_IMAGE_REBUILD` | [Image Rebuild from a Session](image-rebuild.md#image-rebuild-from-a-session-codebuild) |
| External task watchdog | `ENABLE_TASK_WATCHDOG` (on by default), `TASK_WATCHDOG_STALE_SECONDS`, `TASK_WATCHDOG_NOTIFY_AFTER_SECONDS` | [Headless tasks](headless-tasks.md#headless-tasks) |

`sch deploy -h` prints the authoritative list, defaults included.

### Why CodeBuild by default

AgentCore Runtime accepts **linux/arm64 images only**. The bootstrap stack's
CodeBuild project (`<project>-<env>-image-build`) builds that natively: the
deploy zips `image/`, uploads it to the build-sources bucket, starts the build
with the buildspec defined in the template, and verifies the tag exists in ECR
before the runtime stack points at it.

`-l` builds locally instead (`docker build --platform linux/arm64`): native on
Apple Silicon, but on an x86_64 host it needs QEMU binfmt handlers and every
`RUN` step (dnf, npm, uv, aws-cli) runs emulated. `-l` is also the only path
that can use the local `corporate-ca` image base — `image/certs/*.pem` is
never uploaded to CodeBuild.

The deploy-time build project is deliberately separate from the **in-session**
rebuild project (`ENABLE_SESSION_IMAGE_REBUILD`, see [Image Rebuild from a
Session](image-rebuild.md#image-rebuild-from-a-session-codebuild)): deploying never widens the
runtime execution role.

## Lifecycle and Costs

Defaults: `idleRuntimeSessionTimeout=900` (15 min), `maxLifetime=28800` (8h),
parameterized in the stack (`IdleRuntimeSessionTimeoutSeconds`, `MaxLifetimeSeconds`).

**Empirical measurement (Open Question 2)**: a WebSocket shell **open but idle
keeps the microVM Active** beyond the idle timeout (verified: uptime 1076s with
idle timeout 900s, no invocations or input for the entire interval) ⇒ a
forgotten open shell **continues to accrue compute costs**. Operational rule:
**`sch stop <ws>` when done**, or close/detach the terminal and
let the idle timeout expire (the timer only restarts without connected shells).
Useful metrics: `AWS/Bedrock-AgentCore` → `ActiveSessionCount`,
`CPUUsed-vCPUHours`, `MemoryUsed-GBHours` (dimensions `Resource`=runtime ARN).

The default values remain adequate: 900s tolerates short pauses without
an open shell; the cost of idle with an open shell is a conscious user
choice, mitigated by `sch stop`. No changes to the template defaults.

### Memory is billed on peak (TASK-1)

AgentCore bills memory on the peak consumed up to each second (128 MB
minimum) from boot until microVM shutdown: one transient spike prices the
whole session, and releasing memory afterwards does not lower the bill.
Attribution and local evidence: [Memory peak attribution](history/memory-peak-attribution.md).
Three levers, in order of effect:

1. **Never spike — caps.** Every harness launch path (interactive
   dispatcher, headless task, serve supervisor) runs under
   `SCH_NODE_HEAP_MB` (default `1792`, Node `--max-old-space-size`) and
   `SCH_BUILD_JOBS` (default `2`, fanned out to `MAKEFLAGS`,
   `CMAKE_BUILD_PARALLEL_LEVEL`, `CARGO_BUILD_JOBS`, restated for repo
   runners). Deploy-time defaults are the `NodeHeapMb`/`BuildJobs` stack
   parameters (`SCH_NODE_HEAP_MB=`/`SCH_BUILD_JOBS=` env on deploy);
   per-workspace overrides are plain env and need no redeploy.
   **Escape hatch for known-big builds:** `SCH_NODE_HEAP_MB=4096
   SCH_BUILD_JOBS=1` (or `SCH_NODE_HEAP_MB=0` to uncap); raising the cap
   re-prices the billed peak, so raise only as far as the build needs.
   A task killed by the caps fails loudly naming both knobs (spec
   [runtime-image](specs/platform/runtime-image.md) R47).
2. **Shrink the checkpoint amplifier.** The repo tarball and fingerprint
   exclude regenerable dirs (`node_modules`, `.venv`, build outputs —
   spec [workspace-checkpointing](specs/workspace-lifecycle/workspace-checkpointing.md)
   R21); the env is rebuilt best-effort after an L2 restore
   (`SCH_REBUILD_ENV_ON_RESTORE=0` disables). Steady-state floor reference
   (opencode harness, September 2026): `opencode` ~750 MB RSS, shim
   ~150 MB, `mcp-proxy` ~100 MB, aws-docs MCP ~65 MB — ~1.4 GB before any
   workload. Keep `context7` disabled unless needed; audit `serve` + MCP
   children with `bin/mem-trace.sh` when the floor moves.
3. **Short sessions after spikes — recycle.** `sch stop <ws>` runs the
   forced synchronous checkpoint first and then stops the microVM
   (compute meter stops); reopening starts a fresh `runtimeSessionId` on
   the same `checkpoints/<ws>/` identity, so no work is lost and the next
   session bills from a clean peak. Rule of thumb: after a heavy build or
   test run, `sch stop` and reopen instead of idling — the idle tail after
   a 7 GB peak bills 7 GB per second until the microVM dies.

### Cost alarm (proposed — not yet in the template)

Roadmap item; target file `infra/agent_runtime.yaml` (conditional alarm
resource) or `infra/deploy.sh` (opt-in flag). Proposed definition, to be
validated against one billed week before pinning the threshold:

- Metric: `MemoryUsed-GBHours`, namespace `AWS/Bedrock-AgentCore`,
  dimension `Resource` = runtime ARN, statistic Sum, period 1 day.
- Threshold: 2x the trailing-7-day daily average (anomaly-shaped, so
  workspace growth does not page); evaluate over 2 consecutive days.
- Draft (tune the threshold first):
  `aws cloudwatch put-metric-alarm --alarm-name sch-<env>-memory-gb-hours
  --namespace AWS/Bedrock-AgentCore --metric-name MemoryUsed-GBHours
  --dimensions Name=Resource,Value=<runtime-arn> --statistic Sum --period
  86400 --evaluation-periods 2 --threshold <2x-baseline> --comparison-operator
  GreaterThanThreshold`.
- Companion: second-granularity cause analysis with
  `bin/mem-trace.sh -o trace.csv -d <workload_s>` plus the vended
  1-second `agent.runtime.*.used` logs, per the attribution checklist.

## Session Storage (Preview) Limits — Observed and Documented

| Limit | Value | Operational Notes |
| --- | --- | --- |
| Size per session | 1 GB | budget for repo + harness state (OpenCode state, Claude JSONL transcripts or Pi JSONL sessions); large `node_modules` can exceed it |
| Inactivity expiry | 14 days | storage is reset; **mitigated by L2** (`sch-l2-durability-s3-checkpoint`) — no longer data loss, the workspace is restored from S3 on next `sch shell` (including the persisted harness) |
| Runtime version update | **resets ALL session storage** | every deploy that rebuilds the image is one, since the runtime is pinned to the image digest ([What creates a new runtime version](#what-creates-a-new-runtime-version)); **mitigated by L2**: workspaces are repopulated from their S3 checkpoint on first reopen after the bump; no manual backup needed (exception: the one migration deploy that introduces the mechanism itself, see `infra/deploy.sh`). The harness is restored from the manifest, so the right harness is seeded even after a reset. |
| Interactive shells per runtime | max 10, **per-runtime** (confirmed) | official docs (`bedrock-agentcore-limits.html`) confirm the "concurrent shell sessions per runtime: 10" scope is per-runtime, shared across all workspaces on the shared runtime. Applies ONLY to the interactive shell channel (`sch shell`/`sch open`/`sch run`); **not** to `sch attach`/`sch acp`, which use the distinct `InvokeAgentRuntimeWithWebSocketStream` operation — the Phase-3 multiplexer this row once anticipated is no longer needed (`sch-remote-ui-tunnel` D9, resolved by the pivot). |
| `InvokeAgentRuntimeWithWebSocketStream` (tunnel transport for `sch attach`/`sch acp`) | 64 KB/frame, 60 min/connection, 250 frames/s/connection | documented per-connection limits (`bedrock-agentcore-limits.html`); the bridge chunks under 64 KB (~60 KB frames), reconnects proactively before the 60 min cap (default T-55min) and resumes the same logical session without byte loss. The **250 frames/s** cap is the binding constraint for bulk transfer — exceeding it makes AgentCore close the connection (a 1006 reconnect storm that stalled `sch attach`'s TUI, found in live verification); the shim paces sends to ~200 frames/s and applies send-side flow control (`sch-remote-ui-tunnel` D3, image v21). The same flow-control window applies **client-side** since `sch-acp-editor-integration` (image v22): the ACP file-sync channel (`mode:"fs"`, one extra connection per `sch acp` session) pushes multi-MB mirror hydrations local→remote, which desynced resend offsets without it. Effective bulk throughput ≈ 9-12 MB/s per channel — the practical bound on first-mirror-hydration time. |
| `InvokeAgentRuntimeWithWebSocketStream` concurrency | **no dedicated ceiling** (200 TPS open rate) | unlike the 10-shell channel, this operation has no per-operation concurrency quota — governed only by the account-wide "Active session workloads" limit (2,500–5,000); the 200 TPS figure caps the *rate of opening* connections, far above a single `sch attach`'s ~18 one-time connections. This is why `sch attach`/`sch acp` never share or compete for the 10-shell budget (D9 resolved by the pivot; the local soft cap `SCH_TUNNEL_MAX_CHANNELS`, default 32, is a client-side safety net only). |
| POSIX `fcntl` locks | **not supported** (`ENOLCK`) | SQLite default VFS does not work on the mount; see architecture above. Applies to every harness: `opencode.db`, Claude's JSONL state and Pi's JSONL state all live on local disk + L2 mirror. |
| Restore visibility | asynchronous, 30–60s lag | files may appear in scattered order after resume |
| Permissions/ownership | `root:root`, not enforced | git requires `safe.directory` (set at system level in the image) |
| `agentcore exec --it` env | **does NOT inherit container ENV** | login shell is almost empty (only `HOME`/`PATH`/`SHELL`/`TERM`/`USER`); mitigated by `scripts/harness-wrapper.sh` + `/etc/profile.d/sch-env.sh` (v8+); covers `opencode`, `claude` and `pi` (the Pi env vars joined the exported set in v30); not reproducible with local `docker exec` |
| Mount attach "fresh" | **asynchronous even with hint="fresh"** | reproduced 5/5 on new sessions: files missing right after connection, stable after ~15-20s; mitigated by `FRESH_SETTLE_WAIT` + verify-and-retry (`seed_verified`, v10+); verify loop is harness-aware (canary: `opencode.json` for opencode, `~/.claude/.mcp.json` for claude, `~/.pi/agent/settings.json` for pi) |
| Wrapper `opencode` vs `RESUME_WAIT` | **timeout mismatch (90s < 180s)** | final root cause of "0 MCP tools": the wrapper gave up before the "resumed" mount-wait could complete; fix `SCH_OPENCODE_WAIT` default 90→220 (v11+). `SCH_HARNESS_WAIT` generalizes the knob for every harness (claude and pi default to 220s through it). |
| Headless task timeout | `SCH_TASK_TIMEOUT_S` default **25200s (7h)**, min 5s, max 27900s | strictly below AgentCore `MaxLifetime` 28800s (8h) so the shim owns the terminal state; enforced by the worker thread's `subprocess.run(timeout=...)`. Bounds Claude JSONL transcript growth for runaway prompts (residual session-storage budget factor — see debt note below). |
| One task per workspace | enforced in-process by the shim | a second submit returns `status: busy`; persisted `running` status is not trusted as a guard (orphan reconciliation rewrites it to `interrupted` on boot, preserving the orphan's `harness` field for observability) |
| Idle timeout interplay | 15 min idle timer pauses while `HealthyBusy` | during a headless task `/ping` is `HealthyBusy`, so the idle timer does not terminate the session; after the task ends the idle timer restarts |
| Claude/Pi JSONL transcript growth | cumulative with turn count | Claude Code's `~/.claude/projects/<encoded-cwd>/*.jsonl` and Pi's `~/.pi/agent/sessions/<cwd-encoded>/*.jsonl` grow with every turn; the 1 GB session-storage cap bounds the **local-disk mirror replica** (not cumulative on S3, which has 3-day minimum retention). A pathological 7-hour runaway transcript would outgrow the mount replica; mitigated by `SCH_TASK_TIMEOUT_S`. Registered as a residual limit (debt note below). |
| Bedrock env (no OAuth) | `CLAUDE_CODE_USE_BEDROCK=1` always-on for claude | Claude Code rides the execution role via IMDSv2, no OAuth token — sidesteps anthropics/claude-code#28827 (OAuth token-refresh-in-headless failure). `ANTHROPIC_DEFAULT_{FABLE,OPUS,SONNET,HAIKU}_MODEL` (+ one `ANTHROPIC_CUSTOM_MODEL_OPTION`) pinned to Bedrock inference profiles so every `/model` entry resolves through an inference profile. |
| Node 22 already in image | `node-22`/`npm-22` symlinked to `node`/`npm` | Claude Code installed via `npm install -g @anthropic-ai/claude-code@<pinned>`; Pi via `npm install -g --ignore-scripts @earendil-works/pi-coding-agent@<pinned>` (its `engines.node` floor is asserted against the image's Node at build time); no Node version bump needed. |

The interactive TUI path (`sch shell` → `opencode` with a real pty) is intentionally unchanged: permission prompts still require operator confirmation, and no global auto-approval environment is set. Auto-approval is added **only** to the `opencode run` argv constructed by the headless worker (design D6). On `pi` there is nothing to auto-approve in either mode — see "Pi harness" → "Security posture: no permission prompt at all".

Headless use amplifies two known debts already accepted:

- **IAM / cost surface**: `ReadOnlyAccess` plus unrestricted Bedrock invoke, now running unsupervised for up to 7 hours. A runaway prompt can accrue model and compute cost without a human watching. Mitigated by the application timeout; a dedicated IAM/cost hardening change is parked (see [`docs/specs/security/runtime-capability-tuning.md`](specs/security/runtime-capability-tuning.md)). For harness=claude this surface now also includes `--dangerously-skip-permissions` headless auto-approval — the parked change is expected to introduce `--allowedTools`/`--disallowedTools` allow-list hardening on the headless argv for both harnesses, and the "reset for harness switch" path (currently a new-workspace operation per multi-harness D8). Historical cross-link: `sch-multi-harness` change (git tag `pre-openspec-retirement`).
- **Runaway / destructive prompts**: auto-approval means a headless task will execute any tool the model requests inside the container. The container is isolated, but within it the agent has broad read access and can modify the workspace. Use headless tasks only with prompts you would trust to run unattended. On `harness=pi` the same exposure extends to the **interactive** TUI, which has no permission prompt at all (accepted limit, see the Pi harness section).

## Uninstall

Two commands, one per side. Run them in this order: `sch destroy` needs nothing
from the local install, but `sch uninstall` removes the support checkout that
carries the deploy tooling.

```sh
sch destroy      # every AWS resource of one project/environment
sch uninstall    # local state, support checkout and the client itself
```

### AWS side — `sch destroy`

```sh
sch destroy --dry-run                 # print the plan, delete nothing
sch destroy                           # asks you to type: 'DELETE sch-dev'
sch destroy --keep-checkpoints        # tear down compute, keep the L2 workspace data
sch destroy -r us-west-2 -p sch -e stg --yes   # non-default target, unattended
```

It prints the **account it resolved from STS**, the region and the
project/environment before touching anything — a teardown aimed at the wrong
account is the one mistake that cannot be undone. Then it deregisters the
Telegram webhook (reading the token from `infra/setenv.sh` or the environment),
deletes the runtime stack, empties the ECR repository, deletes the bootstrap
stack (and the legacy `-ecr` stack of pre-bootstrap deployments), then the
build-sources, CloudFormation-bootstrap and checkpoint buckets — purging every
object version, which a plain `s3 rm` cannot do on a versioned bucket. Missing
resources are reported as already absent, so an interrupted run is finished by
running the command again.

> **WARNING**: deleting the runtime stack triggers `DeleteAgentRuntime`, which
> **deletes all L1 session storage**. Workspace data survives only in the L2
> checkpoint bucket, and `sch destroy` deletes that too unless you pass
> `--keep-checkpoints`. Delete workspaces first with `sch delete` if you want
> their per-workspace cleanup to run while the bucket still exists.

What it deliberately leaves alone, because SCH never created it: IAM policies
you attached to the deploy principal, and Bedrock model access / Marketplace
agreements. It lists them at the end as a reminder.

### Local side — `sch uninstall`

```sh
sch uninstall --dry-run     # print what would be removed
sch uninstall               # asks you to type: 'UNINSTALL SCH'
sch uninstall --keep-keys   # keep ~/.sch/env (your provider API keys)
sch uninstall --keep-client # remove local state only, leave the sch command
```

It removes the local state (`~/.config/sch` — workspace index and caches), the
managed support checkout (`~/.local/share/sch/repo`), the per-user provider keys
(`~/.sch/env`), `$TMPDIR/sch-*` artifacts, and finally the client itself,
detecting whether pipx, uv or pip installed it. A checkout install has no
package to remove: delete the clone.

It **refuses to run while a runtime stack is still deployed** (`--force`
overrides) so you cannot delete the deploy tooling and leave AWS resources
running by accident.

To only stop sessions and clear local session state — keeping the install, the
caches and the checkpoints — use the narrower utility instead:

```sh
./bin/sch-cleanup           # dry run
./bin/sch-cleanup --yes     # stop sessions, kill tunnel helpers, clear workspace/sync state
```
