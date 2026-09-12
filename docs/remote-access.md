# Remote access: dashboard, web UI, local TUI attach and editors

The ways to look at and drive a running workspace besides `sch shell`: the offline dashboard, the browser UI, a local OpenCode TUI attached to the remote backend, and ACP editors such as Zed. Normative behavior: [`docs/specs/access-surfaces/`](specs/access-surfaces).

## Remote UI Access (`sch web` / `sch attach` / `sch acp`)

### Dashboard (`sch dashboard`)

`sch dashboard [--interval <seconds>]` opens a stdlib-only, full-screen
switchboard for the workspaces in the same local index or optional registry
used by `sch list`. It refreshes every 20 seconds by default. Periodic and
manual refreshes read persisted `task-status.json` objects and the freshness
of each workspace's `manifest.json` (HeadObject) from S3 only: they never
invoke AgentCore and never wake a microVM.

The `LIVE` column is a passive microVM liveness signal derived from that
manifest freshness: a live microVM's checkpoint loop rewrites the manifest
roughly every 60s, so `● <age>` (fresher than 3 minutes) means the workspace's
microVM is almost certainly running, `- <age>` means it is gone (idle timeout,
`sch stop`, or max lifetime), and `?` means no manifest is readable (workspace
never checkpointed, or the S3 read failed). AgentCore Runtime (Preview) has no
ListRuntimeSessions API, so this is an inference from checkpoint heartbeats,
not an authoritative platform answer: a VM younger than its first checkpoint
pass briefly shows `-`/`?`, and after a stop the last manifest keeps aging
from its final value.

| Key | Action |
| --- | --- |
| Up / Down | Select a workspace; selection remains anchored by workspace name across refreshes |
| Enter | Hand the terminal to `sch run <workspace>` |
| `S` | Hand the terminal to `sch shell <workspace>` |
| `w` | Start or reopen a detached `sch web <workspace> --no-browser` bridge |
| `s` | Toggle the full offline status detail |
| `r` | Request an immediate offline refresh |
| `q` | Exit the dashboard |

The dashboard is a switchboard, not a local multiplexer. Enter and `S` leave
the dashboard screen while the foreground child owns the terminal; detach the
remote client with `Ctrl+]` (or quit it) to return to the dashboard. A web
bridge launched with `w` runs in a new session/process group, is tracked only
in memory, and is deliberately left running when the dashboard exits. Pressing
`w` again while that tracked process is alive reopens its existing localhost
URL instead of creating another tunnel. Stop an orphaned bridge explicitly
when it is no longer needed. Web is unavailable for claude- and pi-bound
workspaces (the WEB column shows `n/a`).

The dashboard is read-only: it offers no stop, reset, or task-submission
actions. It discovers only workspaces visible in the local index/registry and
does not adopt web bridges started elsewhere. Interactive run, shell, and web
actions can wake a microVM; observation and refresh cannot.

### Manual verification checklist (Dashboard)

1. On Linux or macOS, run `sch dashboard --interval 5`; resize the terminal,
   move with Up/Down, wait for a refresh, and confirm selection stays on the
   same workspace.
2. Select an existing workspace and press Enter, then `Ctrl+]`; confirm the
   dashboard returns full-screen with a refreshed row. Repeat with `S` and the
   shell.
3. Select an opencode workspace and press `w`; confirm a localhost browser URL
   opens and the WEB indicator appears. Press `w` again and confirm the same
   URL is reused.
4. Press `q`; confirm the terminal cursor/input mode is normal and the opened
   web URL remains reachable. Stop that detached bridge explicitly afterward.
5. Select a claude (or pi) workspace; confirm web is shown unavailable and `w`
   neither starts a process nor wakes the runtime.
6. Repeat steps 1-5 in Windows Terminal, including resize and foreground
   return. A legacy console without VT support must fail with an explicit
   compatibility error rather than drawing ANSI text.

The full checklist has been manually verified on macOS/Linux and Windows
Terminal. Linux rendering/input and automated platform branches remain covered
by the repository test environment.

[`docs/specs/access-surfaces/remote-ui-tunnel.md`](specs/access-surfaces/remote-ui-tunnel.md) specifies the protocol-neutral byte bridge
between local adapters and processes in the microVM, over
`InvokeAgentRuntimeWithWebSocketStream` (a distinct AWS operation from the
interactive shell channel used by `sch shell`/`sch open`/`sch run` — no
shared concurrency budget with those commands; design D1-D3, see
`design.md` "Nota di revisione" for why this transport was chosen over the
shell channel originally planned).

```sh
./bin/sch web my-project                     # open the remote OpenCode UI in the default browser
./bin/sch web my-project --no-browser        # print the local URL and keep the bridge in the foreground
./bin/sch attach my-project                  # creates a new workspace with OpenCode, then connects the local TUI to the shared remote backend
./bin/sch attach my-project --harness opencode # explicit equivalent; --harness claude|pi is rejected
./bin/sch attach my-project --storage s3     # choose storage on first creation
./bin/sch acp my-project                     # expose the workspace's persisted-harness ACP agent over stdio
./bin/sch acp my-project --harness claude    # override harness for a brand-new workspace, same rules as every other command (--harness pi is rejected)
./bin/sch acp my-project --storage session   # explicitly use managed session storage
./bin/sch acp my-project --mirror /tmp/m     # override the local mirror dir (default ~/.config/sch/mirrors/<ws>/repo)
./bin/sch zed-config my-project              # print the Zed agent_servers snippet (see "Editor integration (Zed)")
```

### Web access (`sch web`)

`sch web <workspace> [--harness opencode] [--storage s3|session]
[--no-browser]` exposes the OpenCode UI and API through an ephemeral local URL
such as `http://127.0.0.1:49152`. It requires Node.js and the `tunnel/`
dependencies, but does not require a local `opencode` binary or a client/server
version match. `--harness` and `--storage` follow the same creation-only rules
as `sch attach`; web access is unavailable for a claude- or pi-bound workspace.
`--no-browser` suppresses automatic browser launch. If browser launch fails,
SCH still prints the URL and keeps the bridge in the foreground until Ctrl+C.

The local listener and the remote OpenCode backend both bind only to
`127.0.0.1`. Access to the remote byte bridge is authorized with the
operator's AWS credentials and SigV4; SCH intentionally does not configure an
additional OpenCode HTTP password. This is a local, credentialed tunnel, not a
network-exposed web service or a multi-device URL.

`sch web` is interactive and best-effort. Closing a tab or stopping the bridge
does not cancel a turn already running in the remote backend, but it also does
not guarantee that the turn finishes or that a terminal checkpoint is
published. An open tab normally keeps its SSE connection and tunnel alive,
which keeps the microVM awake and incurs runtime cost. For unattended work,
close the web view and hand the latest OpenCode session to the durable task
path:

```sh
./bin/sch task my-project --continue "implement the agreed changes and run tests"
```

The runtime image must include web-access support. A newer client against an
older image fails explicitly with `runtime image predates web access`; deploy a
new image version before retrying. As with any image version upgrade, first
stop/checkpoint workspaces whose state must survive the L1 session-storage
reset described under Deploy and L2 Durability.

- **`sch attach <workspace> [--harness opencode] [--storage s3|session] [--force]`**: launches the local `opencode`
  binary's `attach <url>` mode against the shared remote `opencode web`
  backend supervised by the shim inside the microVM; that mode serves the
  same API used by attach plus the browser UI. Rendering, clipboard, and
  keybinding stay on your laptop; only API traffic crosses the network. **opencode
  harness only** (fails fast, no runtime call, on a workspace bound to
  claude or pi — no client/server split exists for those harnesses). Checks that the
  local `opencode --version` matches the remote's before connecting
  (`--force` to override). Select the model interactively from the OpenCode
  TUI with `/models`. Each local TCP connection the TUI opens gets its
  own dedicated tunnel channel (soft cap `SCH_TUNNEL_MAX_CHANNELS`, default
  32 — a safety net, not a hard architectural limit, since this transport
  has no shared "10 concurrent shells" ceiling to worry about).
- **`sch acp <workspace> [--harness opencode|claude] [--storage s3|session] [--mirror <dir>]`**
  (**pi is refused**: no ACP agent exists for it in the microVM):
  spawns the workspace's ACP (Agent Client Protocol) agent in the microVM —
  native `opencode acp` for harness=opencode (goes through the same
  dispatcher as the TUI, inheriting the ENV bridge and readiness gate for
  free), or the official `@agentclientprotocol/claude-agent-acp` adapter
  for harness=claude — and mediates this command's stdin/stdout to it at
  the JSON-RPC message level: per-field path translation between the local
  mirror and the active remote worktree, client fs/terminal capability
  neutralization, and a dedicated file-sync channel keeping the mirror
  hot (sch-acp-editor-integration; see "Editor integration (Zed)").
  Intended to be spawned directly by ACP-compatible editors (Zed,
  JetBrains) as their agent command; stdout carries only the agent's
  JSON-RPC, all `sch` diagnostics go to stderr.
- Both commands require Node.js >= 18 locally (`cd tunnel && npm install`
  once).
- The underlying bridge (`tunnel/`) reconnects transparently across the
  connection-duration limit (60 min) and transient drops, using an
  application-level offset/ack/resend protocol layered on top of the
  WebSocket (mirrored in `image/app/tunnel_framing.py` on the shim side and
  `tunnel/framing.js` on the local side — the two MUST stay wire-compatible).
  The shim tracks each logical session (keyed by a client-generated
  `tunnel_id`) independently of any single physical WebSocket connection,
  so a reconnect resumes the same remote process/connection instead of
  restarting it.
- **Verified end-to-end against the live AgentCore runtime** (image v21,
  `eu-west-1`): `sch attach`'s local OpenCode TUI renders against the remote
  `opencode serve` (active remote worktree, model, git branch,
  MCP servers all shown), `sch acp` works for opencode and claude (valid ACP
  `initialize` responses), reconnection resumes across a forced physical drop,
  and the `SCH_TUNNEL_MAX_CHANNELS` cap holds. Live testing surfaced and fixed
  three transport bugs the offline tests missed (see `design.md` D3
  "Fix post-verifica live" / OQ-WS-LIVE): (1) a connection-confirmation
  deadlock — the client waited for a server greeting that HTTP/ACP servers
  never send, so confirmation is now the successful WebSocket upgrade itself;
  (2) large responses stalled — framing now sends one line per WebSocket
  message with send-side flow control; (3) the root cause of a 1006
  close/reconnect storm on large transfers was the **250 frames/sec
  per-connection** WebSocket limit — the shim now paces sends under it (with
  larger ~60KB frames to minimise frame count). The frame *size* limit is
  64 KB per the quota table, not 32 KB. Re-verified against image v22
  (post `sch-acp-editor-integration` deploy): full pass, zero regressions
  (`bin/verify-remote-ui-tunnel.sh` 8/8).
- **OQ-FILE-LOCALITY — RESOLVED** (by `sch-acp-editor-integration`, image >=
  v22): ACP editors assume the agent's repo is on the editor's own local
  filesystem, while with `sch acp` it lives in the microVM at
  the backend's active worktree. `sch acp` now bridges the two with a per-workspace
  **local mirror** kept in sync over a dedicated tunnel channel plus
  **per-field path translation** inside the ACP messages — see "Editor
  integration (Zed)" below. On an older runtime image (<= v21, no `fs`
  tunnel mode) the session degrades explicitly to the previous chat-style
  behavior with a stderr warning. `sch attach` was never affected (its local
  OpenCode TUI talks to the shared remote OpenCode backend over API only).

## Editor integration (Zed)

`sch acp` exposes a workspace as an ACP agent server that editors spawn
directly. Zed is the verified reference editor (change
`sch-acp-editor-integration`; the contract is standard ACP v1, so other
ACP editors should work but are not verified). What you get: prompting the
remote agent from Zed's Agent Panel, **jump-to-file and inline diffs on
real local paths**, `@file` mentions resolved on the remote worktree, and
a two-way file sync between a local mirror of the repo and the microVM.

### Setup

```sh
./bin/sch zed-config my-project           # prints the agent_servers snippet (stdout = JSON only)
```

1. Merge the printed snippet into Zed's `settings.json` (command palette:
   `zed: open settings file`). If you already have an `"agent_servers"`
   object, add just the inner `"SCH my-project"` entry.
2. Open the workspace **mirror directory** as your Zed project:
   `~/.config/sch/mirrors/my-project/repo`. `zed-config` creates it for
   you (empty); it fills up automatically when the first session hydrates
   it (override the root with `SCH_ACP_MIRROR_ROOT` or per-run with
   `sch acp my-project --mirror <dir>`).
3. In Zed's Agent Panel, pick **SCH my-project** as the external agent and
   send a prompt: the remote workspace starts and the mirror syncs. The
   workspace harness (opencode|claude|pi) is resolved at runtime — the
   snippet never encodes it.

### How it works

- Zed spawns `sch acp <ws>`; stdout carries only ACP JSON-RPC, every
  diagnostic goes to stderr (visible in Zed's log: `zed: open log`).
- The bridge opens **two** tunnel channels to the microVM: `exec` (the
  agent's JSON-RPC) and `fs` (file sync, image >= v22). On the first
  session the mirror is fully hydrated from the remote worktree (including
  a one-time `.git` snapshot so Zed shows branch/status); later sessions
  transfer only deltas via manifest diff.
- Paths in the ACP messages are translated per-field in both directions
  (`session/new {cwd}`, tool_call `locations`, diff paths, `resource`/
  `resource_link` content blocks): the editor only ever sees mirror paths,
  the agent only ever sees the active remote worktree.
- The agent's client-side `fs`/`terminal` capabilities are **neutralized**:
  every read/write/command runs in the microVM (single source of truth);
  any stray `fs/*`/`terminal/*` request is rejected by the bridge with a
  JSON-RPC error and logged.
- Files the agent creates/edits appear in the mirror in near-real-time;
  files you **save** under the mirror reach the remote worktree the same
  way (watcher + sync). Conflicts on the same file are last-writer-wins
  with an explicit `CONFLICT` warning on stderr.

### Limits

- **Unsaved buffers are invisible to the agent** — save the file to share
  it (client-fs write-through is future work, OQ-WRITE-THROUGH).
- **`.git` is a static snapshot** taken at first hydration; it is excluded
  from live sync afterwards (branch switches/commits made remotely show up
  in Zed only after deleting the mirror and re-hydrating). This applies to
  ACP mirrors; `sch run`/`sch shell` source sync excludes `.git` by default.
- **Per-file size threshold**: files over `SCH_MIRROR_MAX_FILE_MB`
  (default 10) are excluded from sync with a warning; the default ignore list
  excludes Git metadata, dependencies, virtual environments, caches, build
  output, `.codebase-memory`, and `dummy_data`. Override it with
  `SCH_MIRROR_IGNORE` (comma-separated names/prefixes; `.git` and
  `node_modules`-style segment matches supported).
- **First hydration takes time** on big repos (~9-12 MB/s through the
  paced channel; the session gates the editor's `initialize` until the
  mirror is ready). Later sessions are delta-only.
- Hydration never deletes files on either side (deletions propagate only
  live, within a session) — stale union possible after offline deletions.
- One editor session per workspace at a time is the supported shape
  (OQ-MULTI-EDITOR is future work); the dual-writer advisory applies.

### Troubleshooting

- **Zed shows no response / spawn fails**: check Zed's log (`zed: open
  log`) — `sch` writes every error to stderr. Typical causes: `node`/`aws`
  not resolvable from the login-shell-less editor environment (fix with
  `SCH_NODE_BIN`/`SCH_AWS_BIN` absolute paths), missing `cd tunnel && npm
  install`.
- **"file-locality sync unavailable" warning**: the runtime image is older
  than v22 (no `fs` tunnel mode) — the session still works chat-style;
  redeploy the image to get the mirror.
- **Mirror looks stale**: the fs channel reconnects transparently, but if
  sync stopped (warning on stderr) restart the session; worst case delete
  the mirror directory and let it re-hydrate.
- Scripted E2E: `./bin/verify-acp-editor.sh` drives the full contract
  (both harnesses) against the live runtime without the GUI.

### Manual verification checklist (Zed, macOS)

1. `sch zed-config <ws>` → merge snippet → open the mirror as project.
2. Prompt: "create a file X with content Y" → X appears in the project
   tree; open it and check the content.
3. Click a file path in a tool call in the Agent Panel → Zed jumps to the
   local file (path translation).
4. Ask for an edit to an open file → inline diff renders against the
   on-disk mirror copy.
5. Mention a project file with `@file` in a prompt → the agent answers
   from the remote copy's real content.
6. Edit + save a file locally, then ask the agent to read it → it sees
   your saved content. Repeat everything on both an opencode-bound and a
   claude-bound workspace.
