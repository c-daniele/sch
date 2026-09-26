---
id: doc-10
title: >-
  2026-09-26 Upgrade harness pins: OpenCode 2.0.16, Pi 0.87.1, Claude Code
  2.1.282
type: other
created_date: '2026-09-26 03:56'
updated_date: '2026-09-26 03:56'
tags:
  - journal
---
## The problem

The runtime image pinned OpenCode 1.18.31, Pi 0.85.1 and Claude Code 2.1.272. The maintainer asked to move all three to the current releases (2.0.16, 0.87.1, 2.1.282). Two of the bumps were routine. The third was not: OpenCode 2 is a major release with a client/server split, and SCH depends on OpenCode's CLI surface, on-disk state and plugin API end to end.

Before touching any code we installed the three new binaries in an isolated prefix and probed every surface SCH relies on (findings in `.backlog/brainstorming/2026-09-25.AgentVersionUpgrade/findings.md`). The short version of what changed in OpenCode 2:

- The npm package moved from `opencode-ai` (which stops at 1.18.x) to `@opencode/cli`, and `opencode --version` prints `opencode v2.0.16` instead of a bare version.
- `opencode web` is gone: `opencode serve` hosts the web UI and the API (now under `/api/*`), and it requires HTTP basic auth on every route. The TUI client `opencode attach <url>` became `opencode --server <url>` with the password in `OPENCODE_PASSWORD`.
- The root TUI lost `--model`; `opencode run` lost `--variant` (the variant is now part of the model reference, `provider/model#variant`); `export`/`import` moved under `session`; an unknown `--agent` is a hard error instead of a warning; boolean flags such as `--auto` swallow a following `y`/`true`-like token as their value.
- Sessions live in the `session_v2` table; `auth.json` credentials are imported once into a `credential` table and the file is then ignored.
- Version 1 plugins do not load at all (a warning in the log, the harness continues silently without them), so SCH's Telegram plugin was dead on arrival.
- By default every `opencode` command discovers or starts a per-user background service; `--standalone` embeds a private server in the process instead (the 1.x behavior).

Pi 0.87.1 and Claude Code 2.1.282 kept every CLI flag SCH asserts at build time, and Pi's session-file layout (directory encoding, header version 3) is byte-identical to the version the resolver was written against.

## What changed

Two decisions shaped the work. First, a fresh installation: no compatibility with 1.x sessions or credentials, existing workspace sessions are discarded rather than migrated. Second, the process topology stays as it was: the TUI and headless tasks run as separate `--standalone` processes sharing `opencode.db`, and the shim keeps supervising one `opencode serve` on port 4096 only for `sch attach`, `sch web` and Telegram message injection. The alternative, a single shared server every entry point connects to, would have broken per-task provider-key staging (the model call would run inside a long-lived server whose environment was fixed at start) and made every surface depend on one process's health; it stays available as a future memory optimization.

Concretely:

- **Image**: the three pins, the `@opencode/cli` install (its postinstall must run, it selects the platform binary), a version assert that strips the `opencode v` prefix, and build asserts on the 2.x surface SCH depends on (`serve --hostname/--port`, `acp`, `run --session/--model/--agent/--auto/--standalone`, `session export/import`, `debug paths`). Stale version quotes in comments were corrected.
- **Shim**: the supervisor spawns `opencode serve` with a password pinned through `OPENCODE_SERVER_PASSWORD`, minted once per microVM and kept `0600` on local disk next to `opencode.db`; `serve-ensure` returns it inside the SigV4-authenticated response. The Telegram injector and every DB reader moved to the 2.x API and schema (`/api/session/{id}/prompt`, `session_v2`, `credential`). The headless argv became `run --standalone [--session] [--model p/m#variant] [--agent] --auto -- <prompt>`, with `--agent` passed only when the seeded agent file exists.
- **Autostart**: since the TUI has no `--model` flag, `sch run --model` merges the model through `OPENCODE_CONFIG_CONTENT` for that process only; the seeded `opencode.json` is never rewritten.
- **Telegram plugin**: rewritten for the 2.x plugin API (default export with `id` and `setup`; event subscription, tool hooks, and the `evaluate` permission hook for remote approval). Turn-end text is read through the typed session API instead of the streaming event shape.
- **Laptop CLI and tunnel**: `sch attach` launches `opencode --server <url>` with the password handed over in the environment (never on the argv); `sch web` prints the URL with basic-auth userinfo so the browser opens without a prompt; `sch handoff` uses `session export`/`session list` with `--standalone`; a shared version normalizer keeps the local/remote parity check meaningful.
- **Verification scripts, specs and guides** updated to the new surface: `/api/*` with auth in the tunnel verifier, `--standalone` on remote `opencode run` calls, the runtime-image version table and R4/R40, headless-task-execution R8/R8a/R8b, remote-ui-tunnel R13/R13a, session-handoff, run-model-selection, telegram-interaction.

## Outcome

All offline suites pass: 470 image/app tests, 590 CLI tests, 12 tunnel test files, `bin/verify-docs.sh`. The local image build and `image/test-local.sh` results are recorded in TASK-7. The live-AWS checks (Bedrock through the execution role on OpenCode 2, the Telegram plugin end to end, memory profile of `opencode serve` under AgentCore billing, `bin/verify-remote-ui-tunnel.sh`) are operator-side follow-ups named in the task summary.

Operators upgrading their laptop must install OpenCode 2 (`npm i -g @opencode/cli@2.0.16`): the 1.x binary cannot talk to a 2.x server, and `sch attach`'s version-parity check will say so.

## Lesson

A "version bump" of a tool a system shells out to is only a bump if the tool's surface is unchanged; the way to know is to install the new version and drive every subcommand, flag, file and API the system touches before editing a single pin. Here that probe turned a one-line change into a port, and found a bug no changelog mentions (a prompt equal to `y` disappearing into `--auto`). It also produced the evidence that made the two design decisions quick to take. Keep the probe folder and the build-time surface asserts: the next bump will need both.
