# Remote UI tunnel

> Domain: [Access surfaces](../README.md) · Status: Implemented · Source: rationalized from openspec specs (2026-08-28)

## Purpose

Protocol-neutral byte bridge between local adapters and harness processes in the
AgentCore microVM. It carries three access surfaces over the same transport:
`sch attach` (local OpenCode TUI against the remote backend, rendering/clipboard/
keybindings local, only API calls on the network), `sch acp` (the workspace exposed
as an ACP JSON-RPC agent to compatible editors, both harnesses), and `sch web`
(browser access to the remote OpenCode web UI, no local client required). The
transport guarantees transparent reconnection across the channel's duration and
frame limits with no byte loss or duplication, without the local adapter or editor
noticing.

## Scope

In scope:
- `sch attach`, `sch acp`, `sch web` commands and their local bridge adapters.
- The abstract transport interface over `InvokeAgentRuntimeWithWebSocketStream`.
- Framing with application-level acknowledgement, proactive/reactive reconnection,
  clean closure, orphan timeout.
- The shim-side supervision of the single shared OpenCode backend (`serve-ensure`).
- The `mark-interactive` advisory signal emitted on bridge open/close.

Out of scope:
- The interactive shell channel (`sch shell`/`sch open`/`sch run`), which uses a
  distinct AWS operation and remains unchanged.
- ACP path translation, mirror sync, and fs-capability neutralization (see
  [ACP editor integration](acp-editor-integration.md) and
  [ACP file locality](acp-file-locality.md)).
- Editor configuration generation details ([ACP editor integration](acp-editor-integration.md)).
- Dashboard launching of web bridges ([Dashboard TUI](dashboard-tui.md)).

## Requirements

### Commands and harness availability

**R1.** The system SHALL expose `sch attach <workspace>`, which connects an OpenCode
TUI launched on the local laptop to the `opencode` backend supervised by the shim in
the workspace's microVM. The command SHALL resolve the workspace's persisted harness
and MUST refuse the operation with an explicit error, without any runtime call, when
the persisted harness has no client/server split (today: `claude` and `pi`),
suggesting the alternatives for that harness (`sch acp <workspace>` where supported,
`sch shell <workspace>` always).

**R2.** Before connecting the local TUI, `sch attach` SHALL compare its local OpenCode
version with the one reported by the remote process; on mismatch it SHALL emit an
explicit warning and MUST require the `--force` flag to proceed anyway. An
already-running backend process SHALL be reused, never started twice.

**R3.** The system SHALL expose `sch acp <workspace>`, which starts the ACP agent
process for the persisted harness inside the microVM (`opencode acp` for `opencode`;
the official `@agentclientprotocol/claude-agent-acp` adapter for `claude`) with
working directory `/mnt/workspace/repo`, and connects its stdin/stdout to a local
editor through the bridge. For harness `opencode` the agent start SHALL go through
the existing dispatcher (`harness-wrapper.sh`) to inherit the readiness gate and the
ENV bridge.

**R4.** The `sch acp` bridge SHALL operate at the JSON-RPC message level to apply the
path translation and client-fs capability neutralization defined by
[ACP file locality](acp-file-locality.md), preserving message order, integrity and
semantics: ids, relative order and every field other than the rewritten path/fs
fields remain unchanged. The stdout of `sch acp` MUST contain exclusively ACP
JSON-RPC messages; all diagnostics SHALL go to stderr.

**R5.** The system SHALL expose `sch web <workspace> [--harness opencode]
[--storage s3|session] [--no-browser]`, which opens a local TCP listener on
`127.0.0.1` with an ephemeral port, connects it through the bridge to the supervised
backend, prints the resulting local URL (`http://127.0.0.1:<port>`), and opens the
operator's default browser on that URL. The command MUST NOT require a local
`opencode` binary and MUST NOT perform any local/remote version comparison (the UI
is served by the remote binary and is consistent with it by construction); the
`--force` flag of `sch attach` does not exist on `sch web`.

**R6.** `sch web` SHALL resolve the workspace's persisted harness and MUST reject the
operation with an explicit error, without any runtime call, when the harness has no
web UI (today: `claude` and `pi`), suggesting `sch acp`, `sch shell`, or `sch run` as
alternatives. On a nonexistent workspace the command SHALL create it with the
`opencode` harness (a `--harness claude`/`pi` flag is rejected) and SHALL accept
`--storage` only at creation, with the same rules as `sch attach`. Likewise, `sch acp`
MUST refuse harnesses with no ACP agent (today: `pi`) without any runtime call, and
the dashboard SHALL show `n/a` for harnesses without web.

**R7.** When the shim or probe response indicates a runtime image without web
support, `sch web` SHALL fail with an explicit error stating that the runtime image
predates web access and must be upgraded, instead of showing a broken UI or degrading
silently.

> Status note: `sch web` is code-complete (`cli/sch/commands/web.py`,
> `tunnel/web.js`, `image/app/main.py`) and verified live end-to-end (2026-08-28,
> TASK-5): `bin/verify-remote-ui-tunnel.sh` passed 16/0 live against the deployed
> runtime (serve-ensure `capabilities.web=true`, root UI/asset/session API/SSE
> through the bridge, concurrent attach on the same backend, clean SIGTERM
> shutdown), and the browser UI + `--no-browser` + claude-harness guard were
> verified manually.

### Transport

**R8.** The bridge SHALL expose to the adapters (`sch attach`, `sch acp`, `sch web`)
an abstract transport interface providing an ordered, reliable byte stream,
independent of the underlying physical transport and not tied to a specific
application protocol. The implementation SHALL operate on the
`InvokeAgentRuntimeWithWebSocketStream` operation — distinct from, and not sharing a
concurrency budget with, the interactive shell channel of
`sch shell`/`sch open`/`sch run` — with a dedicated WebSocket connection per client
(no multiplexing over a single connection). The interface MUST be pluggable so a
future transport can be adopted without modifying the adapters' code or `bin/sch`.

**R9.** The bridge SHALL keep the logical stream alive beyond the lifetime of a
single physical connection, transparently handling proactive reconnection before the
connection duration limit is reached and reactive reconnection on unexpected
disconnection, without the local adapter or the connected editor perceiving the
interruption.

**R10.** The bridge SHALL guarantee that no application byte in transit at the moment
of a disconnection is lost or duplicated, via a framing mechanism with
application-level acknowledgement independent of any delivery guarantee of the
underlying transport; on reconnection it SHALL retransmit exclusively the
unacknowledged bytes and discard duplicates.

**R11.** On logical stream closure (local adapter terminated, or EOF on the editor's
stdin), the bridge SHALL signal the closure to the remote end so that the associated
remote process or connection is terminated, without leaving orphan processes in the
microVM. An orphan timeout SHALL terminate the remote end anyway when the closure is
not signaled (e.g. local bridge crash).

**R12.** For `sch attach` and `sch web`, the bridge SHALL open a dedicated transport
channel for each accepted local TCP connection, within a configurable maximum number
of concurrent channels (`SCH_TUNNEL_MAX_CHANNELS`, generous default — not bound by a
budget shared with other `sch` commands), reusing idle channels before opening new
ones. When the number of requested channels exceeds the cap, the bridge MUST return
an explicit error, without silently degrading behavior.

### Shared remote backend

**R13.** The shim SHALL supervise, per microVM, a single OpenCode backend process on
a fixed internal port (`SCH_OPENCODE_SERVE_PORT`, default `4096`), started listening
exclusively on `127.0.0.1` inside the microVM. The backend is OpenCode 2's `serve`
subcommand (`opencode serve --hostname 127.0.0.1 --port <fixed>`), exposing both the
HTTP API (under `/api/*`) and the web UI (on `/`) on the same port; it SHALL be started
lazily at the first `serve-ensure` request and restarted on crash. `sch attach` and
`sch web` SHALL share this same process: enabling the web UI MUST NOT require a second
process nor the restart of an already active backend. On the `claude` harness the
request SHALL be rejected by the shim with an explicit error, unchanged from existing
behavior.

**R13a.** OpenCode 2's `serve` requires HTTP basic auth (user `opencode`) on every
route. The shim SHALL pin the password through `OPENCODE_SERVER_PASSWORD` when it
starts the backend, minting it once per microVM (`secrets.token_urlsafe(32)`) and
persisting it `0600` on local disk next to `opencode.db` (never on the checkpointed
mount, never in a log), so the password survives a backend restart and is regenerated
by deleting the file. The `serve-ensure` response SHALL carry it as
`auth: {scheme: "basic", user, password}` — inside the SigV4-authenticated invoke
response only. Clients SHALL forward it out of band of the argv: `sch attach` passes
it to the bridge in the environment and the bridge hands it to the local TUI as
`OPENCODE_PASSWORD` (the variable `opencode --server <url>` reads); `sch web` embeds it
as userinfo in the printed `http://opencode:<password>@127.0.0.1:<port>` URL; the
shim's own Telegram injection uses it for its `/api/*` calls. The backend process is
NOT OpenCode's per-user "background service": TUI and headless runs in the microVM
stay separate `--standalone` processes sharing `opencode.db`.

### Lifecycle and advisory signaling

**R14.** `sch attach`, `sch acp`, and `sch web` SHALL signal to the shim the
activation and deactivation of an interactive writer on the workspace session, via
the `mark-interactive` advisory signal already employed by
`sch shell`/`sch open`/`sch stop`: `active=true` when the bridge opens, `active=false`
when it closes (including error paths). The signal SHALL be treated as best-effort by
the shim (no security guarantee), reusing unchanged the existing double-writer
warning on the headless path (capability `headless-task-execution`).

**R15.** The web view SHALL be a best-effort view with the same semantics as the
other interactive modes: closing the browser tab or the local bridge MUST NOT
interrupt an in-progress harness turn (which continues in the remote backend
process), and MUST NOT constitute a guarantee of completion nor of a final
checkpoint — completion guarantees remain exclusive to `sch task` (capability
`headless-task-execution`). After the last client closes, the microVM SHALL follow
the existing idle timeout without any keep-alive introduced by the web capability. An
open, connected tab keeps the microVM active (with the associated costs) until the
last client closes.

**R16.** Sessions created or continued from the web UI SHALL be persisted in the same
OpenCode state of the workspace, so that `sch task --continue` and the other views
(remote TUI, `sch attach`) see them without additional steps. Terminating `sch web`
SHALL close the local listener and transport channels cleanly; the browser page loses
connectivity but no orphan process remains on the laptop.

## Behavior

- `sch attach myws` — on an `opencode` workspace: starts (or reuses) `opencode serve`
  in the microVM and opens the local TUI (`opencode --server <local-bridge-url>`,
  authenticated with the backend password via `OPENCODE_PASSWORD`) connected to it;
  only API traffic crosses the network. On a `claude`/`pi` workspace: immediate error
  with alternatives, no runtime call. Version mismatch (bare `X.Y.Z` compared on both
  sides): warning and stop unless `--force` is repeated.
- `sch acp myws` — starts `opencode acp` (via the dispatcher) or
  `claude-agent-acp` in `/mnt/workspace/repo`; the editor drives
  `initialize` → `session/new` → `session/prompt` through the bridge; stdout carries
  only JSON-RPC. On `pi`: refused with alternatives (`sch shell`, `sch run`,
  `sch task`), no runtime call.
- `sch web myws` — opens the bridge, prints
  `http://opencode:<password>@127.0.0.1:<port>`, opens the default browser. With
  `--no-browser` or on browser-open failure: prints the URL and
  keeps the bridge in the foreground until the operator terminates it (Ctrl+C).
  On a `claude`/`pi` workspace: immediate error with alternatives, no runtime call.
  Against an old image: explicit "predates web access" upgrade error.
- Concurrent `sch attach` and `sch web` on the same workspace: both talk to the same
  backend process and see the same sessions and live state; no second process starts.
  If the backend crashes, the supervisor restarts it and the web UI becomes
  reachable again by reloading the page, without restarting the local bridge.
- Closing the browser tab while a turn runs: the turn continues remotely; reopening
  `sch web` within the microVM's lifetime window shows the session with the turn
  completed or in progress. A brainstorm session from the web UI is resumable by
  `sch task myws --continue "..."` afterwards.
- A headless task started while any bridge (attach/acp/web) is active is accepted,
  and its acknowledgement carries the advisory double-writer warning.
- Loss recovery (R10): each side numbers the bytes it sends and retains the
  un-acknowledged tail; a receiver that sees a frame beyond the bytes it holds
  answers `R <offset>` and the sender replays from there. The local side repeats
  the request on every ack-check tick (5 s) while the gap is open, and sends one
  speculative `R <offset>` after every physical *re*connection (never on the first
  connect) — a request for nothing is an empty no-op, and duplicates from a replay
  are discarded. Without the repetition, an outage that lost the data *and* the
  request left a window-paused sender and a waiting receiver with nothing to
  trigger recovery until the orphan timeout (observed as an intermittent failure
  of `tunnel/mirror-reconnect.test.js`, 2026-09-08).

## Invariants

**I1.** During an active `sch acp` session, stdout carries exclusively ACP JSON-RPC
frames; every diagnostic from `sch`, the bridge, or the sync goes to stderr.

**I2.** A JSON-RPC message crossing the bridge in either direction is unchanged
except for the path/fs-capability fields rewritten per
[ACP file locality](acp-file-locality.md): ids, relative order, and all other fields
are preserved.

**I3.** At most one supervised OpenCode backend process exists per microVM, shared by
`sch attach` and `sch web`, listening only on `127.0.0.1` inside the microVM.

**I4.** The byte flow delivered across a reconnection has neither gaps nor
duplications: every application byte is acknowledged at application level, and only
unacknowledged bytes are retransmitted.

**I5.** When a bridge closes (normally or on error), the remote end has no orphan
process or connection left: either it was signaled the closure, or the orphan timeout
terminated it.

**I6.** Two distinct adapters never share a WebSocket connection, and the tunnel
channel never shares a concurrency budget with the interactive shell channel.

**I7.** `sch attach`, `sch acp`, and `sch web` never contact the runtime before
validating the persisted harness against their respective availability rules.

## Cross-references

- Harness availability and alternatives: [ACP editor integration](acp-editor-integration.md),
  [Dashboard TUI](dashboard-tui.md); capability `headless-task-execution` (completion
  guarantees, double-writer warning), `harness-selection`.
- Code: [cli/sch/commands/attach.py](../../../cli/sch/commands/attach.py),
  [cli/sch/commands/acp.py](../../../cli/sch/commands/acp.py),
  [cli/sch/commands/web.py](../../../cli/sch/commands/web.py),
  [tunnel/attach.js](../../../tunnel/attach.js),
  [tunnel/web.js](../../../tunnel/web.js),
  [tunnel/tcp-bridge.js](../../../tunnel/tcp-bridge.js),
  [tunnel/transport.js](../../../tunnel/transport.js),
  [tunnel/framing.js](../../../tunnel/framing.js),
  [image/app/main.py](../../../image/app/main.py) (`serve-ensure` supervisor),
  [bin/verify-remote-ui-tunnel.sh](../../../bin/verify-remote-ui-tunnel.sh).
- [MANIFESTO](../../../MANIFESTO.md).
