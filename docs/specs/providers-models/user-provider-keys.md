# User-provider keys

> Domain: [providers-models](../README.md) · Status: Implemented · Source: rationalized from openspec specs (2026-08-28)

## Purpose

Per-user provider credential source (`~/.sch/env`), invocation transport,
and dispatcher recovery rules on the runtime side. This capability covers
how credentials travel from the operator's laptop into the runtime
invocation; the ephemeral staging and per-harness propagation are defined
in [provider API keys](provider-api-keys.md).

## Scope

In scope:
- Reading provider credentials from `~/.sch/env` and the allowlist of forwardable keys.
- Permission checks on `~/.sch/env`.
- Complete credential coverage on every runtime invocation payload.
- Dispatcher recovery of staged values (and the prohibition on recovering them from PID 1's environment).
- Client-side secrecy on failures.

Out of scope:
- Ephemeral staging semantics and per-harness dispatch mappings — [provider API keys](provider-api-keys.md).
- Model selection per invocation — [run model selection](run-model-selection.md).

## Requirements

### User credential source

- **R1.** The CLI SHALL read provider credentials from `~/.sch/env` for every command that invokes the runtime, and MUST forward only `ANTHROPIC_API_KEY`, `OPENCODE_API_KEY`, `OPENROUTER_API_KEY`, `KILO_API_KEY`, `BEDROCK_API_KEY` and `GITHUB_TOKEN` (allowlist). Missing, empty, or malformed input MUST NOT fail the command; unrelated settings in the file are ignored. `GITHUB_TOKEN` (TASK-26) is the opt-in forge credential for remote `git`/`gh` access, not a model provider; its runtime semantics live in the [git-native workflow](../sync-and-git/git-native-workflow.md) and its harness mapping in [provider API keys](provider-api-keys.md).
- **R2.** If `~/.sch/env` is readable by group or others, the CLI MUST withhold every credential, print a `chmod 600` remedy, and continue the command.
- **R7.** `sch info` SHALL report the configured key NAMES (never values) and the expected `~/.sch/env` path, and SHALL name that path when no key is configured — a misplaced or empty file must be observable, not silent.

### Invocation transport

- **R3.** Every JSON payload sent through the verified or best-effort runtime invocation paths SHALL carry the current complete credential set when configured. Warmup and subsequent action payloads SHALL carry the same credential set.

### Ephemeral staging and dispatcher recovery

- **R4.** The runtime SHALL stage the received credential set per the ephemeral staging requirement of [provider API keys](provider-api-keys.md) (atomic, `0600`, total replacement, never persisted).
- **R5.** The dispatcher SHALL read staged `SCH_*` values without evaluating file contents and apply the existing per-harness mappings (see [provider API keys](provider-api-keys.md)). It MUST NOT recover provider credentials from PID 1's environment.

### Secrecy

- **R6.** Credential values MUST NOT appear in CLI output, logs, diagnostics, or raw invocation errors — client-side and runtime-side alike (see the secrecy requirements of [provider API keys](provider-api-keys.md)).

## Behavior

- `~/.sch/env` contains an OpenRouter key plus unrelated settings → only the OpenRouter key reaches the invocation payload.
- `~/.sch/env` contains a `BEDROCK_API_KEY` (Amazon Bedrock API key issued by a different account, TASK-19) → it travels like any provider key and the harness Bedrock clients authenticate against the key's account, while every non-Bedrock AWS call keeps the execution role.
- A command sends a warmup followed by an action → both payloads carry the same credential set.
- A later invocation omits a previously supplied key → the staged set is replaced in full and subsequent harness processes cannot receive that key.
- `~/.sch/env` has mode `0644` → the runtime invocation proceeds without any provider credentials and the operator sees the `chmod 600` remedy.
- No SCH invocation has staged credentials → a raw shell remains Bedrock-only.
- An invocation carrying credentials fails → the reported error contains no credential value.
- The credential file sits at `~/.config/sch/env` instead of `~/.sch/env` → it is ignored (no warning, no failure) and `sch info` reports `none` naming the expected path.

Note the two distinct per-user trees: `~/.sch/env` is the credential source; `~/.config/sch/` is `sch`'s own config (workspaces, caches). Only the former is ever read for provider keys.

## Invariants

- **I1.** Only the allowlisted keys ever leave the laptop; no other line of `~/.sch/env` is forwarded.
- **I2.** Credentials with unsafe file permissions are never sent, and the command still completes its non-credential work.
- **I3.** Every runtime invocation payload carries the same credential set within one command execution.
- **I4.** A credential absent from the staged file is absent from every harness process, and no dispatcher falls back to PID 1's environment.
- **I5.** No credential value appears in any error, log, or diagnostic, on either side of the tunnel.

## Cross-references

- [Provider API keys](provider-api-keys.md) — staging at `/run/sch/provider-keys.env`, `SCH_*` contract, per-harness mappings.
- [Run model selection](run-model-selection.md) — per-invocation `--model`.
- [MANIFESTO](../../../MANIFESTO.md) — security boundaries (credentials never leave the operator's control).
- Code: `cli/sch/cli.py` and `cli/sch/runtime.py` (payload building on invocation paths), `cli/sch/commands/info.py` (`sch info` key-name reporting, R7), `image/app/main.py` (staging, `SCH_PROVIDER_KEYS_FILE`), `image/app/test_user_provider_keys.py` (staging/recovery tests), `image/app/test_provider_api_keys.py` (dispatcher tests).
