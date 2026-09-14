# Memory peak attribution (TASK-1.1)

Point-in-time evidence for the AgentCore peak-memory billing work (September
2026): which phase spikes, with what measured. Kept for context;
**not normative** — current behavior is specified under
[`docs/specs/`](../specs/README.md), operation under
[Deploying and operating SCH](../deploy.md).

## Billing model

AgentCore microVMs bill memory on peak-consumed-up-to-that-second per second
(128 MB minimum) from boot until termination, so one transient spike prices
the whole session. Releasing memory does not lower the billable value.
Optimize for **never spiking** and **short sessions after spikes**, not for
low average RSS. Operator measurement over ~10 days before this work: memory
~78% of sch cost, peaks around 7 GB on an ~8 GB box.

## Method

Second-granularity sampling with `bin/mem-trace.sh` (wrap mode around a
phase, or observe mode while driving phases from another shell), aligned on
wall-clock phase boundaries from `task-status.json` heartbeats and the shim
log. The billed peak itself is server-side metered — it lives in the
CloudWatch vended logs (`agent.runtime.*.used`, 1-second) and the
`MemoryUsed-GBHours` metric (1-minute), not in `/proc`. Local `/proc`
evidence identifies the phase and the process; the CloudWatch pair confirms
what was billed. Live re-run checklist is at the end.

## Local evidence (2026-09-14, this repo, harness=opencode)

Floor sample (5 s observe, idle-ish box running the harness):

- Box `MemTotal` ~8 GB. Idle-ish floor ~1.4–1.5 GB used.
- `opencode` ~725–830 MB RSS (VSZ ~76 GB is address reservation, not billed),
  shim `main.py` ~140–165 MB, `mcp-proxy-for-aws-cli` ~100 MB,
  `awslabs.aws-documentation-mcp-server` ~65 MB.
- Several zero-RSS zombie `[git]` processes — no billing impact.

Checkpoint amplifier, representative tree (1000 regenerable files across
`node_modules/` incl. nested, `.venv/`, `dist/`, ~33 MB random content,
plus a small `src/` and `.git`):

- `repo.tar.gz` without excludes: ~32.9 MB. With
  `REPO_CHECKPOINT_EXCLUDE_NAMES`: ~0.2 KB (sources + `.git` only).
- Repo fingerprint full walk: ~0.07 s over 1000 files; with excludes: ~0 s,
  and stable under churn inside excluded dirs (no re-upload triggered by
  `npm ci` output).
- Before the fix, every 60 s tick (`SCH_CHECKPOINT_INTERVAL`) walked the
  whole worktree file-by-file and tarred it with no excludes
  (`_fingerprint_repo`, `_create_archive` in `image/app/main.py`), so
  `node_modules/` + `.venv/` rode both. The v29 Dockerfile note already
  flagged this as follow-up debt.

## Attribution

Ranked suspects, with the evidence for each:

1. **Transient build peaks** (`uv sync` / `npm ci` / `gcc` / `node-gyp` /
   `tsc` / test runners, launched autonomously per the seeded "Development
   environments" brief). Only plausible 7 GB shape on an 8 GB box; no
   parallelism caps existed. Addressed by TASK-1.2
   (`SCH_NODE_HEAP_MB`/`SCH_BUILD_JOBS` caps on every launch path).
2. **Harness steady-state (Node)** — `opencode` plus `opencode serve`
   plus MCP children raise the floor the peak is measured from (~1.4 GB
   measured). Not the spike, but it prices the idle tail after one.
   Addressed by TASK-1.4 lifecycle guardrails.
3. **Checkpoint amplifier** — 60 s full-tree fingerprint + tar with no
   excludes; regenerable dirs inflated both artifact size (~33 MB per
   checkpoint on the representative tree) and tick I/O. Addressed by
   TASK-1.3 (excludes + post-restore rebuild).
4. **Shim** — `main.py` + boto3 + notifier threads, ~150 MB. Noise.

## Live re-run checklist (operator follow-up, needs a billed session)

First checkpoint after the TASK-1 deploy re-uploads the repo once (smaller);
later ticks stay quiet (spec checkpointing I2). To close the loop on billed
cost:

1. Fresh opencode workspace on this repo; start
   `bin/mem-trace.sh -o trace.csv -d <workload_s>` in the shell.
2. Run the representative workload: explore + edit + the repo suites
   (`cli/tests`, `image/app/test_*.py`, `infra/test_*.py`, tunnel tests),
   noting wall-clock bootstrap/build/test/checkpoint boundaries.
3. At terminal state, record from `sch status` / shim `info`: harness,
   workload, `checkpoint.last_sizes`, artifact sizes, idle-tail duration.
4. Pull the session's vended 1-second `agent.runtime.*.used` logs and the
   `MemoryUsed-GBHours` metric; align spikes to the noted boundaries.
5. Re-run the same workload with caps on; compare peaks. Bootstrap of a
   fresh workspace must still succeed (Node + Python).
6. Full checkpoint-loss-restore cycle via `bin/verify-l2.sh` (excludes
   active, env rebuilt on restore).
