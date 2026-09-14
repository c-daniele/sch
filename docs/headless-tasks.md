# Headless tasks and git-native parallel sessions

Detached, unattended work: submitting tasks, choosing between interactive and headless modes, per-task models, and running N agents on N branches with `--branch` and `sch fetch`. Normative behavior: [`docs/specs/access-surfaces/headless-task-execution.md`](specs/access-surfaces/headless-task-execution.md) and [`docs/specs/sync-and-git/git-native-workflow.md`](specs/sync-and-git/git-native-workflow.md).

## Headless tasks

Per [`docs/specs/access-surfaces/headless-task-execution.md`](specs/access-surfaces/headless-task-execution.md), `sch` supports **detached headless tasks**: a task submitted from the laptop continues to run after the laptop disconnects, with the microVM kept alive via `HealthyBusy` until the task completes. The outcome is persisted and readable with `sch status` even when the microVM is stopped.

### Which command should I use?

| Mode | Purpose | Durability and completion guarantee |
| --- | --- | --- |
| `sch run` / `sch shell` / `sch web` / `sch attach` / `sch acp` | Interactive, human-in-the-loop work | Live sync and periodic checkpoints are best-effort. Detaching or closing the client does not mean that the harness turn completed or that a final checkpoint exists. An open web tab keeps the microVM awake. |
| `sch task` | Reliable unattended/asynchronous work | Returns a task ID, keeps the runtime `HealthyBusy`, records heartbeat and terminal outcome, enforces a timeout, and forces a terminal checkpoint. |
| `sch status` | Observe an async task | Reads status from S3 without waking the microVM. `checkpoint: confirmed` means both harness outcome and durable artifacts are available. |
| A later `sch run --sync <dir>` | Review and recover work | Restores the remote workspace if needed, performs a three-way reconciliation, and downloads non-conflicting remote-only file changes to the PC before opening the TUI. |

Use interactive modes for exploration and supervised edits. Use `sch task` whenever the laptop may disconnect and you need a trustworthy outcome. An interactive detach is not a substitute for task submission.

#### Interactive busy keep-alive

Interactive turns (run TUI after a detach, `sch attach`/`sch web`, prompts
injected from Telegram) get a best-effort keep-alive of their own: the seeded
OpenCode plugin writes per-session activity markers (`SCH_ACTIVITY_DIR`,
default `/tmp/sch-activity`) from any opencode process, and a shim watcher
advertises `HealthyBusy` on `/ping` while at least one session shows fresh
`busy` activity — so the 15-min idle timer no longer reaps the microVM
mid-turn. Guard rails: markers silent for more than `SCH_BUSY_STALE_SECONDS`
(default 600) release the hold with a `busy-stale` Telegram notification (the
writing process likely died mid-turn); a continuous busy episode longer than
`SCH_BUSY_MAX_HOLD_SECONDS` (default 7200) releases with a `busy-cap`
notification and is not re-held until the episode ends. This is an
availability aid, not a durability guarantee — the `sch task` row above is
still the only mode with heartbeat, terminal outcome and forced checkpoint.
Current watcher state is visible in the `info` action (`interactive_busy`).

### Per-task model override (`sch task --model <id>`)

`sch task <ws> --model <id> "<prompt>"` runs that single detached task with an
explicit model instead of the harness's default:

- **Per-invocation semantics**: the value applies only to that submission.
  Nothing is persisted in workspace metadata, the local index, or the central
  registry; the next `sch task <ws>` without the flag runs with the harness's
  default model again — except an opencode `--continue`, which keeps the
  resumed session's stored model (see below).
- **Harness-specific id format**: the value is opaque to `sch` and forwarded
  verbatim to the harness — `provider/model-id` for OpenCode
  (`opencode run --model <id>`), a Bedrock inference-profile ID for Claude
  (`claude -p --model <id>`, e.g.
  `eu.anthropic.claude-haiku-4-5-20251001-v1:0`) and for Pi, which takes it as
  a provider/model pair (`pi -p --provider amazon-bedrock --model <id>`; the
  provider side is added by the shim, never by the operator). Only the syntax is
  validated (`[A-Za-z0-9._:/-]+`, same rule as `sch run --model`); an id the
  harness does not recognize fails the task as a normal `failed` task with
  the harness's native error readable via `sch status`.
- **Combines with `--continue`**: the resumed session runs its new turns with
  the requested model (native harness support); `sch` does not compare it
  against the model of previous tasks. Without `--model`, an opencode
  `--continue` keeps the model and reasoning effort (e.g. `high`) selected
  in the TUI: the shim forwards the resumed session's stored model as
  `--model` and its stored effort as `--variant`. An explicit `--model`
  overrides the model and drops the stored effort. There is no `--variant`
  flag on `sch task` or `sch run`: an explicit effort cannot be requested,
  only preserved from the resumed session.
- **Interaction with `--`**: everything after `--` is prompt text, so the
  flag must precede `--` (`sch task ws -- --model x` submits the literal
  prompt `--model x`).
- **Observability**: the accepted model is echoed in the submit ack and
  recorded in the task-status object (initial, heartbeats, terminal record),
  so `sch status <ws>` shows a `model` line for tasks submitted with the
  flag. Absence of the field/line means "harness default model" — except on
  an opencode `--continue` without the flag, where it means "the resumed
  session's stored model" (forwarded per above but not recorded as a
  requested model).
- **Older runtime image**: an image predating this feature ignores the field
  and runs the task with the default model; the CLI detects the missing echo
  in the ack and prints a stderr warning while still printing the `task_id`.

### Recommended asynchronous workflow

```sh
# First use: bind the local folder and choose the durable backend.
./bin/sch task my-project --storage s3 --sync . \
  "implement the feature, run the tests, and fix failures"
#> <task-id>

# This is offline-first and does not wake a stopped microVM.
./bin/sch status my-project
#> state        : succeeded
#> checkpoint   : confirmed

# Restore history/worktree if the microVM was recreated, download remote
# changes to the bound local folder, then open the interactive TUI.
./bin/sch run my-project --sync .
```

If `state=succeeded` but `checkpoint=failed`, the harness command completed but its final files/history were not durably confirmed. The last periodic checkpoint remains the recovery point.

## Git-native sessions (`--branch` + `sch fetch`)

Two session modes exist, chosen per use case (add-git-native-workflow):

| | Interactive (`acp`/Zed, `attach`, `run/task --sync`) | Autonomous (`run`/`task --branch`) |
| --- | --- | --- |
| Code transport | **Mirror sync** (file-level, live, bidirectional) | **Git-native** (history via `git bundle` on the tunnel) |
| Live local view of remote files | yes | no (by design — use `sch fetch` or `sch shell` to peek) |
| Parallel sessions on the same local base | 1 by nature | N, structurally isolated |
| Delivery | already in the editor | local branch → review → merge |
| Conflicts | whole-file policies (`--conflict`) | standard `git merge`, hunk-by-hunk, at the operator's pace |

**Why**: running several autonomous sessions in parallel from the same local
root with the mirror sync leads to silent overwrites (each mirror has a
per-workspace baseline that ignores the other sessions, and last-writer-wins
is whole-file). Git-native mode removes the problem structurally: each
session gets its own remote clone and its own branch, and nothing writes to
your local worktree until you `sch fetch`.

**How it works**:

- `sch task <ws> --branch <name> "<prompt>"` (or `sch run <ws> --branch <name>`)
  creates a full `git bundle` from the local `HEAD`, transfers it over the
  existing tunnel file channel, and the remote shim clones it and checks out
  `<name>` **before the harness starts** (mechanical provisioning, never a
  prompt instruction). A dirty local worktree produces a stderr warning; the
  seed is from HEAD. `--branch` is mutually exclusive with
  `--sync`/`--bootstrap`/`--conflict`, and a workspace stays in one mode for
  its lifetime (cross-mode invocations die with an explanatory error).
- `sch fetch <ws> [--push] [--force]` collects the work: the shim snapshots
  the remote worktree (if dirty, a service commit
  `wip: session snapshot (<UTC>)` authored by `sch-session <ws>` is created
  mechanically — the agent is free to commit or not), produces an
  incremental `base..branch` bundle, the CLI downloads it and imports it
  with a **fast-forward-only** `git fetch` into `refs/heads/<branch>` — your
  current checkout is never touched. A diverged local branch fails cleanly
  with the choice of `--force` (overwrite) or manual merge. `--push`
  forwards the imported branch to `origin` using your local git
  configuration and credentials.
- **No git credential ever reaches the remote workspace** (the agent is a
  prompt-injection surface): the remote clone has no `origin`, needs no
  GitHub outbound connectivity, and all provider interaction happens from
  the laptop. The flow works unchanged with a network-isolated runtime.

```sh
# Parallel autonomous work from the same HEAD — no overwrites possible.
./bin/sch task ws-a --branch change/feature-a "implement change A per the spec"
./bin/sch task ws-b --branch change/feature-b "implement change B per the spec"
# ... later, collect both (offline-safe, idempotent):
./bin/sch fetch ws-a
./bin/sch fetch ws-b
git merge change/feature-a && git merge change/feature-b

# Iterate on one session: --continue reuses the seeded branch (no re-seed).
./bin/sch task ws-a --continue "address the review comments"
./bin/sch fetch ws-a

# Recovery after a crashed/interrupted task: the snapshot at fetch time
# captures the agent's commits AND the uncommitted worktree state (as a
# distinguishable service commit).
./bin/sch fetch ws-a
git log change/feature-a   # look for "wip: session snapshot" author sch-session

# Deliver upstream with LOCAL credentials only:
./bin/sch fetch ws-a --push
```

`sch status`/`sch list` show the mode and branch for seeded workspaces
(`mode: git-native`, `branch: <name>`; `[git-native: <name>]` in the list).
End-to-end verification: `bin/verify-git-native.sh <ws-prefix>` (live) and
`cli/tests/test_gitnative_e2e_offline.py` (offline chain against the real
shim handlers, bundle helper, and fetch command).
