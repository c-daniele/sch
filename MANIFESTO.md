# SCH Manifesto

SCH — Serverless Coding Harness. This document is the project's constitution: every
architecture and product decision must align with it. When code, specs, and this file
disagree, this file wins and the rest must be brought back in line.

## Who SCH serves

**Primary user: the individual developer** running AI coding agents as part of their
daily workflow — someone who wants to fire off long, unattended tasks, run several
agents in parallel, and supervise everything from a laptop or a phone without
operating infrastructure.

**Natural extension: small and mid-sized teams**, who get a centrally controlled,
auditable coding-agent environment: one version-pinned container image, one IAM
execution role as the real permission boundary, per-user isolated workspaces.

When trade-offs must be made, optimize defaults for the individual power user first;
team features must never complicate the single-user path.

## The core loop

1. **Dispatch** — the user starts a workspace session from the `sch` CLI (or an
   editor/phone). A disposable microVM is provisioned on demand.
2. **Work** — a coding harness (OpenCode, Claude Code, or Pi) runs inside the
   microVM: interactively, detached, or headless.
3. **Checkpoint** — workspace state (repo, agent sessions, config) is periodically
   and terminally saved to S3.
4. **Supervise** — the user observes progress from anywhere: `sch status`, the web
   UI, or Telegram notifications and approvals.
5. **Deliver** — finished work reaches the user's git history (`sch fetch`, git-native
   branches, or file sync).
6. **Evaporate** — the microVM is destroyed on idle timeout or completion. Idle cost
   is approximately zero. The next session restores the checkpoint transparently.

Everything SCH does exists to make this loop fast, safe, and boring.

## Source of truth (layered)

Each layer names its own source of truth:

| Layer | Source of truth |
| --- | --- |
| Code | The git repository (and its remote) |
| Workspace & session state | S3 checkpoints (restore always wins) |
| Product & architecture | `docs/specs/` + this MANIFESTO |
| Work in flight | Backlog tasks (`.backlog/`) |
| Where we are now | `.backlog/masterplan/MASTERPLAN.md` |

No layer may fork its truth. If two places claim to describe the same thing, fix the
docs — don't add a third place.

## Surface hierarchy

Interfaces, in priority order. Every capability must be reachable from the top of
this list; lower surfaces are conveniences, never the only path.

1. **`sch` CLI** — the foundation. If it can't be done from the CLI, it doesn't exist.
2. **Editor (ACP)** — Zed and other ACP editors connect to a workspace as a remote
   agent, with a synced local file mirror.
3. **Web UI** — browser access to the remote harness UI.
4. **Telegram** — supervision only: notifications, remote tool approval, follow-up
   prompts. Telegram is never a control plane and never holds capability that the
   CLI lacks.

## Interface posture

Lean and terminal-first:

- **One entrypoint**: `sch`. No local daemons, servers, or background processes of
  our own on the user's machine.
- **Detached by default**: long tasks start and return immediately; the session keeps
  running on the server. Attaching is a choice, not a requirement.
- **Observable from anywhere**: every running task is inspectable (`sch status`,
  web, Telegram) without holding a terminal open.
- **Progressive disclosure**: a first run needs one command; advanced behavior
  (storage classes, providers, sync rules, harness selection) is opt-in via flags
  and config.

## Design principles

1. **Serverless only.** No bastion hosts, no Kubernetes, no long-lived VMs. Compute
   exists only while a session runs (Bedrock AgentCore Runtime); everything else is
   managed services.
2. **Disposability over durability of machines.** MicroVMs are cattle. Durability
   comes from checkpoints, never from keeping a machine alive.
3. **Near-zero idle cost.** A workspace that isn't running must cost (almost)
   nothing.
4. **Git-native parallelism.** N agents work on N branches from the same local HEAD
   with no shared writable state. Delivery is a git operation.
5. **One audited image.** All harnesses run in a single hardened, version-pinned
   container image. The image is part of the contract and changes are deliberate.
6. **IAM is the real boundary.** The workspace execution role — not container walls
   alone — defines what an agent can touch in AWS. Scope roles per workspace and per
   user.
7. **Restore always wins.** After idle expiry, crash, or version update, state is
   restored from the latest checkpoint, transparently.
8. **Verify everything.** Each capability ships with a runnable verification script
   (`bin/verify-*.sh`) and tests. If it isn't verified, it isn't done.

## Boundaries

SCH is not:

- **A general VM or container hosting service.** It runs coding harnesses, period.
- **A CI/CD system.** It may run builds inside agent tasks, but pipeline semantics
  (triggers, matrix, artifacts) are out of scope.
- **A PaaS.** It deploys nothing to production.
- **Multi-cloud.** SCH is built on AWS services (Bedrock AgentCore, S3, Lambda) and
  makes no abstraction effort over them.
- **An autonomy grant.** An agent's reach beyond the workspace is exactly what its
   IAM role allows — SCH adds isolation layers, never exceptions.

## Risks

These are accepted, and must be managed, not ignored:

- **Cost surprises.** SCH creates billable AWS resources. The deploy and every
  running session cost money; documentation must say so plainly, and idle timeouts
  must default safely.
- **IAM misconfiguration.** An over-scoped execution role turns a sandboxed agent
  into an incident. Role scoping is reviewed like security code.
- **Checkpoint loss.** State between checkpoints can be lost on session death.
  Checkpoint frequency and terminal saves are correctness features, not optimizations.
- **Secrets leakage.** Synced directories and agent sessions can carry credentials.
  Sync excludes secrets by design; the runtime image must never bake them in.
- **Provider lock-in.** Deep AWS integration is a feature and a risk; price it
  honestly instead of pretending it isn't there.
- **Bus factor.** The project is maintained by one person. Documentation, journal,
  and an always-current masterplan are the mitigation.
