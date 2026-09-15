# Run model selection

> Domain: [providers-models](../README.md) · Status: Implemented · Source: rationalized from openspec specs (2026-08-28)

## Purpose

Ephemeral model selection for a single `sch run` invocation, propagated
safely down to the harness-specific argv without modifying the workspace's
binding or persisted metadata. The value is opaque to `sch`: the harness
owns model validation; `sch` enforces only syntax. Related: provider keys
are covered in [provider API keys](provider-api-keys.md) and
[user-provider keys](user-provider-keys.md).

## Scope

In scope:
- The `--model <id>` flag on `sch run`, its ephemerality, and its absence-of-flag behavior.
- Model propagation through the `prepare-run` payload and the run-once marker consumed by the autostart.
- Harness-specific argv mapping (`opencode`, `claude`, `pi`).
- Syntactic validation client-side and in the shim.

Out of scope:
- Provider credentials and per-harness provider mapping — [provider API keys](provider-api-keys.md).
- Workspace metadata, bindings, and persisted state (explicitly untouched by this capability).

## Requirements

### Per-invocation flag

- **R1.** `sch run <workspace>` SHALL accept an optional `--model <id>` that selects the model with which the harness TUI is started for that single invocation. The value MUST be ephemeral: it is not persisted in the workspace metadata (local index or central registry) and does not affect subsequent invocations.
- **R2.** Without the flag, behavior SHALL remain identical to the pre-flag behavior: the harness starts without arguments and resolves its own default model.
- **R2a.** There SHALL be no `--variant` flag on `sch run`: the pinned opencode TUI defines no such option (only `opencode run` does), so the CLI SHALL reject it with a usage error naming the remedy (in-TUI model picker, or `sch task --variant` for headless tasks). Reasoning effort on headless tasks is covered in [headless-task-execution](../access-surfaces/headless-task-execution.md) (R8a/R8b).

### Payload and marker propagation

- **R3.** When `--model` is specified, `sch` SHALL include the value in the `prepare-run` action payload and the shim SHALL write it into the run-once marker consumed by the autostart.
- **R4.** The consume-once and TTL properties of the marker MUST remain unchanged: an expired or already-consumed marker discards the model as well, and a subsequent shell starts normally without any residual effect of the requested model.

### Harness-specific mapping

- **R5.** The autostart SHALL translate the marker's model field into the harness-specific argument: `opencode --model <id>` for harness `opencode`, `claude --model <id>` for harness `claude`, and Pi's native provider/model pair for harness `pi` — through the harness dispatcher, with the ENV bridge and readiness gating unchanged.
- **R6.** Each value MUST be passed as a discrete argv element, without shell interpolation.
- **R7.** The value is opaque to `sch`: any id accepted by the harness (e.g. `provider/model-id` for opencode, a Bedrock inference-profile ID for claude or for pi) is forwarded without interpretation, and an unrecognized model produces the harness's native error, not an `sch` error.
- **R10.** `--model` SHALL compose with `sch run --continue`: the autostart forwards both the resume selector (`--session`/`--resume`) and `--model` as discrete argv elements (Pi: `--session` plus the provider/model pair); either flag absent leaves the other's behavior unchanged.

### Syntactic validation (two lines of defense)

- **R8.** `sch` SHALL reject client-side a `--model` value that is empty or contains whitespace or control characters, with an explicit usage error before any runtime invocation.
- **R9.** The shim MUST apply the same syntactic validation as a second line of defense and reject a `prepare-run` action with a malformed value without writing the marker.

## Behavior

```console
$ sch run myws --model anthropic/claude-sonnet-4   # TUI starts on that model
$ sch run myws                                     # next run: harness default again
$ sch run myws --continue --model anthropic/claude-sonnet-4   # resume latest session on that model
```

- Run with an explicit valid id → the harness TUI starts using that model for the session.
- Run without the flag → the harness starts without additional arguments, exactly as before the flag existed; no model field appears in the workspace metadata.
- Expired marker → the shell starts without autostart and the requested model is not applied to any subsequent start.
- `prepare-run` without a model field → the marker contains no model field and the autostart runs the harness without arguments.
- Model unknown to the harness → the harness starts and shows its own native error; `sch` does not validate the id against Bedrock.
- `--model ""` or a value containing spaces → `sch` exits with a usage error without invoking the runtime; a malformed value that bypasses the client is rejected by the shim and no marker is written.
- On a runtime image that ignores the unknown `model` key, the runtime may omit the echo: for `run`, the CLI warns and continues; for a headless `task`, the task is already accepted, so the CLI warns that it runs with the default model (no cancel path).

## Invariants

- **I1.** No `--model` value is ever persisted in workspace metadata (local index or central registry); each invocation's selection dies with the invocation.
- **I2.** The model value travels only through the `prepare-run` payload → run-once marker → autostart argv chain, always as a discrete argv element, never shell-interpolated.
- **I3.** An expired, consumed, or absent marker never leaves a residual model effect on any later start.
- **I4.** `sch` performs syntactic validation only (empty, whitespace, control characters) and never semantic validation of model ids.
- **I5.** A malformed model value never reaches a run-once marker: rejected client-side, or shim-side without writing the marker.

## Cross-references

- [Provider API keys](provider-api-keys.md) — per-harness provider mappings and Bedrock defaults.
- [User-provider keys](user-provider-keys.md) — credential source and transport.
- [Local workspace sync](../sync-and-git/local-workspace-sync.md) — the run flow the TUI belongs to.
- [MANIFESTO](../../../MANIFESTO.md) — surface hierarchy and principles.
- Code: `cli/sch/commands/run.py` (`--model` parsing, payload), `cli/sch/commands/task.py` (headless path), `cli/sch/cli.py` (`validate_model_or_die`), `image/app/main.py` (`prepare-run` shim action, run-once marker, autostart argv mapping).
