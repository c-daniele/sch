# 2026-09-14 Memory cost: peak-billing trace plan

Temporary exploration note for TASK-1. Nothing here is durable;
decisions that stick move to the task, specs, or journal.

## Problem

Operator measurement over ~10 days: memory is ~78% of sch cost.
Session hit ~7 GB peak, then released — but AgentCore bills memory on
peak-consumed-up-to-that-second, per second, 128 MB minimum, boot to
microVM shutdown. Releasing does not lower the billable value.

Sources: https://aws.amazon.com/bedrock/agentcore/pricing/ (peak-memory
billing, 128 MB minimum, 1-second minimum) and the lifecycle note
(idle `idleRuntimeSessionTimeout` shutdown or `StopRuntimeSession` is
the only thing that stops the meter):
https://aws.amazon.com/blogs/machine-learning/its-safe-to-close-your-laptop-now-hosting-coding-agents-on-amazon-bedrock-agentcore/

Implication: optimize for **never spiking** and **short sessions after
spikes**, not for low average RSS.

## Live evidence (2026-09-14, opencode run session, mid-brainstorming)

Read-only `ps` + `/proc/meminfo` snapshot, negligible observer cost:

- Box: `MemTotal` ~8 GB. A 7 GB peak is ~90% of the machine.
- Idle-ish floor ~1.3 GB: `opencode` 936 MB RSS (VSZ 76 GB is address
  reservation, not billed), shim `main.py` 156 MB, `mcp-proxy` 102 MB,
  `aws-docs` MCP 65 MB.
- Several zero-RSS zombie `[git]` processes — no billing impact, possible
  unreaped-child hygiene issue, out of scope for TASK-1.
- NOT measured: the billed (server-side metered) peak — lives in
  CloudWatch vended metrics/logs + bill, not in `/proc`.

## Suspects (ranked, with code refs)

1. Transient build peaks — `uv sync` / `npm ci` / `gcc` / `node-gyp` /
   `tsc` / test runners, launched autonomously per the seeded
   "Development environments" brief (`image/opencode-templates/AGENTS.md`,
   `image/claude-templates/CLAUDE.md`, `image/pi-templates/AGENTS.md`).
   No parallelism caps exist in `image/Dockerfile`,
   `image/scripts/harness-wrapper.sh`, or `image/scripts/init-workspace.sh`.
   Only plausible 7 GB shape observed.
2. Harness steady-state (Node) — `opencode`/`claude`/`pi` plus
   `opencode serve` (`serve-ensure` in `image/app/main.py`) plus MCP
   children (`awslabs.aws-documentation-mcp-server`,
   `mcp-proxy-for-aws-cli` in `/opt/uv-tools`, `context7-mcp` if enabled).
   Raises the floor the peak is measured from. Defaults sane (context7
   disabled per `docs/specs/platform/runtime-image.md` R33-R34).
3. Checkpoint amplifier — every `SCH_CHECKPOINT_INTERVAL` (default 60s)
   `_fingerprint_repo()` walks the whole worktree file-by-file and
   `_create_archive(REPO_DIR, ...)` tars it with no excludes
   (`image/app/main.py`); `node_modules/` + `.venv/` ride both.
   Follow-up debt already noted in `image/Dockerfile` v29 comment.
4. Shim — `main.py` + boto3 + notifier threads, tens of MB. Noise.

## Trace plan (TASK-1.1, no code change)

1. Representative workload (confirmed 2026-09-14): harness=opencode,
   workload class = sch self-improvement on this repo (coding, analysis,
   tool usage, tests). Exact historical task unknown, so trace a fresh
   instance: fresh opencode workspace on this repo doing explore + edit +
   the repo test suites (`cli/tests`, `image/app/test_*.py`,
   `infra/test_*.py`, tunnel tests) as the agent would run them.
2. Fresh session, run workload, capture wall-clock phase boundaries
   (bootstrap start/end, build start/end, task terminal state from
   `checkpoints/<ws>/task-status.json`).
3. Pull AgentCore session-level 1-second vended logs
   (`agent.runtime.*.used`) and `MemoryUsed-GBHours` (1-min) per
   https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-runtime-metrics.html
   and align spikes to phase boundaries.
4. Record `info`-action `last_sizes`, artifact sizes, and the idle-tail
   GB-hours after the peak (quantifies the poisoning effect).
5. Write the attribution (phase + evidence) into TASK-1.1; TASK-1.2-1.4
   stay blocked on it.

## Levers queued (not decided)

- B. Caps: `NODE_OPTIONS=--max-old-space-size`, `MAKEFLAGS`,
  npm/uv/tsc/test worker limits, per-workspace escape hatch.
- C. Steady-state: audit `serve` + MCP children; keep context7 default-off;
  consider `aws-docs` opt-in for memory-sensitive work.
- D. Lifecycle: lower `idleRuntimeSessionTimeout` (default 900s,
  `infra/agent_runtime.yaml`) + `sch stop` discipline; isolate heavy
  phases in throwaway sessions (workspace checkpoint identity is stable,
  `docs/specs/workspace-lifecycle/workspace-checkpointing.md` R11);
  exclude regenerable dirs from L2 tar + fingerprint.
- E. Guardrails: peak in `sch status`, CloudWatch alarm on
  `MemoryUsed-GBHours` (masterplan roadmap already lists cost alarms).

## Open questions (updated 2026-09-14)

- RESOLVED: harness=opencode, workload class = sch self-improvement
  (coding/analysis/tool-use/tests). Exact task unrecoverable; tracing a
  representative instance instead.
- What idle timeout is tolerable (cold-start cost vs GB-hours)?
- Is OOM-fail-fast acceptable with an escape hatch, or must big builds
  always succeed regardless of peak?
