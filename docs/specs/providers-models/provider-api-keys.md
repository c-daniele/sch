# Provider API keys

> Domain: [providers-models](../README.md) · Status: Implemented · Source: rationalized from openspec specs (2026-08-28)

## Purpose

Per-invocation ephemeral staging of user-supplied provider API keys at
`/run/sch/provider-keys.env` (mode `0600`) and per-harness propagation to
the runtime harnesses (opencode, claude, pi). Bedrock through the
execution role remains the default provider for every harness; staged keys
only make additional providers selectable — or, for claude only, switch the
provider entirely. The one exception is a staged `BEDROCK_API_KEY`
(TASK-19): an Amazon Bedrock API key (bearer token) issued by a different
account, which does not add a provider but re-authenticates the existing
Bedrock provider against the key's account (model access, quotas and
billing move there; every non-Bedrock AWS call keeps the execution role).
The client-side credential source and transport are
defined in [user-provider keys](user-provider-keys.md); model selection per
invocation in [run model selection](run-model-selection.md).

## Scope

In scope:
- Ephemeral staging semantics of the credential set at `/run/sch/provider-keys.env`.
- The `SCH_*` environment contract between staging and the per-harness dispatchers.
- Per-harness mappings for opencode, claude and pi, and the claude Bedrock↔Anthropic switchover.
- Credential secrecy in staging, state, and diagnostics.

Out of scope:
- How credentials reach the invocation (`~/.sch/env`, payload transport) — [user-provider keys](user-provider-keys.md).
- `--model` selection on `sch run` — [run model selection](run-model-selection.md).
- Bedrock setup and the execution role itself.

## Requirements

### Ephemeral staging (shared with user-provider keys)

- **R1.** The shim SHALL atomically stage the complete credential set in `/run/sch/provider-keys.env` with mode `0600`, replacing the previous set in full (total-replacement semantics: a later invocation that omits a previously supplied key makes it unavailable to subsequent harness processes).
- **R2.** Credential values MUST NOT be persisted in checkpointed storage, runtime state, or diagnostics. Diagnostics MAY report credential names, never values.

### `SCH_*` contract

- **R3.** User-supplied keys SHALL reach harness processes only as inert `SCH_*` variables sourced from the staged file. Missing keys MUST remain absent (neither the `SCH_*` nor the canonical name is present). The `SCH_*` variables are the single source; canonical names are mapped only by the per-harness dispatcher.
- **R4.** A shell starting without staged credentials maps no provider keys and the session remains Bedrock-only, without writing values to storage or to the shell profile.

### opencode dispatcher

- **R5.** The opencode dispatcher SHALL map all present `SCH_*` keys onto `ANTHROPIC_API_KEY`, `OPENCODE_API_KEY`, `OPENROUTER_API_KEY`, `KILO_API_KEY` and `AWS_BEARER_TOKEN_BEDROCK` (from `SCH_BEDROCK_API_KEY`), in all interactive, headless and serve/web paths. A single `OPENCODE_API_KEY` enables both `opencode` (Zen) and `opencode-go`. The seeded Bedrock model MUST remain the default; with keys present the additional providers become selectable in `/models` without additional login.

### claude dispatcher

- **R6.** With an Anthropic credential staged, claude SHALL use the Anthropic API and remove Bedrock-only model defaults; without it, claude SHALL use Bedrock (`CLAUDE_CODE_USE_BEDROCK=1` and the Bedrock profiles remain active).
- **R7.** Bootstrap SHALL reconcile managed settings and SCH-owned approval suffixes in both directions: the claude seeding SHALL set `settings.json` `env.CLAUDE_CODE_USE_BEDROCK` to `"0"` with a staged Anthropic key and `"1"` without — including on existing and resumed workspaces — while preserving operator-chosen different values. When the key is present it SHALL additionally record, idempotently and with merge, only the 20-character suffix in `customApiKeyResponses.approved` in the global config actually read by Claude Code; without the key it MUST NOT write that config and MUST remove the SCH-owned suffix if previously added.
- **R8.** The claude dispatcher MUST NOT export `OPENCODE_API_KEY`, `OPENROUTER_API_KEY` or `KILO_API_KEY`, and MUST NOT modify `ANTHROPIC_BASE_URL` (gateway keys are not wired into Claude Code).
- **R8a.** With a staged `BEDROCK_API_KEY` and no staged Anthropic key, the claude dispatcher SHALL export `AWS_BEARER_TOKEN_BEDROCK` and keep `CLAUDE_CODE_USE_BEDROCK=1` and the Bedrock model defaults (they resolve in the key's account). When both keys are staged the Anthropic key SHALL win and the bearer token MUST NOT be exported: claude rides the Anthropic API and no unused secret reaches the process.

### pi dispatcher

- **R9.** The pi dispatcher SHALL map `SCH_ANTHROPIC_API_KEY` onto `ANTHROPIC_API_KEY`, `SCH_OPENROUTER_API_KEY` onto `OPENROUTER_API_KEY`, `SCH_OPENCODE_API_KEY` onto `OPENCODE_API_KEY`, `SCH_KILO_API_KEY` onto `KILO_API_KEY` and `SCH_BEDROCK_API_KEY` onto `AWS_BEARER_TOKEN_BEDROCK` when present, in all interactive and headless paths, with additive semantics: the corresponding providers become selectable, but the seeded default Bedrock provider and model MUST remain unchanged — no switchover, unlike the claude harness. Without keys, pi's behavior MUST be identical to before the feature.
- **R10.** The gateway providers (OpenCode Zen, Kilo Gateway) reach pi through its custom-provider `models.json` mechanism, because pi has no built-in provider consuming their keys. The image build SHALL generate the gateway catalog from models.dev (no runtime outbound call); bootstrap SHALL reconcile pi's `models.json` in both directions — merge the SCH-owned gateway blocks only for keys staged in the session, withdraw blocks a previous session registered when their key is no longer staged — while preserving operator-written blocks (per-provider ownership tracked in a sidecar) and writing only `$ENV_VAR` references, never key values. A keyless session MUST leave no `models.json` trace.

### Cross-account Bedrock (TASK-19)

- **R11.** A staged `BEDROCK_API_KEY` SHALL re-authenticate only Bedrock clients: the value reaches harness processes exclusively as `AWS_BEARER_TOKEN_BEDROCK`, and every non-Bedrock AWS call (checkpoint S3, DynamoDB, AgentCore, the aws-mcp MCP server) MUST keep authenticating with the execution role.
- **R12.** Cross-account inference is defined for account Y with model access in the SAME region as the runtime: the harnesses call the bedrock-runtime endpoint of the runtime region and need no extra configuration. Account Y outside the runtime region is out of scope for the dispatcher: the operator may only redirect individual harnesses by hand (e.g. `env.AWS_REGION` in Claude settings, `providers.amazon-bedrock.settings.region` in `opencode.json`), because a process-wide region override would apply to every harness at once instead of one (the aws-mcp operation region itself stays frozen at its seeded `--metadata AWS_REGION` value; only Bedrock clients follow the override).

### GitHub token (TASK-26)

- **R13.** A staged `GITHUB_TOKEN` SHALL reach harness processes of every harness (opencode, claude, pi) as both `GH_TOKEN` and `GITHUB_TOKEN` with the same value (`gh` consumes `GH_TOKEN` first). The mapping is purely additive: no provider switch, no model default change, on any harness.
- **R14.** The token value MUST NOT be written to any file except the tmpfs credential store (`/run/sch/git-credentials`, mode `0600`, atomic, total replacement) from which the repo-local git `credential.helper` reads it. In particular the value MUST NOT appear in `models.json`, `.mcp.json`, `hosts.yml` (`gh auth login` persistence is forbidden), checkpoints, or diagnostics — names only, as with every key (I4). The git-side lifecycle (origin binding, helper setup, withdrawal) is owned by the [git-native workflow](../sync-and-git/git-native-workflow.md), not by the dispatcher.

## Behavior

- OpenCode, OpenRouter and Kilo credentials staged → the opencode dispatcher exposes their canonical variables; Zen/Go, OpenRouter, Kilo and Anthropic become selectable without further login; the seeded Bedrock default is retained.
- No `KILO_API_KEY` staged → neither `SCH_KILO_API_KEY` nor `KILO_API_KEY` exists in any harness process.
- Headless opencode task with a staged key → the harness process receives the canonical key through the same dispatcher reading the staged file.
- Claude session with `SCH_ANTHROPIC_API_KEY` staged → Anthropic API; key absent (including on a resumed workspace whose prior credential was removed) → back to Bedrock, managed settings restored and the SCH-owned approval removed.
- Claude session with gateway keys present → they are not exported and `ANTHROPIC_BASE_URL` is untouched.
- Pi session with `SCH_ANTHROPIC_API_KEY` → Anthropic selectable, default stays Bedrock via the execution role.
- Pi session with `SCH_OPENCODE_API_KEY` / `SCH_KILO_API_KEY` → the matching gateway blocks are merged into `models.json` and the providers become selectable; the key values never appear in the file (only `$ENV_VAR` references), and removing a key withdraws the block on the next bootstrap.
- `SCH_BEDROCK_API_KEY` staged (any harness) → the harness Bedrock client authenticates with the bearer token of the key's account; the seeded Bedrock defaults are unchanged; a claude session that also stages an Anthropic key uses the Anthropic API and never sees the bearer token.
- `SCH_GITHUB_TOKEN` staged (any harness) → `GH_TOKEN` and `GITHUB_TOKEN` carry the same value in every harness process; `gh` works without login, and on git-native workspaces the repo's credential helper serves the token from tmpfs.
- A raw shell on a runtime where no SCH invocation staged credentials stays Bedrock-only.

## Invariants

- **I1.** `/run/sch/provider-keys.env` always holds mode `0600` and is written atomically (no torn reads).
- **I2.** Staging is total-replacement: the staged set always equals the current invocation's complete credential set.
- **I3.** A key absent from staging is absent from every harness process environment.
- **I4.** No provider key value ever appears in CLI output, shim output, `info`/`status`, notifications, checkpoints, or diagnostic output, in cleartext or otherwise — including inside pi's `models.json`, which carries only `$ENV_VAR` references.
- **I5.** The seeded Bedrock default provider/model is never changed by key staging alone (only the claude harness switches provider, and only on a staged Anthropic key).
- **I6.** Canonical provider variable names exist in a harness process only through the dispatcher mapping of staged `SCH_*` variables.
- **I7.** The gateway catalog for pi is generated at image build and contains no credential values; runtime reconciliation may only add or withdraw SCH-owned blocks.
- **I8.** A staged `BEDROCK_API_KEY` changes the identity Bedrock clients authenticate with, never the seeded model defaults, and never the credentials used by non-Bedrock AWS calls (TASK-19).

## Cross-references

- [User-provider keys](user-provider-keys.md) — credential source (`~/.sch/env`), invocation transport, dispatcher recovery.
- [Run model selection](run-model-selection.md) — per-invocation `--model`.
- [MANIFESTO](../../../MANIFESTO.md) — boundaries and security principles.
- Code: `image/app/main.py` (staging, `SCH_PROVIDER_KEYS_FILE`, per-harness dispatchers, tmpfs git credential store), `image/scripts/harness-wrapper.sh` (SCH_* mapping incl. `GH_TOKEN`/`GITHUB_TOKEN`), `image/scripts/gen-pi-gateway-models.py` (build-time gateway catalog), `image/scripts/init-workspace.sh` (pi `models.json` reconciliation), `image/app/test_provider_api_keys.py` (dispatcher tests), `image/app/test_pi_gateway_keys.py` (generator + reconciliation tests), `image/app/test_github_access.py` (token mapping + git reconciliation tests), `image/app/test_user_provider_keys.py` (staging tests).
