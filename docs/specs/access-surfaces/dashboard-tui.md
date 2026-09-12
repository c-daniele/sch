# Dashboard TUI

> Domain: [Access surfaces](../README.md) · Status: Implemented · Source: rationalized from openspec specs (2026-08-28)

## Purpose

Offline-first full-screen workspace dashboard: one aggregated view of all
known workspaces, refreshed without waking microVMs, with interactive handoff to the
existing session and web views and an explicit, confirmed handoff to `sch delete` for the selected workspace. All other mutating operations remain the responsibility of the
explicit `sch` commands.

## Scope

In scope:
- `sch dashboard` full-screen TUI implemented with the Python standard library only.
- Offline-first refresh from S3 (`task-status.json`) with the operator's local
  credentials.
- Foreground handoff to `sch run`/`sch shell`, detached launch of `sch web`
  bridges, and foreground handoff to `sch delete` for the selected workspace.
- Read-only posture except for the explicit workspace-deletion handoff (no stop,
  reset-session, or task submission).

Out of scope:
- The tunnel and web-bridge behavior itself
  ([Remote UI tunnel](remote-ui-tunnel.md)).
- Other mutating `sch` commands (stop, reset-session, task submission) — invoked
  only as explicit standalone commands, never from the dashboard.

## Requirements

### Aggregated workspace view

**R1.** `sch` SHALL expose an `sch dashboard` command that opens a full-screen TUI
implemented exclusively with the Python standard library (no pip dependencies, no
curses), working with parity on macOS, Linux, and Windows (terminals with VT
support). On Windows the command SHALL attempt to enable VT sequences and MUST fail
with an explicit error — suggesting a compatible terminal (e.g. Windows Terminal),
not with corrupted output — if the terminal does not support them.

**R2.** The view SHALL list all known workspaces (local index or registry, same
source as `sch list`) with at least: name, harness, storage backend, most recent
task state, heartbeat/completion time, checkpoint status when available, plus an
indicator for web bridges launched by the dashboard itself. A detail row SHALL show
extended information for the selected workspace.

**R3.** The selection SHALL be anchored to the workspace name and MUST remain stable
across refreshes; row ordering MUST NOT change non-deterministically.

### Offline-first refresh

**R4.** The dashboard SHALL update task state by reading the `task-status.json`
objects from S3 with the operator's local AWS credentials (same offline-first path
as `sch status`), with parallel fan-out, on a configurable periodic interval (default
on the order of tens of seconds, overridable by flag) as well as on demand with a
manual refresh key.

**R5.** The refresh cycle MUST NOT emit any invocation toward the AgentCore runtime
(no microVM wake): microVMs SHALL be contacted exclusively as the effect of an
explicit operator action (opening a session or a web view).

**R6.** The refresh SHALL be asynchronous with respect to the UI loop: the view
shows the latest snapshot with an indication of its age and MUST NOT block waiting
for I/O; navigation and keys remain responsive during refresh cycles.

**R7.** Per-workspace read errors (credentials, missing object, workspace never
checkpointed) SHALL degrade to an unknown state on the affected row without
interrupting the loop or the refresh of the other workspaces.

### Foreground handoff to interactive sessions

**R8.** The dashboard SHALL allow opening, on the selected workspace, the harness
TUI (equivalent of `sch run <ws>`) and the login shell (equivalent of
`sch shell <ws>`) via foreground handoff: exit from full-screen mode, restore of the
terminal state, execution of the command as a foreground child process with
inherited stdio, wait for its exit, then re-entry into full-screen mode with a full
redraw and an immediate data refresh. Terminal state restore MUST also happen on a
child crash or an exception in the dashboard loop.

**R9.** The existing detach (`Ctrl+]`) and closing the remote TUI SHALL return the
operator to the dashboard without additional steps. The behavior of the `sch run`/
`sch shell` commands invoked standalone MUST remain unchanged (the POSIX
process-replacement path does not change); the handoff SHALL use a dedicated
spawn+wait primitive.

### Web view launch

**R10.** The dashboard SHALL allow opening the browser view (equivalent of
`sch web <ws>`, [Remote UI tunnel](remote-ui-tunnel.md)) on the selected workspace,
launching the bridge as a detached process that MUST survive the dashboard's exit,
and opening the browser on the local URL reported by the bridge (the bridge is
spawned with `--no-browser`; the dashboard reads its ready line and opens the URL
itself).

**R11.** The dashboard SHALL track the bridges it launched (pid and URL, in memory)
and show an indicator on the workspace row; a new web request on a workspace with an
already active dashboard-launched bridge SHALL reopen the browser on the existing URL
instead of duplicating the tunnel.

**R12.** On workspaces whose harness has no web UI (today: `claude` and `pi`) the
web action SHALL be disabled at the root with a visual indication (`n/a` in the web
column), without runtime calls.

**R13.** Terminating the detached bridges remains an explicit operator gesture, and
the dashboard MUST NOT terminate them implicitly on its own exit.

### Read-only posture

**R14.** The dashboard MUST NOT expose actions that mutate the remote runtime state
of workspaces (no stop, reset-session, or task submission): the available actions
SHALL be limited to navigation, refresh, status detail viewing, launching views
(harness TUI, shell, browser), and a single mutating handoff — workspace deletion
via foreground handoff to the explicit `sch delete <ws>` command (exit full-screen
mode, restore terminal state, execute `sch delete` as a foreground child with
inherited stdio and its own confirmation, wait for exit, then re-enter full-screen
mode with a full redraw and an immediate refresh). All other mutating actions remain
the responsibility of the explicit `sch` commands invoked outside the dashboard.
Rationale: deletion is an offline registry/S3 operation (no microVM wake), requires
explicit confirmation inside `sch delete`, and keeps the dashboard as the workspace
switchboard without duplicating stop/task semantics.

## Behavior

- `sch dashboard` with several workspaces: one row per workspace with name,
  harness, storage, task state, a detail row for the selected one, and a key bar.
- Arrow keys during a refresh: the selection stays on the same workspace (anchored
  by name); ordering is deterministic.
- Windows console without VT support: explicit error suggesting a compatible
  terminal, no corrupted drawing.
- The dashboard stays open for multiple refresh cycles on stopped microVMs: state
  updates come from S3 reads; no microVM is started or woken.
- One workspace's read fails while others succeed: the affected row shows an
  unknown state; the other rows update normally, no crash.
- Refresh in flight: keys remain responsive; the previous snapshot and its age stay
  visible.
- `Enter` (open TUI) then `Ctrl+]`: the harness TUI occupies the terminal during the
  session and, on detach, the dashboard redraws with updated data while the remote
  TUI stays alive in the microVM. Quitting the TUI instead returns to the dashboard;
  the microVM follows its existing idle-timeout lifecycle.
- Child crash/handshake error: the dashboard re-enters full-screen mode with the
  terminal in a consistent state and shows the error, never leaving the terminal in
  raw mode or with the alternate screen active.
- `sch run myws` standalone: identical behavior to before the dashboard's
  introduction, including process replacement on POSIX.
- Web key on an `opencode` workspace with no active bridge: the bridge starts
  detached, the browser opens on the local URL, and the row shows the active-bridge
  indicator. Pressing the key again while that bridge is alive reopens the browser
  on the existing URL, no second tunnel.
- Exiting the dashboard with a launched bridge active: the bridge and the browser
  session keep working.
- Web key on a `claude`/`pi` workspace: shown unavailable; pressing it produces no
  runtime call.
- `d` (delete) on the selected workspace: the dashboard leaves full-screen mode,
  restores terminal state, runs `sch delete <ws>` as a foreground child with
  inherited stdio (so the existing `sch delete` confirmation applies), waits for
  exit, then re-enters full-screen mode with a full redraw and an immediate
  refresh; on success the in-memory web bridge for that workspace is dropped and
  `deleted <ws>` is shown, on non-zero exit the status is shown, and the terminal
  is restored consistently even on crash.

## Invariants

**I1.** No dashboard refresh cycle ever invokes the AgentCore runtime; the only
runtime contacts are explicit operator actions (session open, web view).

**I2.** The selection is always anchored to a workspace name and survives any
refresh.

**I3.** After any handoff or failure, the terminal is restored to a consistent state
(no raw mode, no alternate screen left active).

**I4.** A dashboard-launched web bridge is never a child killed by the dashboard's
exit; it is terminated only by an explicit operator gesture.

**I5.** The dashboard never performs stop, reset-session, or task submission itself; workspace deletion is performed only by handing off to the external `sch delete` command (no in-TUI deletion logic).

## Cross-references

- Web bridge and tunnel semantics: [Remote UI tunnel](remote-ui-tunnel.md).
- Code: [cli/sch/commands/dashboard.py](../../../cli/sch/commands/dashboard.py),
  [cli/sch/commands/web.py](../../../cli/sch/commands/web.py).
- [MANIFESTO](../../../MANIFESTO.md).
