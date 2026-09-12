# ACP editor integration

> Domain: [Access surfaces](../README.md) · Status: Implemented · Source: rationalized from openspec specs (2026-08-28)

## Purpose

Integration contract between a real ACP editor and a remote SCH workspace driven via
`sch acp`. Zed is the reference editor: it is configured (through a generated
snippet) to spawn `sch acp <workspace>` as a custom agent server, and conducts
complete ACP sessions against the agent running in the microVM, for both harnesses.
File locality (mirror, path translation, fs-capability neutralization) is specified
by [ACP file locality](acp-file-locality.md); the tunnel transport by
[Remote UI tunnel](remote-ui-tunnel.md).

## Scope

In scope:
- `sch zed-config` snippet generation for Zed's `settings.json`.
- Robustness of `sch acp` when spawned by an editor outside a login shell.
- End-to-end ACP session lifecycle (`initialize` → `session/new` → `session/prompt`)
  for both harnesses.

Out of scope:
- Transport, framing, and reconnection ([Remote UI tunnel](remote-ui-tunnel.md)).
- Path translation, local mirror, sync, and fs/terminal capability neutralization
  ([ACP file locality](acp-file-locality.md)).

## Requirements

### Configuration generation

**R1.** `sch` SHALL expose `sch zed-config <workspace>`, which prints to stdout
exclusively a valid JSON snippet for the `agent_servers` section of Zed's
`settings.json`, containing an entry for the workspace with `"type": "custom"`,
`command` resolved to the absolute path of the `bin/sch` launcher, and `args` equal
to `["acp", "<workspace>"]`; any usage instructions or diagnostics MUST be written to
stderr. The workspace name SHALL be validated with the same rules as the other `sch`
commands. On Windows, where `.ps1` scripts are not directly executable, the snippet
MAY target the PowerShell host explicitly (`pwsh -NoLogo -NoProfile -File bin/sch.ps1
acp <workspace>`).

**R2.** The snippet MUST NOT encode the harness: resolution of the persisted harness
happens at runtime in `sch acp`. The command SHALL pre-create the workspace's local
mirror directory, because the editor cannot open a folder that does not exist yet.

### Editor-spawn robustness

**R3.** `sch acp` MUST work when spawned by an editor outside a login shell, with a
minimal PATH and no TTY: it MUST NOT present interactive prompts, SHALL resolve its
local dependencies (`node`, AWS CLI) without assuming an enriched environment, and on
an unresolvable dependency MUST fail immediately with an explicit message on stderr
and a non-zero exit code, leaving stdout empty.

### End-to-end editor session

**R4.** An ACP session conducted by an editor (or by a client that replicates its
contract) through `sch acp` SHALL complete the `initialize` → `session/new` →
`session/prompt` cycle for both harnesses: the `initialize` response comes from the
real agent in the microVM (`protocolVersion` 1), `session/new` accepts the `cwd` of
the project open in the editor (translated per
[ACP file locality](acp-file-locality.md)), and `session/prompt` produces streaming
`session/update` notifications until the end of the turn. Content blocks of type
`resource`/`resource_link` in prompts that reference project files SHALL be resolved
by the agent against the remote worktree.

**R5.** `sch acp` SHALL refuse harnesses with no ACP agent (today: `pi`) with an
explicit error and no runtime call, per the availability rules of
[Remote UI tunnel](remote-ui-tunnel.md).

## Behavior

- `sch zed-config myws` prints the JSON snippet to stdout and the insertion
  instructions to stderr; the operator pastes the snippet into Zed's
  `settings.json`. The snippet is identical regardless of the workspace's persisted
  harness; `sch acp` resolves the harness at startup.
- Zed spawns `sch acp myws` with a minimal environment: the session starts normally
  and stdout carries exclusively JSON-RPC. If `node` is missing, the command exits
  immediately, non-zero, with an explicit error on stderr naming the missing
  dependency and how to remedy it, and nothing on stdout.
- Complete session on the `opencode` harness: each phase receives a valid response
  from the remote `opencode acp` agent and the turn's updates arrive streamed as
  `session/update` notifications. On the `claude` harness the remote
  `claude-agent-acp` adapter responds with the same protocol guarantees.
- A prompt including a `resource_link` pointing to a file under the local project
  root results in the agent receiving the path translated under
  `/mnt/workspace/repo` and resolving the file content from the remote worktree.

## Invariants

**I1.** During editor-driven sessions, stdout carries exclusively ACP JSON-RPC
frames; all diagnostics go to stderr.

**I2.** The generated `zed-config` snippet is harness-independent and depends only on
the workspace name and the launcher's absolute path.

**I3.** A harness without an ACP agent is refused before any dependency resolution,
mirror creation, or runtime call.

## Cross-references

- Path translation and fs-capability rules: [ACP file locality](acp-file-locality.md).
- Transport and harness availability: [Remote UI tunnel](remote-ui-tunnel.md).
- Code: [cli/sch/commands/zed_config.py](../../../cli/sch/commands/zed_config.py),
  [cli/sch/commands/acp.py](../../../cli/sch/commands/acp.py),
  [tunnel/acp.js](../../../tunnel/acp.js).
- [MANIFESTO](../../../MANIFESTO.md).
