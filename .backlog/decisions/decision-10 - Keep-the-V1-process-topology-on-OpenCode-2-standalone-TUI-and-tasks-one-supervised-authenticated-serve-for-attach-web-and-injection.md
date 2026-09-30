---
id: decision-10
title: >-
  Keep the V1 process topology on OpenCode 2: standalone TUI and tasks, one
  supervised authenticated serve for attach, web and injection
date: '2026-09-26 03:48'
status: accepted
---
## Context

OpenCode 2 (TASK-7) splits client and server: by default every `opencode` command discovers or starts one per-user background server that owns sessions, configuration, permissions, tool execution and the model loop, and TUIs and `opencode run` become thin HTTP clients of it (`--server <url>`, basic auth). `--standalone` embeds a private server in the process instead, which is how OpenCode 1 always behaved.

SCH's microVM ran three kinds of OpenCode 1 processes, coordinated only through the shared SQLite `opencode.db` (WAL): the interactive TUI (`sch run`/`sch open`), headless tasks (`opencode run`, one per `sch task`), and one supervised `opencode web` for `sch attach`, `sch web` and Telegram message injection. Porting to OpenCode 2 forced a choice between keeping that shape and adopting the shared-server model.

Two facts decided it. First, SCH stages provider API keys per child process (decision-7): each `sch task` gets its keys in that process's environment. In a shared-server topology the model call runs inside the long-lived server, whose environment was fixed when the supervisor started it, so per-task staged keys would silently stop reaching the model runtime. Second, one server for every surface makes a server crash take down the TUI, the running task and the tunnel at once, where today they fail independently. The probe also confirmed that OpenCode 2 tolerates the old shape: `serve` plus concurrent `--standalone` processes against the same `OPENCODE_DB` work, and `serve` sees sessions the standalone processes write.

## Decision

Keep the V1 topology on OpenCode 2:

- The TUI and every headless task run `--standalone` (private embedded server, no per-user background service is ever started in the microVM), sharing `opencode.db` on local disk as before.
- The shim keeps supervising exactly one `opencode serve` on `127.0.0.1:4096`, used only as the backend of `sch attach`, `sch web` and Telegram injection. Its basic-auth password is pinned through `OPENCODE_SERVER_PASSWORD`, minted once per microVM, kept `0600` on local disk next to `opencode.db` (never checkpointed, never logged), and returned to clients only inside the SigV4-authenticated `serve-ensure` response. Clients receive it out of band of the argv (`sch attach` → bridge environment → `OPENCODE_PASSWORD` for `opencode --server`; `sch web` → URL userinfo).
- Fresh installation: the port reads OpenCode 2's `session_v2` and `credential` tables only; 1.x sessions and `auth.json` are not migrated or read.

## Consequences

- Per-task provider-key staging and failure isolation keep working unchanged; the memory profile is the same as today (each process a full runtime).
- Every SCH-spawned `opencode` invocation must carry `--standalone` (or `--server`); a bare invocation would spawn an unsupervised background service. The build asserts `run --standalone` exists.
- The shared-server topology remains available later as a deliberate memory optimization, but it requires redesigning provider-key transport around the server's credential API first.
- Existing workspaces keep their old sessions on disk but SCH does not resume them; operators start fresh after the upgrade (maintainer's explicit choice for TASK-7).
