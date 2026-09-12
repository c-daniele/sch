# Git-native workflow

> Domain: [sync-and-git](../README.md) · Status: Implemented · Source: rationalized from openspec change `add-git-native-workflow` (2026-08-28)

## Purpose

Git-native mode is the alternative to the mirror sync for autonomous
sessions: launched with `--branch <name>` on `sch run` and `sch task`, it
isolates each session structurally on a dedicated git branch in a dedicated
remote workspace. Code travels as git history (bundles over the tunnel's
file channel) and the work is delivered back as a local branch with
`sch fetch`. Parallel autonomous sessions on the same code base can then
never silently overwrite each other; conflicts, if any, surface at
`git merge`, content-aware and under the operator's control.

The rule of thumb: **interactive = mirror sync** (see
[local workspace sync](local-workspace-sync.md)); **autonomous = git-native**.

## Scope

In scope:
- `--branch <name>` activation on `run` and `task`, and mutual exclusion with the mirror sync options.
- Seed of the remote workspace via a full git bundle over the tunnel (`git-seed` shim action).
- Delivery of the session's work via `sch fetch` (`git-snapshot` shim action, incremental bundle, local fast-forward import, optional push).
- Persistence of the mode (branch, base sha, local repo) in the workspace state and reuse without re-seeding.
- Recovery of work after an abnormal session end via checkpoint restore.
- Mode visibility in `sch status` and `sch list`.

Out of scope:
- The mirror sync engine, its baselines and conflict policies (unchanged; see [local workspace sync](local-workspace-sync.md)).
- Git credentials or any push from the remote workspace, except through the explicit opt-in below (otherwise excluded by design).
- Automatic PR opening, partial/shallow bundles, auto-fetch on clean exit (possible future extensions).
- Per-root locking of the mirror sync (deferred hardening; the mirror is confined to interactive 1:1 use).

## Requirements

### Activating git-native mode

- **R1.** `sch run` and `sch task` SHALL accept `--branch <name>`, which activates git-native mode for the invocation: no mirror sync is started and the session works on a dedicated git branch in the remote workspace.
- **R2.** The branch name MUST be validated as a valid git ref (`git check-ref-format --branch`) before any local or remote mutation; an invalid name SHALL die with a validation error without contacting the runtime.
- **R3.** `--branch` MUST be rejected at parse time (die with a usage message) when combined with `--sync`, `--bootstrap` or `--conflict`.

### Seeding the remote workspace

- **R4.** On the first git-native invocation on a workspace, the system SHALL create a full, self-contained bundle from the `HEAD` of the current local repository (`git bundle create <file> HEAD`), transfer it to the remote workspace via the tunnel file channel, and invoke the `git-seed` shim action, which clones from the bundle and creates the requested branch before the harness starts. The invocation MUST run from inside a local git repository; `--branch` requires a local git installation.
- **R5.** If the local worktree is dirty at seed time, the system MUST print a warning to stderr that the uncommitted changes are not included and proceed by seeding from `HEAD`.
- **R6.** The remote branch creation MUST happen as harness provisioning (the `git-seed` action), never as an instruction in the agent's prompt.
- **R7.** The harness or task SHALL start only after the seed completes successfully.
- **R8.** After the seed, the CLI SHALL verify that the remote-reported base sha matches the local HEAD and refuse to record an inconsistent binding otherwise. If the runtime image does not support the `git-seed` action, the CLI MUST fail before starting the harness with a message pointing to an image update — there is no warn-and-continue degraded path for the seed.
- **R9.** The seed bundle size SHALL be printed to stderr before the transfer.

### Mode persistence and reuse

- **R10.** Upon seed completion, the system SHALL persist `{mode: "git-native", branch, baseSha, localRepo}` in the workspace's local state (`gitNative` field of the workspace index). `localRepo` records the local repository root the seed came from.
- **R11.** Subsequent invocations on the same workspace (including `--continue`) MUST reuse the existing branch and base without repeating the seed; no bundle is transferred.
- **R12.** A workspace in git-native mode MUST reject invocations with `--sync`, and a workspace with a saved mirror sync binding MUST reject `--branch`, each with an error stating the current mode and how to proceed. A seeded workspace MUST reject a `--branch` naming a different branch (use a new workspace name).

### Delivering the work with `sch fetch`

- **R13.** The system SHALL expose `sch fetch <workspace> [--push] [--force]`, which: invokes the `git-snapshot` shim action; downloads the incremental `base..branch` bundle produced by the remote via the tunnel's file channel; and imports it into the local repository recorded at seed time with `git fetch <bundle> <branch>:<branch>` in fast-forward-only mode.
- **R14.** `fetch` MUST NOT modify the operator's current checkout — it only creates or updates the branch ref `refs/heads/<branch>`. If the target branch is currently checked out in the local repository, the command MUST fail with a message telling the operator to switch to another branch (or use a worktree).
- **R15.** If the local branch has diverged, the command MUST fail without modifying anything, proposing `--force` (overwrite of the local ref) or a manual merge. `--force` overwrites the local ref via a forced refspec.
- **R16.** With `--push`, after a successful import, the system SHALL run `git push origin <branch>` using exclusively the operator's local git configuration and credentials.
- **R17.** `fetch` MUST be idempotent: repeated invocations with no new remote work produce neither new commits nor errors. The shim's `no-work` status (clean worktree, no commits beyond the base) and the CLI's local-ref short-circuit (local ref already at the remote head) SHALL both exit cleanly.
- **R18.** `fetch` SHALL require the workspace to be in git-native mode; a mirror-synced workspace is rejected with a message pointing out that mirror-synced workspaces deliver through the sync itself.
- **R19.** `fetch` is a delivery path, not a provisioning one: it SHALL NOT rotate the session or change the runtime version; it may, however, be the invocation that revives a stopped microVM (checkpoint restore), in which case it warms up and verifies storage exactly like `run`/`task`.
- **R20.** On a dirty remote worktree, `fetch` SHALL report on stderr that a service snapshot commit was created. On success, `fetch` SHALL mark the workspace status as `fetched`.

### No git credentials in the remote workspace

- **R21.** By default the git-native flow MUST NOT transfer, inject or make readable any git credential (PAT, SSH keys, ephemeral tokens) in the remote workspace, at any stage (seed, session, snapshot, delivery). All operations toward `origin` MUST happen on the local side — unless the operator opts in per [Opt-in GitHub access](#opt-in-github-access-task-26) below.
- **R22.** The seed and delivery flow MUST work without outbound connectivity from the runtime to the git provider; without the opt-in, the remote clone SHALL have no `origin` remote configured.

### Opt-in GitHub access (TASK-26)

- **R29.** The operator MAY stage a GitHub token by configuring `GITHUB_TOKEN` in `~/.sch/env`. Use a fine-grained PAT with the minimum scopes the run needs (typically `Contents: read/write` and, for PR workflows, `Pull requests: read/write`; `Actions: read` for monitoring runs), a short expiry, and no SSO-bypass beyond what the run requires. The token rides the same invocation transport, ephemeral staging (`/run/sch`, mode `0600`, total replacement) and secrecy rules as the provider keys (see [user-provider keys](../providers-models/user-provider-keys.md) and [provider API keys](../providers-models/provider-api-keys.md)). A session without a staged token behaves exactly as R21/R22 describe.
- **R30.** At seed time the CLI SHALL resolve the local `origin` remote and, only when it is a usable GitHub remote, send it as the credential-free https `originUrl` of the `git-seed` payload (SSH forms rewritten, embedded userinfo stripped, anything else dropped). The shim SHALL sanitize the received URL again server-side and record it in the workspace state (`originUrl` in `git-native.json`, bound at seed like the branch). The URL is remote metadata, not a secret.
- **R31.** On every invocation the shim SHALL reconcile the repo's git access, but only on workspaces with a recorded git-native branch: with a staged, URL-safe token it SHALL write the tmpfs credential store (`/run/sch/git-credentials`, one github.com-scoped line, `0600`, atomic, total replacement) and point the repo-local `credential.helper` at it; without a staged token it SHALL remove the store file, its own helper entry and any SCH-managed `origin`.
- **R32.** The shim SHALL add the recorded `origin` only when the repo has no `origin` at all, and SHALL mark origins it adds (`remote.origin.schManaged=true`). It MUST NOT modify or remove an operator-owned `origin`, nor overwrite an operator-owned `credential.helper`.
- **R33.** The token value MUST NOT appear in `.git/config` (which rides the repo checkpoint), in any other checkpointed state, in logs, or in diagnostics — only the credential-free helper pointer and the `schManaged` marker may persist. The token MUST NOT be passed in any process argv.
- **R34.** The `gh` CLI (pinned in the runtime image) authenticates exclusively through the staged `GH_TOKEN`/`GITHUB_TOKEN` environment; the harness MUST NOT persist `gh` auth state (`gh auth login` stores into the checkpointed config and is forbidden).

### Recovery after an abnormal session end

- **R23.** Work done in a git-native session MUST be recoverable with `sch fetch` even if the session terminates abnormally, provided the workspace is restorable from checkpoint: the remote git-native state (branch, base sha) rides the state checkpoint, and the mechanical snapshot at fetch time captures both the agent's commits and any uncommitted state of the remote worktree.

### Shim actions (runtime image)

- **R24.** The shim SHALL expose a `git-seed` action that, given the seed bundle in the staging area and a branch name: validates the branch format, refuses a double seed, verifies the bundle (`git bundle verify`), fetches the bundle's history into the canonical repo path, creates and checks out the work branch, and responds with `status` and `baseSha`. Failures (missing/corrupt bundle, already-seeded repo, path with existing history) MUST return an explanatory error status with no partial mutations visible to the harness.
- **R25.** The bundle staging path (`state/bundles` under the active backend's workspace root: `/home/sch/workspace/state/bundles` for `s3`, `/mnt/workspace/state/bundles` for `session`) MUST reside outside the repo worktree, and the seed bundle SHALL be deleted from staging after a successful seed.
- **R26.** The shim SHALL expose a `git-snapshot` action that: if the repo worktree is dirty, creates a service commit with author `sch-session <workspace> <sch-session@local>` and message `wip: session snapshot (<ISO-8601 UTC timestamp>)`; produces the incremental bundle `<baseSha>..<branch>` in staging; and responds with `status`, `branch`, `headSha`, `snapshotCommitted`, `baseSha` and the bundle reference. The action MUST be idempotent: a clean worktree never creates a commit; with no commits beyond the base and a clean worktree it MUST return the dedicated `no-work` status instead of producing an empty, unimportable bundle. A snapshot on an unseeded workspace MUST return an error.
- **R27.** Bundle transfer over the tunnel SHALL reuse the existing fs-sync file channel (hash-verified, ack'd, chunked) with the per-file size policy effectively disabled for bundles — bundles MUST never be excluded by size.

### Mode visibility

- **R28.** `sch status` and `sch list` SHALL expose, for git-native workspaces, the mode and the working branch (e.g. `[git-native: feat/x]`).

## Behavior

```console
# Parallel autonomous sessions from the same HEAD
$ sch task ws-a --branch verify/a "implement change A"
$ sch task ws-b --branch verify/b "implement change B"
$ sch fetch ws-a && sch fetch ws-b   # local branches verify/a, verify/b
$ git merge verify/a && git merge verify/b   # conflicts, if any, are here

# Iterate on a seeded workspace
$ sch task ws-a --continue "fix the tests"    # no re-seed, same branch

# Collect and forward
$ sch fetch ws-a --push            # imports locally, then pushes from the laptop

# Opt-in remote push (TASK-26): GITHUB_TOKEN in ~/.sch/env (chmod 600) BEFORE
# the first --branch run; the harness can then `git push origin <branch>`
# and drive `gh pr create`, `gh run watch`, ... itself. Removing the token
# withdraws the remote credential on the next invocation.
```

- Forbidden combinations fail before any mutation: `--branch` with `--sync`/`--bootstrap`/`--conflict`; `--sync` on a git-native workspace (remedy: `sch fetch` or a new workspace name); `--branch` on a mirror-bound workspace; a different `--branch` on a seeded workspace.
- Dirty local worktree at seed → warning that uncommitted changes are not included; the seed proceeds from HEAD.
- Old runtime image → explicit failure before the harness starts ("rebuild/update the runtime image"); no ambiguous degraded session.
- Divergent local branch at fetch → clean failure proposing `--force` or a manual merge; branch checked out locally → failure asking to switch branches.
- Recovery: task crashes with a dirty worktree → stop (checkpoint) → `reset-session` → `sch fetch` restores the agent's commits plus a `wip: session snapshot` service commit.
- Credential audit: without the opt-in, the remote workspace has no git credentials in its environment, no `~/.git-credentials`, no credential helper, and its clone has no `origin`. With the opt-in, the audit shows exactly: `GH_TOKEN`/`GITHUB_TOKEN` in the harness env, one tmpfs `store` helper line in the repo-local git config pointing at `/run/sch/git-credentials`, and at most one SCH-managed `origin` — no secret in any persisted file.

## Invariants

- **I1.** A git-native session never runs the mirror sync, and a mirror-synced workspace never enters git-native mode.
- **I2.** The harness or task never starts before the seed completes and the remote base sha matches the local HEAD.
- **I3.** The seed bundle always contains exactly the local HEAD's history; the delivery bundle's prerequisite (base sha) is by construction part of the local history, so every delivery is locally importable.
- **I4.** `sch fetch` never modifies the current checkout and never moves a branch ref backwards (fast-forward only, unless `--force`).
- **I5.** Without the opt-in, no git credential ever exists in the remote workspace at any stage; the remote clone has no `origin`. With the opt-in, the only secret is the staged token on tmpfs (I9–I11).
- **I6.** Service snapshot commits are always distinguishable by author `sch-session <workspace>` and message `wip: session snapshot (...)`.
- **I7.** `git-snapshot` is idempotent: a clean worktree never produces a commit; no-work never produces a bundle.
- **I8.** The branch name is validated (`git check-ref-format --branch`) both client-side and shim-side, before any mutation.
- **I9.** The GitHub token exists only on tmpfs (`/run/sch`): never in `.git/config`, never in a checkpoint, never in a log, never in an argv.
- **I10.** The recorded `originUrl` is bound at seed like the branch; a session without a staged token has no SCH-managed `origin` and no SCH helper entry.
- **I11.** Operator-owned git config (`origin`, `credential.helper`) is never modified or removed by the reconciliation.

## Cross-references

- [Local workspace sync](local-workspace-sync.md) — the mirror sync, unchanged and confined to interactive use.
- [MANIFESTO](../../../MANIFESTO.md) — core loop and principles (invariants live in the harness, never in the prompt).
- Code: `cli/sch/gitnative.py` (mode resolution, validation, seed flow, `local_origin_url`), `cli/sch/bundlexfer.py` (transfer plumbing, staging roots), `cli/sch/commands/fetch.py` (delivery flow), `cli/sch/commands/run.py` and `cli/sch/commands/task.py` (`--branch`), `cli/sch/workspace.py` (`gitNative` state), `image/app/main.py` (`git-seed`/`git-snapshot` actions, `_reconcile_github_access`, tmpfs credential store), `image/scripts/harness-wrapper.sh` (`GH_TOKEN`/`GITHUB_TOKEN` mapping), `tunnel/bundle.js` (bundle transfer helper).
- Tests: `cli/tests/test_gitnative.py`, `cli/tests/test_gitnative_e2e_offline.py`, `image/app/test_github_access.py`; live verification: `bin/verify-git-native.sh`, `bin/verify-github-access.sh`.
