# OpenCode 2.0.16 / Pi 0.87.1 / Claude Code 2.1.282 — empirical probe findings

Date: 2026-09-25 · Task: TASK-7 · Probe env: macOS arm64, isolated prefix + scratch HOME
(`$TMPDIR/opencode/agent-upgrade-probe`). CLI surface is platform-independent; AWS-side
behavior (Bedrock via execution role, AgentCore memory) still needs a live microVM re-check.

## Verdict per harness

| Harness | Bump | Risk | Notes |
| --- | --- | --- | --- |
| Pi 0.85.1 → 0.87.1 | safe | low | All 7 asserted CLI flags present; `engines.node>=22.19.0` unchanged; session layout (`--<cwd-encoded>--` dir, `version: 3` JSONL header, `getDefaultSessionDirPath`) byte-identical to 0.84.2 → resolver keeps working. |
| Claude 2.1.272 → 2.1.282 | safe | low | `--agent`, `--dangerously-skip-permissions`, `--print`, `--continue`, `--resume`, `--append-system-prompt`, `--permission-mode` all present. Patch bump. |
| OpenCode 1.18.31 → 2.0.16 | **porting project** | high | New npm package, renamed/removed subcommands, authenticated server, new plugin API, DB schema rename, auth.json migrated into DB. Details below. |

## OpenCode 2.0.16 — verified facts

### Packaging / install
- npm package renamed: `opencode-ai` (1.x, latest 1.18.32) → **`@opencode/cli`** (2.x, latest 2.0.16).
  Bins: `opencode` AND `opencode2` (same binary).
- Install needs the **postinstall** script (`postinstall.mjs` copies the platform binary from
  `optionalDependencies` — linux x64/arm64, glibc+musl, baseline/avx2 picked via /proc/cpuinfo).
  npm ≥ 11.19 blocks it by default (`--allow-scripts=@opencode/cli`); npm 10.x (Node 22's npm,
  used in the image) runs it by default. Build assert catches breakage either way.
- `opencode --version` prints `opencode v2.0.16` (V1 printed bare `1.18.31`) → Dockerfile equality
  assert and every `--version` parser must strip the `opencode v` prefix.

### CLI surface changes SCH depends on
| V1 (pinned 1.18.31) | V2 (2.0.16) |
| --- | --- |
| `opencode web --hostname --port` | **gone** → `opencode serve --hostname --port` (serves web UI on `/` + API under `/api/*`) |
| `opencode serve` (headless API) | `opencode serve` (same name, now API+web, **authenticated**) |
| `opencode attach <url>` | **gone** → `opencode --server <url>` (+ `OPENCODE_PASSWORD`) |
| `opencode session list --format json` | `opencode session list --format json` — unchanged shape (`id`, `updated`, `directory`, + new `projectId`) |
| `opencode export <id>` / `opencode import <file>` | `opencode session export [--sanitize] <id>` / `opencode session import [--directory] <file>` — export JSON still `{info:{id...}, messages}`; import round-trip OK ("Session already exists" on dup) |
| `opencode db path` | `opencode debug paths db` |
| `opencode run --model X --variant V` | `--variant` **gone** → `--model provider/model#variant` |
| root TUI `opencode --model X` | **gone** → seed config or `OPENCODE_CONFIG_CONTENT='{"model":"p/m#v"}'` env merge (verified working) |
| root TUI `--session/--continue` | unchanged; `--prompt` and `--auto` added at root |
| `run --agent <unknown>` warns + falls back | **hard error**: `Agent not found: "does-not-exist"` → SCH must validate agent existence before passing `--agent` |
| `run --auto <prompt>` | `--auto` still boolean, BUT boolean flags consume a following boolean-literal token (`y`, `n`, `true`, `false`...) as their value → a prompt equal to such a literal is swallowed ("You must provide a message"). Robust form: `run --auto -- <prompt>` (verified). |

### Server (`opencode serve`)
- Prints `server password <random>` at startup; every `/api/*` route returns 401 without credentials.
- Auth: HTTP **basic auth** `opencode:<password>` (Bearer and custom headers rejected).
- `OPENCODE_SERVER_PASSWORD=<pw>` pins the serve password; `OPENCODE_PASSWORD=<pw>` authenticates
  CLI clients using `--server <url>` (verified with `run --server`).
- Web UI served at `/` (200 text/html) — replaces V1 `opencode web`.
- API moved from `/session...` to `/api/session...`. Relevant: `GET /api/session`,
  `POST /api/session/{id}/prompt`, `POST /api/session/{id}/permission/{requestID}/reply`,
  `GET /api/event` (SSE), `GET /api/info`. Old `prompt_async`/`message` endpoints gone.
- `serve` is NOT the "background service" (`service status` stays `stopped`) — good: SCH can keep
  its own supervisor without fighting the service registration.
- Multi-process model still works: `serve` + concurrent `run --standalone` against the same
  `OPENCODE_DB` is fine (WAL), and serve sees sessions written by the standalone process.

### State / DB
- `OPENCODE_DB` env still honored (verified via `debug paths db`). XDG dirs unchanged.
- Schema: V1 `session` table → **`session_v2`**. Columns SCH reads survive: `id`, `directory`,
  `time_updated`, `model` (JSON `{id, providerID, variant}` — variant now inline, incl. the
  `"default"` sentinel). `SELECT ... FROM session` breaks; `session_v2` works.
- **In-place auto-migration**: first V2 start on a V1 DB migrates rows into `session_v2`
  (verified: V1 session created by 1.18.31 appears in `session_v2` with same id/model),
  old tables kept. `/api/experimental/migration/v1` reports `{"status":"completed"}`.
- **auth.json is absorbed into the DB**: V1 `$XDG_DATA_HOME/opencode/auth.json` is migrated into
  the new `credential` table (`integration_id`, `value` JSON `{"type":"key","key":...}`); the file
  is left on disk but new logins write only the DB. → `_opencode_available_providers()` must read
  the `credential` table (fallback: auth.json for pre-migration state); durability of logins now
  rides on the existing opencode.db backup loop, and DB restore MUST precede first V2 start
  (else a stale auth.json could re-migrate).
- WAL mode confirmed (`-shm`/`-wal` files) → existing sqlite3-backup-API loop stays valid.

### Config / templates / plugin
- Seeded V1-shaped `opencode.json` (model, `small_model`, `default_agent`,
  `provider.amazon-bedrock.options.region`, `mcp.*.enabled`) loads **unchanged, no warnings**
  (official migration guide: V1 config remains supported).
- V1 agent files under `agent/` (frontmatter `mode`, `permission`) still discovered and honored
  (verified: `default_agent: remote-interactive` used by `run`).
- **V1 plugins do NOT run**: `sch-telegram.js` fails with
  `PluginModule.LoadError: Plugin must export a default definition with an id and an effect or
  setup function` — WARN only, harness continues silently without Telegram supervision.
  → full port to the V2 plugin API required (default export `{id, setup/effect}`, new hook names,
  new client; see https://opencode.ai/v2/docs/build/plugins + plugin migrate-v1 guide).
- TUI prefs move to global `cli.json` under `$XDG_CONFIG_HOME/opencode/` → on the mount,
  checkpointed automatically. No action.

### ACP
- `opencode acp` exists; protocol v1 handshake verified (initialize OK, `agentInfo 2.0.16`,
  `loadSession: true`). `session/new` errored in the probe env (no usable provider) — needs
  re-verification with fixtures; `tunnel/acp-path-map` fixtures were captured from 1.18.3 and
  must be re-captured/re-validated against 2.0.16.

## Local verification evidence (2026-09-26)

- `docker build --platform linux/arm64 --build-arg RUNTIME_BASE=corporate-ca -t sch-runtime:dev image/` → rc=0.
  Step 28 (`@opencode/cli@2.0.16` install + prefix-stripped version equality) and Step 29 (2.x surface
  asserts: `serve --hostname/--port`, `acp`, `run --session/--model/--agent/--auto/--standalone`,
  `session export --sanitize`, `session import --directory`, `debug paths`) both passed; `claude --version`
  → `2.1.282 (Claude Code)`; pi asserts passed. (The first attempt failed at the base `dnf` layer on the
  Netskope TLS interception of this laptop — unrelated to the change; `image/certs/*.pem` + the existing
  `corporate-ca` stage fixed it, exactly as `docs/getting-started.md` documents.)
- `image/test-local.sh sch-runtime:dev` → **138 passed, 0 failed**, including
  `pinned=2.0.16 installed=2.0.16`, `opencode debug paths db honors OPENCODE_DB`, `pi pinned=0.87.1
  installed=0.87.1`, and the seeding/idempotency suite with the seeded `sch-telegram.js`.
- Ported plugin in the real image: `opencode run --standalone --model … -- "plugin probe"` through the
  dispatcher logs `loading plugin id=…/plugin/sch-telegram.js` with **no** `failed to load plugin` WARN
  (the V1 file failed with `Plugin must export a default definition…`). Only the model call failed (fake key).
- Offline suites: image/app 470 OK, cli 590 OK, tunnel 12/12 OK, `bin/verify-docs.sh` PASSED.

## Not verifiable off-AWS (live follow-ups)
- Bedrock via AgentCore execution role (default credential chain) on V2.
- Peak-memory behavior of `opencode serve` + standalone processes under AgentCore billing.
- Telegram plugin port end-to-end (permission relay, idle notify, busy keep-alive).
- models.dev catalog fetch offline behavior (kv table cache observed locally).

## SCH change surface (if V2 goes ahead)
1. `image/Dockerfile` — pin + package rename + `--version` assert rewrite + floor comment rewrite
   + `serve/acp --help` asserts (still valid) + stale 2.1.210/1.18 comments.
2. `image/app/main.py` — `_opencode_version` parse; `session`→`session_v2` queries (R8a,
   `_resolve_latest_opencode_session`, import recency bump); `_opencode_available_providers`
   (credential table); serve supervisor argv (`web`→`serve`) + password provisioning +
   `_opencode_api` basic auth + `/api/*` endpoints; `opencode import`→`session import`;
   headless argv `--variant`→`#variant` compose + `--auto --` guard + agent pre-validation.
3. `image/scripts/sch-run-profile.sh` — root `--model` gone → `OPENCODE_CONFIG_CONTENT` merge.
4. `image/opencode-templates/plugin/sch-telegram.js` — V2 plugin API port.
5. `cli/sch` — `handoff.py`/`task.py` (`export`→`session export`, `session list` OK, version
   parse), `attach.py` (version parity parse; tunnel client argv), `run.py` (--variant message).
6. `tunnel/attach.js` — `attach <url>`→`--server <url>` + `OPENCODE_PASSWORD` pass-through;
   `web.js` (password surface for browser basic-auth); `acp-path-map` re-validation.
7. `image/test-local.sh` — version equality, `db path`→`debug paths db`, plugin checks.
8. Docs/specs — runtime-image version table, harnesses.md, headless-task-execution,
   remote-ui-tunnel, session-handoff, run-model-selection (R2a note), stale quotes.
