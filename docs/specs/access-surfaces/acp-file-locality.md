# ACP file locality

> Domain: [Access surfaces](../README.md) · Status: Implemented · Source: rationalized from openspec specs (2026-08-28)

## Purpose

Bridging between the local-filesystem expectations of ACP editors and the
workspace's remote repo (`/mnt/workspace/repo` in the microVM). A local mirror of
the worktree, path translation in ACP messages, and a continuous bidirectional sync
over a dedicated tunnel channel (`mode:"fs"`) cooperate transparently for both
editor and agent. The agent remains the sole authority over the remote filesystem,
and the editor sees and manipulates exclusively local paths.

## Scope

In scope:
- Per-workspace local mirror of the remote worktree and its hydration.
- Per-field path translation in ACP JSON-RPC messages, both directions.
- Neutralization of client-fs and terminal capabilities toward the agent.
- Continuous bidirectional sync over a dedicated `fs` tunnel channel, with
  conflict resolution and reconnection semantics.
- Explicit degradation when the remote shim does not support `fs` mode.

Out of scope:
- The tunnel transport, framing, and reconnection primitives
  ([Remote UI tunnel](remote-ui-tunnel.md)).
- Editor configuration and session lifecycle
  ([ACP editor integration](acp-editor-integration.md)).

## Requirements

### Local mirror

**R1.** `sch acp` SHALL maintain a local mirror of the remote worktree for each
workspace, in a predefined per-workspace directory (overridable via the `--mirror
<dir>` flag or the `SCH_ACP_MIRROR_ROOT` environment variable), creating it if
absent.

**R2.** On session open the mirror SHALL be hydrated from the remote worktree: fully
on first open (including a one-time snapshot of `.git`), and delta-only on subsequent
opens via manifest exchange (path, size, fingerprint), in both directions.

**R3.** Files over a configurable size threshold and patterns in a configurable
ignore-list SHALL be excluded from sync with a warning on stderr identifying the
path (and size); `.git` is excluded from continuous sync after the initial snapshot.

### Path translation

**R4.** The `sch acp` bridge SHALL translate absolute paths in ACP JSON-RPC messages
in both directions — local mirror root toward the editor and `/mnt/workspace/repo`
toward the agent — operating per-field on the path fields defined by the ACP v1
protocol (e.g. `session/new`/`session/load` `cwd`; `session/prompt`
`resource_link`/`resource` URIs; `session/update` and
`session/request_permission` tool-call `locations[].path`, diff and content block
paths; chunk content URIs), without blind substitutions on arbitrary strings.
Messages with unrecognized fields SHALL pass through unchanged; when an untranslated
string value looks like a remote path, the bridge SHALL emit a diagnostic log on
stderr identifying the field and value.

**R5.** Path matching SHALL accept both the as-given and the canonical (realpath)
form of each root on input, so that macOS canonicalization of the mirror root
(e.g. `/var` → `/private/var`) does not break the translation.

### Capability neutralization

**R6.** The bridge SHALL rewrite the `initialize` response forwarded to the agent so
that `clientCapabilities.fs.readTextFile` and `clientCapabilities.fs.writeTextFile`
are `false`, and the terminal capability is `false`, regardless of what the editor
offers. If the agent nevertheless sends `fs/*` or `terminal/*` requests, the bridge
MUST respond with a JSON-RPC error without forwarding them to the editor, logging the
event on stderr. All agent file reads and writes happen exclusively on the microVM's
remote filesystem.

### Continuous bidirectional sync

**R7.** For the duration of the `sch acp` session, changes to the remote worktree
produced by the agent SHALL propagate to the local mirror in near-real-time, and
local saves by the operator under the mirror SHALL propagate to the remote worktree.
Local application of events SHALL be atomic per file.

**R8.** The sync SHALL survive channel reconnections without loss or duplication of
events, relying on the bridge's framing with acknowledgement
([Remote UI tunnel](remote-ui-tunnel.md)): on reconnection the unacknowledged events
are retransmitted and the mirror converges without missing, truncated, or duplicated
files.

**R9.** In case of concurrent modification of the same file from both sides, the
event with the most recent timestamp prevails (last-writer-wins) and the bridge MUST
emit an explicit warning on stderr identifying the file and the overwritten side.

### Dedicated sync channel and degradation

**R10.** The sync SHALL travel over a dedicated tunnel channel in `fs` mode, distinct
from the exec channel that carries the ACP JSON-RPC: the two flows MUST NOT share the
same logical stream, and each has its own `tunnel_id`.

**R11.** When the remote shim does not support `fs` mode, `sch acp` SHALL degrade
explicitly: the ACP session proceeds in chat-style mode without mirror sync, with a
warning on stderr that explains the degradation and the required image version.
Message mediation (path translation, capability neutralization) remains active even
when degraded.

## Behavior

- First `sch acp myws` with no local mirror: the bridge creates the directory and
  materializes the complete remote worktree, including the initial `.git` snapshot,
  before declaring the session ready for the editor.
- Subsequent opens: only the files whose manifest differs between mirror and remote
  worktree are transferred, in both directions.
- A file in the remote worktree exceeding the size threshold is not synced; a
  warning on stderr identifies its path and size.
- `tool_call` locations under `/mnt/workspace/repo` reach the editor rewritten under
  the local mirror root, so files can be opened by path; `session/new` with
  `cwd` = mirror root reaches the agent as `/mnt/workspace/repo`.
- An unmapped field containing a string starting with `/mnt/workspace/repo` passes
  through unchanged, and the bridge logs the suspicious field and value on stderr.
- An editor declaring `fs.readTextFile: true`, `fs.writeTextFile: true`, or
  `terminal: true` results in the agent receiving all of them as `false`; a
  nonconforming `fs/write_text_file` or `terminal/*` request is answered with a
  JSON-RPC error and logged, never forwarded.
- Agent changes appear in the mirror in near-real-time; operator saves reach the
  remote worktree and are visible to the next prompt that reads them.
- If the sync channel drops while file events are in flight, reconnection
  retransmits the unacknowledged events and the mirror converges.
- If agent and operator modify the same file within an overlapping window, the most
  recent modification prevails and a warning identifies the file and the overwritten
  side.
- Against a shim that refuses `fs` mode, the session starts in chat-style mode with
  an explicit warning naming the cause and the minimum required image version.

## Invariants

**I1.** The agent never reads or writes any local (laptop) filesystem path: all its
file accesses resolve inside the microVM, and client-fs/terminal capabilities are
always `false` as seen by the agent.

**I2.** Path translation rewrites only the ACP-defined path-bearing fields; every
other field, id, and the relative order of messages are unchanged.

**I3.** ACP JSON-RPC and sync events never share the same logical stream/tunnel
channel.

**I4.** The mirror, once hydrated, converges to the remote worktree without missing,
truncated, or duplicated files across reconnections.

**I5.** Sync exclusions (size threshold, ignore-list) are always accompanied by an
explicit stderr warning; they never fail silently.

## Cross-references

- Transport, framing, reconnection, and harness availability:
  [Remote UI tunnel](remote-ui-tunnel.md).
- Editor configuration and session lifecycle:
  [ACP editor integration](acp-editor-integration.md).
- Code: [tunnel/acp-path-map.js](../../../tunnel/acp-path-map.js),
  [tunnel/mirror.js](../../../tunnel/mirror.js),
  [tunnel/sync.js](../../../tunnel/sync.js),
  [tunnel/acp.js](../../../tunnel/acp.js),
  [cli/sch/commands/acp.py](../../../cli/sch/commands/acp.py).
- [MANIFESTO](../../../MANIFESTO.md).
