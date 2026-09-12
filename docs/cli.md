# Using the `sch` CLI

Command reference by example, per-user provider API keys, session handoff, local workspace sync, and the Windows notes. Every capability of SCH is reachable from this CLI ([MANIFESTO](../MANIFESTO.md), surface hierarchy). Normative behavior: [`docs/specs/access-surfaces/`](specs/access-surfaces).

## Using `sch`

```sh
./bin/sch shell my-project        # open (or reopen) a shell in the workspace microVM (default harness: opencode for new workspaces)
./bin/sch shell my-project --harness opencode   # force OpenCode on a brand-new workspace
./bin/sch open my-project         # alias for `sch shell`
./bin/sch run my-project          # open the workspace and jump STRAIGHT into its harness TUI in the active worktree
./bin/sch run my-project --harness claude   # same, explicitly selecting Claude on a brand-new workspace
./bin/sch run my-project --harness opencode --model provider/model-id  # OpenCode model for this invocation only
./bin/sch run my-project --harness claude --model eu.anthropic.claude-haiku-4-5-20251001-v1:0  # Claude Bedrock inference profile
./bin/sch run my-project --harness pi       # same, explicitly selecting Pi on a brand-new workspace
./bin/sch run my-project --harness pi --model eu.anthropic.claude-haiku-4-5-20251001-v1:0  # Pi: sent as --provider amazon-bedrock --model <id>
./bin/sch list                    # known workspaces + harness + last local action
./bin/sch list --remote-check     # same, plus S3 writer-claim comparison reporting orphans/collisions
./bin/sch task my-project "build and test"   # submit a headless detached task (uses the workspace's persisted harness)
./bin/sch task my-project --continue "proceed with build and test"  # continue the brainstorm session of the persisted harness
./bin/sch handoff my-project       # push the latest local OpenCode session for this project
./bin/sch handoff my-project --session ses_abc --sanitize  # explicit session, redacted export
./bin/sch task my-project --model provider/model-id "build and test"  # run this one task with an explicit model (opencode id shown)
./bin/sch status my-project       # show task status (offline-first; reads S3) — includes `harness` field
./bin/sch status my-project --live # enrich with live microVM state if it is up
./bin/sch stop my-project         # L2 checkpoint (db + worktree/state + claude state if harness=claude, pi state if harness=pi -> S3) + StopRuntimeSession
./bin/sch reset-session my-project  # regenerate the local sessionId (confirms); exercises/tests L2 restore (harness preserved)
./bin/sch info                    # resolved region, runtime ARN, default harness for new workspaces
```

### Provider API keys (per-user: Anthropic, OpenCode Zen/Go, OpenRouter, Kilo, Bedrock)

By default, harnesses in the microVM use **only** Bedrock through the execution
role. Each user can add their own external providers without deployment-operator
involvement or a redeploy: keys live in `~/.sch/env`, and `sch` forwards them in
every invocation payload.

```sh
mkdir -p ~/.sch
touch ~/.sch/env && chmod 600 ~/.sch/env
cat > ~/.sch/env <<'EOF'
ANTHROPIC_API_KEY=sk-ant-...      # api.anthropic.com
OPENCODE_API_KEY=...              # one key covers Zen and Go
OPENROUTER_API_KEY=...            # openrouter.ai
KILO_API_KEY=...                  # Kilo Gateway (api.kilo.ai)
BEDROCK_API_KEY=...               # cross-account Bedrock (see below)
EOF
```

All keys are optional and independent. `sch` forwards only those explicit
names; unrelated AWS, Telegram, or other variables never leave the client. The
parser tolerates malformed lines and missing or empty files. If the file is
readable by group or others, `sch` prints a `chmod 600 ~/.sch/env` warning,
withholds every key, and continues in Bedrock-only mode.

The file must be exactly `~/.sch/env` — not `~/.config/sch/env` (that directory
holds `sch`'s own config: workspaces, caches). A file at the wrong path is
silently ignored; `sch info` shows the configured key names and the expected
path:

```sh
sch info
# provider keys: KILO_API_KEY, OPENCODE_API_KEY, OPENROUTER_API_KEY (in /home/me/.sch/env)
```

Inside the microVM, the shim atomically stages the complete set in
`/run/sch/provider-keys.env` (mode `0600`, under an image-created `0700`
directory). The path is outside `/mnt/workspace` and `/home/sch`, so checkpoint
archives, DB backups, state mirrors, and S3 objects cannot include key values.
The
`SCH_ANTHROPIC_API_KEY` / `SCH_OPENCODE_API_KEY` / `SCH_OPENROUTER_API_KEY` /
`SCH_KILO_API_KEY` / `SCH_BEDROCK_API_KEY` names remain inert;
`image/scripts/harness-wrapper.sh` is the
only component that maps them to provider variables:

| key                  | `opencode`                                    | `claude`                     | `pi`                          |
| -------------------- | --------------------------------------------- | ---------------------------- | ----------------------------- |
| `ANTHROPIC_API_KEY`  | adds `anthropic`                              | **switches Bedrock to API**  | adds `anthropic`              |
| `OPENCODE_API_KEY`   | adds `opencode` (Zen) and `opencode-go`       | not wired                    | adds `opencode` (Zen)         |
| `OPENROUTER_API_KEY` | adds `openrouter`                             | not wired                    | adds `openrouter`             |
| `KILO_API_KEY`       | adds `kilo`                                   | not wired                    | adds `kilo`                   |
| `BEDROCK_API_KEY`    | re-auths Bedrock (bearer token)               | re-auths Bedrock (unless an Anthropic key is also staged: the Anthropic key wins) | re-auths Bedrock (bearer token) |

For OpenCode and Pi, keys add selectable providers without changing the seeded
Bedrock default. Claude instead treats `ANTHROPIC_API_KEY` as an explicit switch
to the Anthropic API. Remove it and restart the session to return to Bedrock.

#### Cross-account Bedrock inference

`BEDROCK_API_KEY` is an Amazon Bedrock API key (bearer token) issued by a
different account than the one hosting the runtime: deploy SCH in account X,
issue the key in account Y, and inference (model access, quotas, billing)
happens in Y while runtime, checkpoints and state stay in X. The token is
consumed only by Bedrock clients — checkpoint S3, DynamoDB, AgentCore and the
aws-mcp MCP server keep riding the execution role in X. Setup in account Y:

1. Enable model access for the target models **in the runtime's region**
   (including Anthropic's one-time use-case form).
2. Create the key: short-term (Bedrock console → API keys, up to 12 h, AWS's
   recommendation) or long-term
   (`aws iam create-service-specific-credential --user-name bedrock-api-user
   --service-name bedrock.amazonaws.com --credential-age-days 90`).
3. Give the key's principal `bedrock:InvokeModel`,
   `bedrock:InvokeModelWithResponseStream`, `bedrock:CallWithBearerToken` (+
   `bedrock-mantle:CallWithBearerToken` for OpenAI-family models) and
   `bedrock:GetInferenceProfile` on the models/profiles it may use.
4. Put the key in `~/.sch/env` as `BEDROCK_API_KEY=...`.

The system-defined cross-region inference profile IDs (`eu.*`, `us.*`,
`global.*`) are the same in every account, so the seeded model defaults keep
working; application inference profiles are account-scoped — use the ones from
account Y. Account Y must have model access in the runtime's region; a
different region is not covered by the dispatcher (redirect individual
harnesses by hand if you must — see the
[provider API keys spec](specs/providers-models/provider-api-keys.md),
R12).

On Pi, the two gateways arrive through a generated provider catalog
(`models.json`, built from models.dev at image build and reconciled per
session): a key merges its provider, removing the key withdraws it on the next
bootstrap. Only `$ENV_VAR` references are written — key values never touch the
file.

At every bootstrap, Claude reconciles managed `CLAUDE_CODE_USE_BEDROCK` settings
and its SCH-owned 20-character approval suffix in both directions. Operator
settings and approvals are preserved.

Each invocation replaces the complete staged set. Removing a key takes effect
for processes started afterward; already-running harnesses retain their startup
environment until restarted. A raw `agentcore exec --it` session that has not
gone through `sch` remains Bedrock-only.

Run `./bin/verify-user-provider-keys.sh` for the offline payload and secrecy
checks. Add `<workspace> live` (and optionally `--harness claude`) after deploying
the updated image for microVM and checkpoint verification.

This is a breaking migration from deploy-time keys. `deploy.sh` now ignores
leftover provider variables, and every user must configure `~/.sch/env`.

### OpenCode session handoff

`sch handoff <workspace>` transfers a local OpenCode conversation into an
OpenCode-bound remote workspace, where `sch task <workspace> --continue` or an
interactive TUI resumes it. A local `opencode` executable is required only for
this command. By default SCH selects the most recently updated session whose
directory is the current project; `--session <id>` overrides selection and
`--sanitize` opts into OpenCode's export redaction.

Fresh workspaces are bound permanently to OpenCode; Claude-bound workspaces are
rejected. Repeating a handoff is last-write-wins and warns that remote work may
have been overwritten. Conversation transfer is independent of repository
alignment: use `--branch` or `--sync` on the normal run/task commands when the
remote also needs local files. Success is reported only after the imported DB
has reached durable storage. Older runtime images must be rebuilt before they
support the `session-import` action. A disposable live check is available as
`SESSION_ID=ses_... ./bin/verify-handoff.sh <workspace>`.

To stop every locally indexed session and remove local SCH workspace, sync,
mirror, and temporary state, preview and then apply the maintenance cleanup:

```sh
./bin/sch-cleanup
./bin/sch-cleanup --yes
```

The cleanup preserves local runtime/bucket caches, repositories, and S3
checkpoints.

### Local workspace sync

Start a remote harness from the project currently checked out on the laptop:

```sh
./bin/sch run my-project --sync .
./bin/sch shell my-project                 # reuses the saved local binding
./bin/sch task my-project "build and test" # preflights sync, then runs detached
```

`--sync <dir>` resolves and saves a machine-local binding for the workspace;
the path is never written to the shared workspace registry. `--no-sync`
temporarily bypasses that binding. If both replicas already contain divergent
files without a baseline, SCH stops safely. Choose a one-off bootstrap policy
with `--bootstrap abort|local-wins|remote-wins|union`; for later true conflicts
use `--conflict abort|local-wins|remote-wins|keep-both`. These policies are not
persisted. A detached `sch task` closes sync after the preflight barrier; its
remote output is reconciled at the next synchronized invocation.

`--sync` needs Node.js, the installed `tunnel/` dependencies, and AWS tunnel
credentials. Progress, exclusions, and warnings go to stderr, leaving task
stdout scriptable. `Ctrl+]` detaches the remote TUI, closes the local sync
lease, and leaves subsequent remote changes for the next reconciliation.

Sync limitations:

- A workspace accepts one fs-sync client at a time. A second client is rejected
  while the first lease is held.
- `.git` is excluded from `sch run`/`sch shell` sync, including the initial
  bootstrap, so source-only sync does not transfer a potentially large object
  database. Set `SCH_MIRROR_INCLUDE_GIT_SNAPSHOT=1` to opt into a one-time
  snapshot into a semantically empty destination; it is never live-synced.
- Ignored paths, symlinks, special files, and files above the configured size
  limit are excluded and reported before the workload begins.
- By default, SCH excludes `.git`, dependency and virtual-environment folders
  (`node_modules`, `.venv`, `venv`), Python/test/tool caches, common build and
  coverage output (`dist`, `build`, `coverage`, `htmlcov`),
  `.codebase-memory`, and `dummy_data`. Source files, configuration, and
  `.env` files remain included. Set `SCH_MIRROR_IGNORE` to a comma-separated
  list to replace these defaults for one invocation or in the shell profile.
- Initial scans and hashing can be expensive for large repositories. Sync is
  atomic per file, not atomic for the repository as a whole.
- A crash before a barrier leaves the previous baseline intact. The next
  invocation recalculates from both manifests rather than assuming partial work
  converged.

- 1 workspace = 1 `runtimeSessionId` (>= 33 chars, generated by SCH and persisted in
`~/.config/sch/workspaces/<ws>` as JSON `{runtimeSessionId, harness, storage, sessionEpoch}`).
- **Per-workspace harness** (see "Multi-harness" below): each workspace is
  bound to exactly one of `opencode`, `claude` or `pi`, persisted alongside the
  `runtimeSessionId`. New workspaces default to `opencode` (override with
  `SCH_DEFAULT_HARNESS`); legacy workspaces (index written before the
  multi-harness change) are reconciled to `opencode` on first access.
  Switching harness on an existing workspace is
  rejected — use a new workspace name or the documented reset path.
- Inside the shell: use the active worktree shown by the runtime `info` diagnostics
  (`/home/sch/workspace/repo` for `s3`, `/mnt/workspace/repo` for `session`) and run
  `opencode`, `claude` or `pi` for
  the TUI of the workspace's harness. All three binary names go through the same
  dispatcher (`image/scripts/harness-wrapper.sh`) that applies the ENV
  bridge and gates on the per-harness readiness marker. **The binding is
  enforced**: only the bound harness's binary is available — invoking any
  other one fails fast with an explicit error (exit 127) pointing at the
  bound harness.
- `sch run <ws>` skips the manual step: the login shell consumes a run-once
  marker armed by the shim (`action=prepare-run`, local disk, 300s TTL),
  creates the active worktree if missing, `cd`s into it and `exec`s the
  harness. Quitting the harness closes the remote session and returns to
  your terminal (no lingering shell); `Ctrl+]` still detaches while keeping
  the TUI running.
- `sch run <ws> --model <id>` selects a model only for that invocation. Use a
  harness-specific identifier: `provider/model-id` for OpenCode, or a Bedrock
  inference-profile ID for Claude and for Pi (which receives it as the
  `--provider amazon-bedrock --model <id>` pair). The override is not persisted
  in workspace metadata and does not affect later runs.
- Detach: `Ctrl+]` (TUI keeps running); reconnect to the same logical shell
  with the command printed by the CLI, `sch shell <ws> --shell-id <id>`.
  `sch run`/`sch shell` generate and pass an explicit `shellId` when one is
  not supplied.
- Clone a repo on first start: set `SCH_REPO_URL` (and `SCH_REPO_TOKEN`)
  among the session env vars — HTTPS only; robust credential handling is out of scope.
- When done: `sch stop <ws>` (or let the idle timeout expire).
- `sch reset-session <ws>` is mainly a testing/ops tool: it does not touch any
  checkpoint data, only the workspace→sessionId mapping (and preserves the
  persisted harness and storage backend). Use it to force a new microVM with
  an empty active root for an existing workspace (see "L2 Durability" below)
  instead of waiting for the 14-day expiry or a real version bump.

## Windows support

`bin/sch.ps1` and `bin/sch` are both thin shims (no application logic) that
locate a Python interpreter and delegate the entire invocation to the shared,
cross-platform implementation in `cli/sch/` (Python 3.8+ stdlib-only —
`argparse`/`json`/`subprocess`, no `pip install`). Because both platforms run
the *same* Python code, every command has **full parity across macOS, Linux,
and Windows** — including `acp`, `zed-config`, and `attach`, which used to be
bash-only (see [`docs/specs/access-surfaces/cli-cross-platform.md`](specs/access-surfaces/cli-cross-platform.md)).

Prerequisites on a Windows laptop are the Windows-native counterparts of the
macOS/Linux prerequisites:

- **Python 3.8+**: the shim tries `python3`, then `python`, then the `py -3`
  launcher, in that order, and errors out explicitly if none is found.
- **PowerShell 5.1+** (built into Windows 10/11) or PowerShell 7+ (`pwsh`) —
  only to run the one-line `bin/sch.ps1` shim itself.
- **AWS CLI v2** for Windows (https://aws.amazon.com/cli/)
- **Node.js + npm** for `@aws/agentcore`, and for `acp`/`attach`'s
  `tunnel/*.js` bridge: run `npm install -g @aws/agentcore` and, for
  `acp`/`attach`, `cd tunnel && npm install` once. Open a new PowerShell
  session afterwards so the npm global-bin directory is on `PATH`.
- **Docker Desktop** for Windows (required only for local builds,
  `./infra/deploy.sh -l`; the default CodeBuild build needs no Docker. The
  `linux/arm64` local image build via buildx + QEMU is already supported as in
  the macOS/Linux path)

Usage (PowerShell) — identical command surface to the POSIX examples below:

```powershell
.\bin\sch.ps1 shell my-project                       # default harness (opencode for new workspaces)
.\bin\sch.ps1 shell my-project --harness opencode    # force OpenCode on a brand-new workspace
.\bin\sch.ps1 run my-project --harness claude
.\bin\sch.ps1 list
.\bin\sch.ps1 task my-project "build and test"
.\bin\sch.ps1 task my-project --continue "proceed with build and test"
.\bin\sch.ps1 status my-project                      # offline-first (reads S3)
.\bin\sch.ps1 status my-project --live
.\bin\sch.ps1 stop my-project
.\bin\sch.ps1 reset-session my-project
.\bin\sch.ps1 info
.\bin\sch.ps1 acp my-project                         # now available on Windows too
.\bin\sch.ps1 zed-config my-project
.\bin\sch.ps1 attach my-project
```

Both shims read/write the **same** workspace index
(`~/.config/sch/workspaces`, honoring `XDG_CONFIG_HOME`, default
`$env:USERPROFILE\.config\sch\workspaces` on Windows), so an operator can
alternate between macOS/Linux and Windows on the same project without
re-persisting sessions or harness choices. The `image/*` AgentCore runtime
code path (Dockerfile, shim, harness dispatcher, profiles) is **unchanged** —
it ships `linux/arm64` regardless of the operator's OS.

Caveats:

- Interactive shell handoff (`shell`/`open`/`run`) uses `os.execvp` on
  POSIX (process replacement, identical to a real `exec`) and a foreground
  `subprocess` + `sys.exit($LASTEXITCODE)`-equivalent on Windows, since
  Windows has no working `exec()` semantics for this case — same pattern
  already used by the previous PowerShell port.
- The `bin/verify-*.sh` test-lab scripts remain bash (they invoke `sch` as
  an external command, so they pass through the new shim unmodified); run
  them under **WSL** on Windows — they are not part of the regular operator
  flow.

### Why `sch` delegates to the `aws` CLI instead of using boto3

`sch` is not a Python library but a process orchestrator: its job is to
spawn external tools (`agentcore exec --it` is interactive and not
replicable via an SDK; the ACP/attach tunnels are `node` processes), and
delegating to the `aws` CLI is consistent with that design. This choice
keeps the CLI at zero pip dependencies, and inherits the user's entire
credential configuration (profiles, SSO, MFA, `credential_process`,
assume-role caching) without reimplementing it. Every invocation is an
argv list with no shell (`subprocess.run([...])`, never `shell=True`): no
injection surface. The layer is isolated in `cli/sch/runtime.py`, so a
future migration to boto3 would stay confined to that one module.
