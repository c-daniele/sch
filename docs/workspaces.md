# Workspaces: registry, storage backends, durability and deletion

Where a workspace's identity lives, which storage backend holds its files, how the S3 checkpoint layer makes it survive anything, and how it is deleted for good. Normative behavior: [`docs/specs/workspace-lifecycle/`](specs/workspace-lifecycle) and [`docs/specs/security/`](specs/security).

## IAM Workspace Registry

### Where workspace session IDs are stored

SCH, not AWS, generates each `runtimeSessionId` in the form
`sch-<workspace>-<uuid4>` and passes it to AgentCore on every invocation.
AgentCore does not assign a separate session ID that SCH must discover.

By default, `SCH_WORKSPACE_REGISTRY_URL` is unset and the workspace-to-session
mapping is stored only on the client in
`~/.config/sch/workspaces/<workspace>` (or
`$XDG_CONFIG_HOME/sch/workspaces/<workspace>`). The JSON file is the source of
truth for the `runtimeSessionId`, harness, storage backend, and session epoch.
This local mode has two important consequences:

- mappings are not shared with another machine or OS user;
- deleting the local index loses the mappings, even though AgentCore session
  storage or S3 checkpoints may still exist.

The optional IAM workspace registry moves the authoritative mapping to its
DynamoDB-backed API. The local files then become an owner-tagged cache rather
than the source of truth. A DynamoDB workspace-registry table exists only when
the stack is deployed with `ENABLE_WORKSPACE_REGISTRY=true`.

The optional registry makes logical workspace names owner-scoped. Enable it by
deploying with `ENABLE_WORKSPACE_REGISTRY=true`, then set the CloudFormation
`WorkspaceRegistryUrl` output as `SCH_WORKSPACE_REGISTRY_URL` on each client:

```sh
export SCH_WORKSPACE_REGISTRY_URL="$(aws cloudformation describe-stacks \
  --stack-name sch-dev-runtime --region eu-west-1 \
  --query "Stacks[0].Outputs[?OutputKey=='WorkspaceRegistryUrl'].OutputValue" --output text)"
./bin/sch shell my-workspace
```

The endpoint uses API Gateway `AWS_IAM` authorization. Grant each caller
`execute-api:Invoke` on the CloudFormation `WorkspaceRegistryInvokeArn` output,
in addition to their existing direct AgentCore and checkpoint-bucket permissions. The CLI signs
requests through the normal AWS CLI credential chain, so profiles, SSO, MFA,
`credential_process`, and assume-role configurations continue to work without
installing Python dependencies.

- With the endpoint unset, SCH retains the legacy local workspace-index mode.
- With the endpoint set, registry resolution is authoritative for every
  workspace command. Local files are only an owner-identity-tagged cache and
  advisory status; registry failures never reuse cached session IDs.
- The authenticated API Gateway caller ARN is hashed into the namespace key.
  Different callers can create the same logical name without sharing session
  IDs or `checkpoints/<workspaceIdentity>/` prefixes.
- This is intentional logical isolation and discovery UX, not a data-plane
  security boundary. A principal that independently has AgentCore permissions
  and learns another session ID can still invoke it directly.
- Existing local workspaces are not imported automatically. Enable registry
  mode to create a new owner-scoped record and checkpoint namespace; retain
  legacy mode to access legacy session mappings and checkpoint prefixes. No
  import command is included in this change.

### Rollback / teardown

```sh
aws cloudformation delete-stack --stack-name sch-dev-runtime --region eu-west-1
# empty the ECR repository, then:
aws cloudformation delete-stack --stack-name sch-dev-bootstrap --region eu-west-1
```

> **WARNING**: `DeleteAgentRuntime` (triggered by deleting the runtime stack)
> **deletes ALL session storage** associated with the runtime, with no recovery
> for that L1 copy. The **L2 checkpoint bucket has `DeletionPolicy: Retain`**
> and is NOT deleted by this teardown (see "L2 Durability" below) — as long as
> a workspace was checkpointed at least once, its data survives the runtime
> stack's deletion in S3. Delete the checkpoint bucket separately (and only)
> once you are certain none of its workspaces are needed anymore.

For a full reset that also removes the retained L2 bucket, all ECR images and
every other resource of the deployment, use the guarded command (see
[Uninstall](deploy.md#uninstall)):

```sh
sch destroy
```

It requires typing `DELETE sch-dev` in full (or `--yes` in disposable
automation) and
leaves no SCH session, checkpoint, image or bucket behind.

## Workspace storage backends

Storage is selected once, when a workspace is created, and is immutable until an explicit migration command is introduced:

```sh
./bin/sch run my-project --storage s3 --sync .       # recommended default
./bin/sch run managed-project --storage session --sync .
```

Later commands reuse the persisted choice and do not need `--storage`.

| Backend | Active remote worktree | Persistence model | Operational notes |
| --- | --- | --- | --- |
| `s3` (default for new workspaces) | `/home/sch/workspace/repo` | Periodic and terminal generation checkpoints to S3; automatic restore before readiness | Avoids managed-session write backpressure and the 1 GB mount limit on the hot path. Cold resumes download and unpack the latest published generation. |
| `session` | `/mnt/workspace/repo` | AgentCore managed session storage, plus the existing S3 safety checkpoint | Managed by AWS, but currently subject to the 1 GB limit, asynchronous mount/restore behavior, and transient `ENOSPC` under write bursts. |

Existing workspace records created before storage selection are reconciled to `session`, preserving their current paths and data. A conflicting later flag fails safely:

```text
workspace 'my-project' is bound to storage='s3'; storage migration is not implemented
```

For `s3`, each checkpoint uploads immutable objects under `checkpoint-generations/` as `candidate`, promotes all required objects to `active=true`, and publishes `checkpoints/<workspace>/manifest.json` last. An interrupted upload therefore leaves the previous complete generation authoritative; only unpublished candidates expire automatically. Published objects remain available for retained historical manifest versions. OpenCode history (`opencode.db`), Claude JSONL transcripts, configuration, Git metadata, uncommitted changes, and untracked files are restored before the harness becomes ready. Private model chain-of-thought that the harness itself does not record cannot be recovered.

Manifest and task-status writes are fenced by a monotonic per-workspace
`sessionEpoch`, a writer claim, and conditional S3 writes. After session
rotation, an older microVM can no longer overwrite the newer workspace state.
Malformed/unreadable manifests and partial restores fail closed: the harness is
not marked ready and checkpointing does not start against an incomplete root.

```sh
# Brainstorm interactively, then hand off to a headless build/test run
./bin/sch shell my-project
# ... brainstorm in the TUI, then exit and stop ...
./bin/sch stop my-project

# Submit the headless task. Returns immediately with a task_id; laptop can go offline.
./bin/sch task my-project --continue "proceed with build and test"
#> a1b2c3...

# Check status any time later — reads S3, does NOT wake the microVM
./bin/sch status my-project
#> state        : running
#> task_id      : a1b2c3...
#> heartbeat    : 2026-07-14T10:00:00Z

# Once finished (succeeded/failed/timed-out/interrupted):
./bin/sch status my-project
#> state        : succeeded
#> exit_code    : 0
#> finished     : 2026-07-14T10:05:00Z
```

Key behaviors:

- `--continue` resumes the **latest session of the workspace's persisted
  harness** (OpenCode `opencode.db` row → `--session <id>`; Claude JSONL
  transcript → `--resume <id>`; Pi per-cwd JSONL session file →
  `--session <path>`) — the brainstorm→build handoff works for
  all three harnesses automatically (see "Multi-harness" → "Per-harness
  `--continue` handoff" above).
- One task per workspace at a time; a second submit while one is `running` returns `status: busy` with the current `task_id`.
- A **warning** is emitted if a TUI is still considered active on the same workspace (advisory flag set by `sch shell` / cleared by `sch stop`); the task still proceeds — the flag is best-effort.
- Auto-approval of permissions is scoped **only** to the headless argv of the workspace's harness (`--auto` for opencode, `--dangerously-skip-permissions` for claude; `pi` needs no flag — it has no permission prompt at all, in either mode); the interactive TUI path is unchanged and still prompts per-permission on the two harnesses that have prompts.
- Every terminal state (`succeeded`, `failed`, `timed-out`, `interrupted`) forces an L2 checkpoint before the shim returns to `Healthy`, so uncheckpointed work is not lost. For `harness=claude` this includes the Claude state replica (`state/claude`) and for `harness=pi` the Pi state replica (`state/pi`); each harness's artifact is skipped entirely on the other two.
- `sch status` is **offline-first**: it reads the sibling `task-status.json` object on S3 using the operator's local AWS credentials. The persisted status includes the `harness` field (which harness ran the task). `--live` optionally merges live fields via `action: info` if the microVM happens to be up.
- An **external task watchdog** (Lambda, every 2 minutes, `ENABLE_TASK_WATCHDOG` on by default) is the only observer that survives the death of a microVM: a `running` record whose heartbeat is older than `TASK_WATCHDOG_STALE_SECONDS` (600) is reconciled to `interrupted` and notified on Telegram. With Telegram configured it also guarantees the terminal notification of every task: the shim records the notification as `pending` and marks it `delivered` once sent; a record still `pending` after `TASK_WATCHDOG_NOTIFY_AFTER_SECONDS` (300) is re-sent from outside the microVM (at-least-once; visible in `sch status --json` as `notification_status`/`notified_by`).
- Verification: `./bin/verify-headless-tasks.sh <workspace> --harness <opencode|claude|pi>` covers submit → detached completion → offline status → checkpoint capture → concurrency rejection → orphan reconciliation → brainstorm handoff → application timeout, for a single harness; `./bin/verify-multi-harness.sh` runs the harness paths plus the multi-harness contract (mutual-exclusivity, upgrade-reconcile).

## L2 Durability (S3 Checkpoint)

The shim checkpoints the **entire** workspace (worktree, including uncommitted
changes; OpenCode state — db, snapshot, storage; user config) to a dedicated
S3 bucket. For backend `s3`, this is the primary durability layer for the
ephemeral `/home/sch/workspace`; for backend `session`, it remains the L2
safety layer restored when managed session storage is empty. It closes the
three hazards of the managed session storage recorded in the
[proof-of-concept findings](history/implementation-findings.md): inactivity
expiry (14 days), runtime version update (any deploy that rebuilds the image
resetting L1), and sessionId rotation for the same workspace. ("Phase 0"
below names that proof-of-concept baseline, before the S3 layer existed.)

**Mechanism**:

- **Periodic checkpoint** (default every 60s, `SCH_CHECKPOINT_INTERVAL`,
  min 10s): the shim's existing 60s DB-backup loop is extended into a full
  checkpoint loop. Per-artifact **change detection** (git HEAD + status hash
  + worktree mtime for the repo; mtime for OpenCode state; content hash for
  the DB backup) means **no S3 upload at all when the workspace is idle**
  (the DB backup file itself is the one exception — see "Residual limits"
  below).
- **Synchronous, forced checkpoint on `sch stop`**: the `checkpoint` action
  (already invoked by `sch stop` before `StopRuntimeSession` in Phase 0 for
  the DB) now also uploads worktree/state and always refreshes the manifest,
  even with nothing detected as changed. A checkpoint failure never blocks
  the stop — `sch stop` prints an explicit warning and proceeds regardless.
- **Automatic restore on an empty active root**: at boot, `s3` restores into
  `/home/sch/workspace`; `session` waits for mount classification and restores
  only when `/mnt/workspace` is empty. In both cases the shim checks
  `checkpoints/<workspace>/manifest.json` and extracts the referenced artifacts
  before falling back to the Phase-0 fresh seed. Readiness (`opencode`
  wrapper unblocked) is gated on a read-after-write verification of the
  restore, same pattern as the Phase-0 seed-verify loop.
- **Per-workspace key, not per-session**: backend `s3` publishes immutable
  artifacts under `s3://<bucket>/checkpoint-generations/<workspace>/<generation>/`
  and commits them by updating `checkpoints/<workspace>/manifest.json` last.
  Backend `session` retains the legacy fixed artifact keys beside the manifest.
  The workspace **name** (stable) keys the checkpoint, not the
  `runtimeSessionId` (which can rotate) — `sch` propagates the workspace name
  AND the persisted harness in its invocation payloads; the shim persists both
  in the active-root marker and in the L2 manifest, so a runtime version bump (which
  resets L1) restores the harness choice plus its state. The `claude.tar.gz`
  artifact is present only for `harness=claude` workspaces and `pi.tar.gz`
  only for `harness=pi` ones (each skipped entirely, no upload, on the other
  harnesses; `harness=opencode` has neither). Without a known workspace name (e.g. a
  raw shell opened without `sch`), L2 restore/checkpoint is skipped entirely
  — logged in `BOOT_STATE`, visible via `info`.
- **Retention/versioning**: the bucket (`infra/agent_runtime.yaml`) has S3
  versioning enabled — every checkpoint overwrite preserves the previous
  version — plus a lifecycle rule expiring non-current versions after
  `CheckpointRetentionDays` (CFN parameter, default 30) and aborting
  incomplete multipart uploads after 1 day. The execution role can
  `PutObject`/`GetObject`/`AbortMultipartUpload` **only** under
  `checkpoints/*` and `checkpoint-generations/*` on that one bucket, and explicitly **cannot**
  `DeleteObject` — retention is delegated entirely to the lifecycle policy,
  so neither a compromised agent nor a stray `aws-mcp` command can destroy
  checkpoint history. `DeletionPolicy: Retain` on the bucket itself means
  even deleting the whole runtime stack does not delete checkpoints.
- **Rollback without redeploy**: set `SCH_CHECKPOINT_INTERVAL=0` on the
  runtime stack's environment to degrade the loop back to the Phase-0
  behaviour (DB → mount only, no S3 upload at all) — no infrastructure
  change needed; the (retained) bucket stays harmlessly in place.

**Observability**: the `info` action's response includes a `checkpoint` block
(enabled, bucket, interval, resolved workspace identity, last result/
timestamp) and `boot.restore_l2` (attempted, result, verified) — see
`image/app/main.py`.

**Verification**: `bin/verify-l2.sh <workspace>` runs the full
checkpoint → simulated total L1 loss (`sch reset-session`) → restore cycle
and asserts worktree, OpenCode session history, and config were all
recovered from S3; `bin/verify-l2.sh <workspace> scenarios` checks the
upload/no-upload/manifest-on-stop behaviours directly (task 6.3) without
rotating the session.

**Residual limits** (not covered by L2, by design):

- **Raw shells without `sch`** (e.g. direct `agentcore exec`, never through
  `sch shell`/`sch stop`): no workspace identity is ever propagated, so
  neither checkpoint upload nor restore is attempted for that session —
  Phase-0 behaviour (fresh seed on empty storage) applies unchanged.
- **Fuzzy backup of a hot worktree**: the repo/state tar archives are built
  from a live, possibly-being-written-to worktree; a file that changes mid-
  archive can land in a slightly inconsistent (but always more recent than
  the previous checkpoint) snapshot. `tar` is retried once on "file changed
  as we read it"; the OpenCode DB is NOT subject to this — it is always a
  consistent SQLite backup-API snapshot, checkpointed independently.
- **Up to one checkpoint interval of loss** (default ~60s) for anything
  **not** gracefully stopped via `sch stop` (e.g. idle timeout expiry, a
  killed microVM): identical to the Phase-0 DB-only loss window, now also
  covering the worktree/state artifacts. An explicit `sch stop` has zero
  loss (synchronous, forced checkpoint).
- **Restore time on large workspaces**: download + extraction happens inside
  the `opencode` wrapper's readiness wait window (`SCH_OPENCODE_WAIT`,
  default 220s). Very large repos may need this raised; the wrapper degrades
  gracefully (starts OpenCode anyway with a warning) if the window is
  exceeded.

## Persistence Verification

```sh
./bin/verify-persistence.sh <workspace>          # full cycle: setup → stop → verify
./bin/verify-persistence.sh <workspace> setup    # only create known state
./bin/verify-persistence.sh <workspace> verify   # only verify (after idle timeout)
```

Asserts: worktree integrity (including uncommitted changes), OpenCode session
recovered with history, user config preserved. Pass/fail result
for each check. For the idle timeout test: run `setup`, wait > 900s
without touching the session, then `verify`. This exercises the **L1**
stop/resume cycle (same `runtimeSessionId` throughout). For the **L2**
checkpoint→total-loss→restore cycle (simulated storage expiry/version-update
via sessionId rotation), see `bin/verify-l2.sh` below.

## Deleting workspaces

`sch delete <workspace>` is irreversible and requires typing the exact name;
automation may use `--yes`. `sch delete --all` snapshots the local index (or
the authenticated owner's registry records), requires typing `DELETE ALL WORKSPACES`,
and reports each target independently. Failed targets retain a deletion marker
or registry record and can be retried with the same command.

Deletion quiesces the recorded runtime first, removes every version and delete
marker in the checkpoint, generation, and writer-claim scopes, then removes
SCH-owned local metadata. It never removes `--sync` sources, local repositories,
branches, or custom mirrors. Operators need runtime stop, `s3:ListBucketVersions`,
and `s3:DeleteObjectVersion` permissions. Unlike `stop` and `reset-session`,
deletion is destructive and does not checkpoint.

For `storage=session`, SCH purges its S3 safety checkpoints and removes its
mapping, but AgentCore managed session storage may remain until service
retention expires. `DeleteAgentRuntime` is a broader runtime-wide destructive
operation and is not a per-workspace deletion mechanism.

Deploy the registry Lambda/API and its delete permissions before distributing
the CLI. During rollback, keep the deletion endpoint available until records in
`deleting` have been retried or manually completed; removing only the CLI would
leave those records safely hidden from normal resolution but harder to resume.
