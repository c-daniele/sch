# CLI cross-platform

> Domain: [Access surfaces](../README.md) · Status: Implemented · Source: rationalized from openspec specs (2026-08-28)

## Purpose

The `sch` CLI provides a single implementation with identical observable behavior across all supported platforms (macOS, Linux, Windows), requiring nothing beyond a repository checkout, `python3 ≥ 3.8`, the `aws` CLI, and `agentcore`.

## Scope

In scope:
- Single Python implementation, entry-point shims, interpreter resolution.
- Command parity across platforms, including Windows availability of editor and deletion commands.
- stdout/stderr discipline, exit codes, environment variables, workspace index format compatibility with the previous bash/PowerShell implementation.
- Safe delegation to external processes.

Out of scope:
- Per-command semantics (see the individual access-surface specs: interactive shell, headless tasks, and the sync and deletion capabilities).
- Harness selection rules (covered by the `harness-selection` capability) and local sync options (`local-workspace-sync` capability).

## Requirements

### Implementation and entry points

**R1.** The `sch` CLI SHALL be a single multi-module Python 3 package in `cli/sch/`, using exclusively the standard library (minimum Python 3.8), with no pip-installable dependencies and no installation step beyond checking out the repository. Every `sch` command MUST work from a bare checkout without `pip install` or a virtualenv.

**R2.** When executed with a Python interpreter older than the minimum supported version, the CLI SHALL exit with code 1, an `sch: ...` message on stderr indicating the required version, and no traceback.

**R3.** `bin/sch` (POSIX) and `bin/sch.ps1` (Windows) SHALL be minimal shims free of application logic: their sole job is to locate a suitable Python interpreter and delegate the entire invocation to the `cli/sch/` package, propagating arguments and exit code unchanged. On Windows the shim MUST resolve the first available interpreter in the order `python3`, `python`, `py -3`. When no Python interpreter is available, the shim SHALL exit with code 1 and an explicit stderr message naming `python3` as a prerequisite.

### Command parity

**R4.** The CLI SHALL expose the same commands on all platforms: `shell`, `open`, `run`, `list`, `task`, `status`, `stop`, `reset-session`, `delete`, `info`, `acp`, `zed-config`, `attach`, `web`, `dashboard`, and `fetch`. `acp`, `zed-config`, `attach`, `web`, `dashboard`, `fetch`, and `delete` MUST also work on Windows through the same shared code path, with the same contracts and exit codes (including delete's confirmation, stop, remote purge, local cleanup, and the bulk `--all` flow with strengthened confirmation and per-target outcomes).

**R5.** When a command that requires a local prerequisite (`node`, `opencode`) is invoked and the prerequisite is not resolvable, the CLI SHALL exit with code 1 and an `sch: ...` message on stderr before any state mutation (workspace index, status markers, runtime invocations).

### Observable behavior parity

**R6.** The CLI SHALL preserve the observable behavior of the previous reference bash implementation: same flags; same exit codes (0 success, 1 error, pass-through of the delegated process's exit code for `shell`/`run`/`acp`/`attach`); same `SCH_*` environment variables with the same defaults (`SCH_REGION`, `SCH_PROJECT`, `SCH_ENV`, `SCH_RUNTIME_ARN`, `SCH_CHECKPOINT_BUCKET`, `SCH_DEFAULT_HARNESS`, `SCH_TUNNEL_MAX_CHANNELS`, `SCH_ACP_MIRROR_ROOT`, `SCH_NODE_BIN`, `SCH_AWS_BIN`); same output format (`list` columns, `key : value` rendering of `status`, no ANSI colors).

**R7.** stdout SHALL carry exclusively machine-consumable output: `task_id` for `sch task`, the status JSON for `sch status --json`, the JSON snippet for `sch zed-config`, the ACP JSON-RPC frames for `sch acp` — with no diagnostic lines intermixed. Every diagnostic SHALL be prefixed `sch: ` on stderr.

**R8.** The existing `bin/verify-*.sh` scripts SHALL pass against the Python implementation without modification to the scripts themselves.

### Workspace index compatibility

**R9.** The CLI SHALL read and write the workspace index in `~/.config/sch/` (honoring `XDG_CONFIG_HOME`) in the previous implementation's format: JSON files `workspaces/<ws>` with keys `runtimeSessionId` and `harness`; textual files `.status.<ws>` of the form `<status> <ISO-8601 UTC timestamp>`; plain-text caches `runtime-arn` and `checkpoint-bucket`. On read the CLI MUST tolerate the legacy formats (bare string, JSON scalar, `sessionId` key) without requiring any migration, applying the legacy harness reconciliation rules of the `harness-selection` capability.

### Per-platform interactive semantics

**R10.** For interactive commands (`shell`, `open`, `run`) the CLI SHALL yield the terminal to the delegated `agentcore` process: on POSIX by replacing its own process (exec), on Windows by running the delegated process in the foreground and exiting with its same exit code.

**R11.** For `acp` and `attach` the CLI MUST run the `node` process as a foreground child with inherited stdio (no intermediary pipes) on all platforms, so that the post-exit cleanup (`mark-interactive active=false`, `*-closed` status marker) is performed and the node process's exit code is returned. Frames exchanged between editor and node process MUST transit through handle inheritance without Python intermediating, altering, or buffering them.

### Safe delegation

**R12.** Every external process invocation (`aws`, `agentcore`, `node`, harness) SHALL use arguments in list form (argv), with no intermediary shell and no string interpolation in the command. User-supplied data inserted into JSON payloads (prompt, workspace names, timeout) MUST be serialized via the `json` module; in particular `--timeout` MUST be validated as an integer before insertion. Delegations SHALL remain: the `aws` CLI for the control plane, `agentcore exec --it` for the interactive shell, `node tunnel/*.js` for ACP and attach — no new prerequisites.

## Behavior

- `sch task <ws> --timeout abc "<prompt>"` → exit 1, `sch: ...` on stderr, no invocation sent to the runtime.
- A prompt containing hostile characters (`"quote", $var, \`backtick\`, ; rm -rf`) reaches the remote shim byte-for-byte identical, encapsulated in the JSON payload, with no local execution or expansion.
- `sch delete <ws> --yes` with all phases succeeding → empty stdout, completion report on stderr, exit 0. A failed mandatory phase → empty stdout, stderr identifies the workspace and failed phase (`quiesce`, `S3 purge`, `registry finalize`, or `local cleanup`), exit 1, without exposing credentials or sensitive payloads.
- `sch delete --all` with mixed outcomes → empty stdout; stderr carries one outcome per workspace sorted by name, final counts, and the targets to retry.

## Invariants

**I1.** Diagnostics go only to stderr with the `sch: ` prefix; stdout never mixes diagnostics with machine-consumable output.

**I2.** No command mutates state after a missing-prerequisite error: the check precedes every mutation.

**I3.** The workspace index is read-compatible with legacy formats and never requires a migration step.

**I4.** External processes are only ever launched with argv lists — never through a shell, never with interpolated command strings.

## Cross-references

- [MANIFESTO](../../../MANIFESTO.md) — project constitution and surface hierarchy.
- Entry points and implementation: [bin/sch](../../../bin/sch), [cli/sch/](../../../cli/sch), [cli/sch/cli.py](../../../cli/sch/cli.py).
- Delegated processes: [tunnel/](../../../tunnel) (ACP and attach), `agentcore exec --it` (interactive shell, see [interactive-shell-access.md](interactive-shell-access.md)).
- End-to-end checks: [bin/verify-headless-tasks.sh](../../../bin/verify-headless-tasks.sh) and the other `bin/verify-*.sh` scripts (R8).
- Related capabilities (specs to be rationalized): `harness-selection`, `local-workspace-sync`, workspace deletion contract.
