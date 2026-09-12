# Implementation findings and early decisions (historical)

Point-in-time notes from the proof-of-concept phase (July-August 2026): empirical findings about the AgentCore Runtime, outcomes of the open questions the first design registered, and the preflight decisions that fixed the target region and tooling. Kept for context; **not normative** -- current behavior is specified under [`docs/specs/`](../specs/README.md) and current limits under [Deploying and operating SCH](../deploy.md).

## Runtime Architecture (implementation findings)

The container exposes the `BedrockAgentCoreApp` shim on :8080 (`noop`/`info`/
`checkpoint`/`task`); the harness TUI (opencode, claude or pi, per the
workspace's
persisted harness) launches on-demand in the interactive shell through the
single `harness-wrapper.sh` dispatcher. Canonical paths: worktree
`/mnt/workspace/repo`, harness state `/mnt/workspace/state` (XDG for
opencode; `state/claude` L2 replica for claude's local-disk `~/.claude`;
`state/pi` L2 replica for pi's local-disk `~/.pi/agent`).
Five empirical findings refined the design:

1. **No POSIX `fcntl` locks on session storage** (`ENOLCK`; `flock` and SQLite
   VFS `unix-dotfile` work). `opencode.db` therefore lives on local disk
   (`OPENCODE_DB`) and is checkpointed to the mount: restore at boot,
   periodic backup (60s, SQLite backup API), synchronous checkpoint on
   `sch stop`. Maximum loss on non-graceful kill: ~60s of DB writes.
2. **Session storage restore is asynchronous** with respect to container boot
   (observed lag 30–60s, unordered across files). The shim waits for
   restored content (marker `state/.sch-initialized` + `fresh`/`resumed` hint
   sent by `sch` on first invocation) and the `opencode` wrapper waits for
   the readiness marker before launching the binary.
3. **Role credentials are exposed only via IMDSv2**: boto3 works, but the
   JS SDK credential chain used by OpenCode hangs. Bridge:
   `credential_process` in `~/.aws/config` (IMDSv2 → JSON with `Expiration`)
   + `AWS_PROFILE=default`. No static credentials on disk.
4. **The "fresh" path also races with mount attach** (v10, discovered while
   debugging "0 MCP tools after first launch, present after close+reopen").
   `hint="fresh"` used to skip all waiting (nothing to "restore"), assuming
   the mount was already ready — but the session-storage volume **attach**
   itself can be asynchronous with respect to shell readiness
   (`agentcore exec --it` can become interactive before main.py has finished).
   A fast user can read `opencode.json`/`repo/` before the real mount has
   attached: the writes from `init-workspace.sh` (which ran against whatever
   was mounted at that instant, e.g. a local placeholder) are then
   **shadowed** once the real mount attaches.
   Reproduced empirically consistently (5/5 attempts on new sessions,
   `cat opencode.json` immediately after connection →
   "No such file"; the file appears stably after ~15-20s). Fix:
   `FRESH_SETTLE_WAIT` (short wait even on "fresh" hint) + a verify-and-retry
   loop (`_verify_workspace_seeded`, field `seed_verified` in
   `/invocations info`) that re-runs `init-workspace.sh` if, right after
   success, repo/config are still absent — it does not declare the workspace
   ready (`READY_MARKER`, which gates the `opencode` wrapper)
   until verification passes or retries are exhausted.
5. **The final root cause of "0 MCP tools" was a timeout mismatch**
   (v11). `sch shell` sends hint `"resumed"` (not `"fresh"`) for any
   workspace name already known locally (e.g. a retry on the same name) —
   and if that session is actually empty, the mount classification step alone
   can legitimately take the full `RESUME_WAIT` (180s) before proceeding to
   seed. The `opencode` wrapper, however, only waited **90s** for the
   readiness marker before giving up and starting OpenCode anyway (with a
   warning on stderr that's easy to miss) against a not-yet-seeded workspace —
   the agent correctly reported "I don't have an aws-docs tool" and `/mcps`
   showed an empty list, because at that point it was true. Reproduced and
   confirmed the fix empirically: with hint "resumed" on an empty session,
   the wrapper now prints
   `sch: workspace initializing (waiting up to 220s)...` and after ~177s
   correctly finds both MCP servers connected. Fix: default of
   `SCH_OPENCODE_WAIT` in `scripts/harness-wrapper.sh` (renamed from
   `scripts/opencode-wrapper.sh` in sch-multi-harness) raised from `90` to
   `220` (above `RESUME_WAIT`, with margin for db-restore/init-workspace).
   `SCH_HARNESS_WAIT` generalizes the knob for every harness (the claude and pi
   branches read it with the same 220s default).

## Multi-harness Open Questions — Outcomes (debt note)

The `sch-multi-harness` change registered four Open Questions in its `design.md`
to be resolved empirically during/after implementation. Their status when these
notes were last updated (September 2026):

- **OQ-CLAUDE-VERSION**: which pinned `@anthropic-ai/claude-code` version
  honors BOTH `CLAUDE_CODE_USE_BEDROCK=1` AND `--dangerously-skip-permissions`
  simultaneously? The Dockerfile's `ARG CLAUDE_CODE_VERSION` default is a
  starting pick (design estimates "likely anything ≥ 2.1"); the exact floor
  must be confirmed empirically at build/runtime (task 1.4) and recorded in
  the build-arg comment. **Status: pending empirical verification on the
  runtime** (tasks 1.3 / 1.4 / 10.x are runtime-gated and not executable
  from the repo alone).
- **OQ-HEADLESS-BEDROCK**: does `claude -p --dangerously-skip-permissions
  --resume <id>` with `CLAUDE_CODE_USE_BEDROCK=1` actually skip all
  permission prompts without a TTY? Design assumes yes (the documented "Safe
  YOLO mode" semantics); documented fallback is `--permission-mode acceptEdits`
  + an explicit `--allowedTools` whitelist (which would add a second row to
  the per-harness flag table above). **Status: pending empirical
  verification on the runtime** (task 5.9).
- **OQ-MCP2**: does `claude -p` hang on a foreground TTY when both `aws-docs`
  and `aws-mcp` are enabled, the same way `opencode run` did? If not, the
  default `/dev/null` stdin redirect (D7, applied to both harnesses for
  parity) can be lifted for the claude path only. **Status: pending
  empirical verification on the runtime** (task 5.10).
- **OQ-JSONL-LIFETIME**: does Claude Code prune/gzip old JSONLs itself, or
  do transcripts grow unbounded on the local disk? Affects how aggressively
  the L2 mirror should delta-compress old turns. Registered as a residual
  limit in the table above (Claude JSONL transcript growth, mitigated by
  `SCH_TASK_TIMEOUT_S`). **Status: pending empirical observation** (spans
  Phase 1's deferred multi-hour OQ1/OQ4).

These four are the runtime-gated debt items from `sch-multi-harness`; the
repo-side implementation is complete and historical tasks are preserved at
git tag `pre-openspec-retirement` pending a live runtime pass.

**Not (fully) mitigated by L2** — see "L2 Durability" above, "Residual limits":
raw shells opened without `sch` (no workspace identity, no restore); fuzzy
(not atomically-consistent) backup of a hot worktree; up to one checkpoint
interval (~60s) of loss for anything not gracefully stopped via `sch stop`;
restore time on very large workspaces bounded by `SCH_OPENCODE_WAIT`.

## Open Questions Outcomes

1. **Session storage in `eu-west-1`?** Yes (Preview, 14 regions; verified). Parametric region, fallback to `us-west-2` without template changes.
2. **Open WS shell ⇒ Active session for billing purposes?** **Yes** (measured): the microVM stays Active beyond the idle timeout as long as the shell is connected. Mitigation: explicit `sch stop` when done.
3. **Quota/size sufficient?** 1 GB/session; ok for small-medium repos, watch out for `node_modules`. Empirical observation with a real repo deferred to real-world usage.
4. **CLI vs SDK for `sch`?** npm CLI `@aws/agentcore` (≥ 0.24) for the data plane (`exec --it`), AWS CLI v2 for the control plane (`stop-runtime-session`, `invoke-agent-runtime`). Bash wrapper, no boto3.

## Preflight Decisions

### Target Region: `eu-west-1` (verified 2026-07-12)

AgentCore Runtime's session storage (Preview) is available in `eu-west-1` (Europe/Ireland).
Source: AWS announcement "Amazon Bedrock AgentCore Runtime now supports managed session storage"
(2026-03-25) — public Preview in 14 regions, including Europe (Frankfurt, Ireland, London,
Paris, Stockholm). Empirical verification: `aws bedrock-agentcore-control create-agent-runtime`
in `eu-west-1` exposes `filesystemConfigurations[].sessionStorage`.

No need for the `us-west-2` fallback: the default target region is **`eu-west-1`**
(parameterized in `deploy.sh`, so the fallback remains possible without template changes).

Known session storage (Preview) limits, from the documentation:

- max **1 GB** per session;
- retention **14 days** of inactivity, then storage is reset;
- **runtime version update ⇒ reset** of all session storage;
- max **10 interactive shells** per runtime.

### CLI `agentcore`: npm package `@aws/agentcore` (verified v0.24.0)

The old Python starter toolkit (`bedrock-agentcore-starter-toolkit`, PyPI) is **deprecated**
and does not have the `exec` command. The official CLI is `@aws/agentcore` (npm):

```sh
npm install -g @aws/agentcore
```

Verified capabilities (v0.24.0), sufficient for current needs (Open Question 4 → resolved):

- `agentcore exec --it --runtime <name|arn> --session-id <id> [--shell-id <id>] [--region <r>]`
  → interactive PTY shell, explicit session-ids, reconnection to existing shell;
- session stop: the npm CLI is not needed, use AWS CLI v2
  (`aws bedrock-agentcore stop-runtime-session --agent-runtime-arn ... --runtime-session-id ...`).

**Decision**: the `sch` wrapper is a **bash script** that composes `agentcore exec` (interactive
data plane) and `aws bedrock-agentcore` / `bedrock-agentcore-control` (control plane).
No boto3/Python SDK.

### Bedrock Model Access in `eu-west-1` (verified 2026-07-12)

Model access active: `bedrock-runtime converse` invocation succeeded with the
cross-region profile `eu.anthropic.claude-haiku-4-5-20251001-v1:0` without API key (credential chain).

Inference profiles chosen by default (all `ACTIVE` in `eu-west-1`, cross-region EU routing):

| Role | Inference Profile ID |
| --- | --- |
| Main model | `eu.anthropic.claude-sonnet-4-6` |
| Small/fast model | `eu.anthropic.claude-haiku-4-5-20251001-v1:0` |

ARNs for the execution role (account `<ACCOUNT_ID>`):

- `arn:aws:bedrock:eu-west-1:<ACCOUNT_ID>:inference-profile/eu.anthropic.claude-sonnet-4-6`
- `arn:aws:bedrock:eu-west-1:<ACCOUNT_ID>:inference-profile/eu.anthropic.claude-haiku-4-5-20251001-v1:0`

IAM note: with cross-region inference profiles, permissions are **also** needed on the
underlying foundation models in the profile's destination regions; the template uses
`arn:aws:bedrock:*::foundation-model/anthropic.claude-*` alongside the profile ARNs
(document AWS pattern for `eu.*` profiles).
