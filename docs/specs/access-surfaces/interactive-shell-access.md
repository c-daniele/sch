# Interactive shell access

> Domain: [Access surfaces](../README.md) · Status: Implemented · Source: rationalized from openspec specs (2026-08-28)

## Purpose

Opening, detaching from, and reconnecting to an interactive terminal inside the AgentCore microVM; launching the coding-agent harness TUI in the remote runtime; and managing multiple distinct runtime sessions (1 `runtimeSessionId` = 1 workspace) through the `sch` wrapper.

## Scope

In scope:
- `sch shell` / `sch open` / `sch run` interactive terminal via the AgentCore CommandShell channel.
- Remote TUI launch through the single harness dispatcher, ENV bridge, readiness gating.
- Detach (`Ctrl+]`), reconnect, and presence-lease semantics for the CommandShell channel.
- Multi-workspace management (`sch list`, `sch stop`) and interactive-session sync preflight.

Out of scope:
- Headless task execution and auto-approval flags (see [headless-task-execution.md](headless-task-execution.md)).
- Cross-platform CLI rules and stdout discipline (see [cli-cross-platform.md](cli-cross-platform.md)).
- Sync reconciliation internals and deletion (`local-workspace-sync` and deletion capabilities).
- `sch web`, `sch attach`, `sch acp` tunnel semantics beyond their exclusion from CommandShell state (R3).

## Requirements

### Opening an interactive terminal

**R1.** The system SHALL open an interactive terminal inside the microVM via `agentcore exec --it` (the `InvokeAgentRuntimeCommandShell` channel), wrapped by `sch shell <workspace>`, which SHALL resolve the workspace name to a valid `runtimeSessionId` (≥ 33 characters, generated and stored by the wrapper). For a workspace never used before, the wrapper SHALL generate a conformant `runtimeSessionId`, associate it with the workspace name, and open the shell in the corresponding microVM. For a known workspace, the wrapper SHALL reuse the associated `runtimeSessionId` so the shell opens in the same runtime session (same session storage).

### Remote TUI launch

**R2.** The user SHALL be able to launch the TUI of the harness selected for the workspace (`opencode` or `claude`, persisted by the `harness-selection` capability) in the remote shell, operating on the canonical worktree `/mnt/workspace/repo`, with rendering and interactive input usable from the local terminal (including terminal-resize adaptation without corrupted rendering, for any harness).

**R3.** The wrapper SHALL launch the harness through a single dispatcher (`harness-wrapper.sh`) that applies the same environment-variable bridge required to survive the minimal `bash --login` shell of `agentcore exec --it` (XDG paths, `AWS_PROFILE`, `AWS_REGION` and, for claude, `CLAUDE_CODE_USE_BEDROCK=1`) and that unblocks the harness only once the readiness marker has been verified.

**R4.** The TUI path MUST present permission confirmation prompts to the user (no auto-approval): auto-approval flags are exclusive to the headless path (see [headless-task-execution.md](headless-task-execution.md)) and MUST NOT appear on the TUI argv of any harness.

**R5.** For harness=opencode on a fresh workspace, the TUI SHALL use `remote-interactive` as the default agent via the seeded config (see the `runtime-image` capability, "Custom OpenCode agents for the microVM"). The TUI MAY explicitly select other agents; the wrapper MUST NOT modify the argv in that case.

### Detach and reconnection

**R6.** The system SHALL support detaching from the interactive shell with the `Ctrl+]` sequence, leaving the remote processes running, and reconnecting to the same shell using the `runtimeSessionId` + `shellId` pair, with replay of the recent buffer (up to 256 KB) provided by the CommandShell channel.

**R7.** `sch run` and `sch shell` SHALL assign and explicitly pass a `shellId` for each logical shell and SHALL publish distinct presence leases to the shim for each connected local client. Each client SHALL register its presence before opening the CommandShell channel, renew it periodically while connected, and revoke it best-effort when the local process terminates (detach, closure, or error).

**R8.** Each lease MUST have a short, bounded expiry, so an abnormal disconnection (client, laptop, or network lost without revocation) makes the session detached without requiring cleanup from the lost client. The session SHALL be considered attached as long as at least one client lease is valid, regardless of the existence of the TUI process. Reconnect SHALL create or renew a lease without restarting the remote shell or losing the TUI process.

**R9.** With two clients attached to the same session, termination of one SHALL revoke only that client's lease; the session stays attached while the other lease is valid. Periodic renewals SHALL keep a lease valid without modifying the shell's state or output.

**R10.** `sch web`, `sch attach`, and `sch acp` MUST stay outside the CommandShell state: without a connected `sch run` or `sch shell` client, those tunnels do not create a CommandShell lease and the interactive session remains detached (relevant to the Telegram milestones).

### Best-effort guarantees of the interactive modes

**R11.** `sch run`, `sch shell`, `sch attach`, and `sch acp` SHALL offer live sync and periodic checkpoints while the microVM is active, but detach MUST NOT be documented as marking the end of a harness turn and does not guarantee a terminal checkpoint. For asynchronous work with observable outcome and durability, the CLI SHALL direct the user to `sch task` and `sch status`.

### Multiple runtime sessions

**R12.** The wrapper SHALL manage multiple workspaces in parallel under the model 1 `runtimeSessionId` = 1 workspace, offering at least `sch list` (known workspaces and their state) and `sch stop <workspace>` (explicit shutdown via `StopRuntimeSession`, preserving the session storage). Shells on different workspaces SHALL run in distinct microVMs with separate session storage and no mutual filesystem visibility.

### Local sync in interactive sessions

**R13.** `sch run <workspace>` and `sch shell <workspace>` SHALL accept the binding, bootstrap, and conflict options defined by `local-workspace-sync`. When sync is active, the command SHALL complete reconciliation and the barrier before opening the shell or arming the TUI autostart, then keep the sync process alongside the CommandShell client for the duration of the connection.

**R14.** Termination of the local process SHALL close the `fs` channel and release the lease without forcibly terminating a remote shell or TUI left active via detach; subsequent remote changes are reconciled on the next connection. A preflight conflict (e.g. `abort` policy) SHALL prevent the shell and TUI from opening, release the lease, and terminate the command with unambiguous diagnostics.

### Session resumption

**R15.** `sch run <workspace>` SHALL accept an optional `--continue` flag that resumes the harness's most recent session in the TUI instead of opening a fresh one: the `prepare-run` payload SHALL carry the request, the shim SHALL resolve the latest session with the same per-harness resolvers as `sch task --continue` (`opencode.db` session via `--session <id>`, Claude JSONL transcript via `--resume <id>`, Pi session file via `--session <path>`), and the autostart SHALL exec the corresponding resume argv. The shim SHALL run that resolution only once the workspace is ready (session-store restore landed), waiting for readiness up to a bound (240 s by default) inside the `prepare-run` invocation, so a `sch stop` followed by `sch run --continue` resumes the pre-stop session on a cold boot instead of arming a fresh TUI; the CLI SHALL raise its invocation read timeout above that bound for this call. Resolution failure or no prior session, checked on a ready workspace, SHALL degrade to the fresh TUI argv without failing and the CLI SHALL say so. A workspace still not ready after the bound SHALL be an explicit `prepare-run` error (no autostart armed) that tells the operator to retry, never a silently fresh TUI. Without `--continue` the `prepare-run` invocation SHALL NOT wait for readiness (the dispatcher gates the TUI itself). The flag SHALL compose with `--model` (both forwarded as discrete argv elements) and leave workspace bindings and persisted metadata untouched.

## Behavior

- `sch shell newws` on a fresh workspace → generates and stores a `runtimeSessionId` (≥ 33 chars), opens an interactive shell in the microVM.
- `sch shell myws` on a known workspace → reuses the stored `runtimeSessionId`; same session storage.
- `Ctrl+]` with the TUI open → local client revokes its lease best-effort and disconnects; shell and TUI keep running in the microVM.
- Reconnect with the same `runtimeSessionId` + `shellId` → new lease registered, shell found in its prior state, recent output replayed (up to 256 KB).
- All leases expired + reconnect → session becomes attached again from the new lease, without losing the TUI process or creating a new logical shell.
- `sch stop myws` → `StopRuntimeSession` invoked, microVM stopped, session storage preserved.
- `sch run myws --sync .` in a local project with a new remote → project materialized and confirmed in `/mnt/workspace/repo` before the TUI starts.
- `sch run myws --continue` after detaching from a brainstorm → TUI reopens the harness's latest session; with no prior session it opens fresh (the CLI says so).
- `sch stop myws` then `sch run myws --continue` on a cold microVM → `prepare-run` waits for the restore before looking up the session; the TUI reopens the pre-stop session. If the workspace is still restoring after the bound, the command fails with a retry hint instead of opening a fresh TUI.

## Invariants

**I1.** A `runtimeSessionId` is ≥ 33 characters and is always generated and stored by the wrapper before first use.

**I2.** No auto-approval flag ever appears on an interactive (TUI) harness argv, for any harness.

**I3.** The ENV bridge is applied by the dispatcher before the exec of the real harness binary, for every harness.

**I4.** Attach state is a function of valid leases only: ≥ 1 valid lease ⇒ attached; 0 valid leases ⇒ detached, without remote process termination.

**I5.** Observation and tunnel commands (`web`, `attach`, `acp` without `run`/`shell`) never create a CommandShell lease.

**I6.** Interactive reconciliation and barrier always precede shell opening or TUI autostart when sync is active.

## Cross-references

- [MANIFESTO](../../../MANIFESTO.md) — project constitution and core loop.
- Wrapper and delegation: [cli/sch/](../../../cli/sch), [bin/sch](../../../bin/sch); cross-platform rules in [cli-cross-platform.md](cli-cross-platform.md).
- Headless counterpart: [headless-task-execution.md](headless-task-execution.md) (auto-approval flags and detach semantics rationale; R2 for the same post-ready session resolution applied to `sch task --continue`).
- `--continue` implementation: [cli/sch/commands/run.py](../../../cli/sch/commands/run.py) (`PREPARE_RUN_READ_TIMEOUT_S`, read-timeout override and error rendering), [cli/sch/runtime.py](../../../cli/sch/runtime.py) (`invoke_verified(read_timeout_s=...)`), [image/app/main.py](../../../image/app/main.py) (`prepare-run` handler, `PREPARE_RUN_READY_TIMEOUT_S`, `_wait_workspace_ready_or_error`), [image/scripts/sch-run-profile.sh](../../../image/scripts/sch-run-profile.sh) (run-once marker consumer).
- Remote side: `image/` (harness dispatcher `harness-wrapper.sh`, seeded agents and readiness markers — see the `runtime-image` capability, to be rationalized).
- Sync options: `local-workspace-sync` capability (to be rationalized under `docs/specs/sync-and-git/`).
