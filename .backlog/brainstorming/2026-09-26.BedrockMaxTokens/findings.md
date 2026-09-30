# Bedrock maxTokens on OpenCode 2 — probe notes (TASK-9)

Date: 2026-09-26 · Binary: `@opencode/cli@2.0.18` (image pin) in an isolated prefix,
plus the local 2.0.16. Scratch only; the durable record is TASK-9 and its journal entry.

## Root cause

- `BedrockConverse.fromRequest` builds `inferenceConfig` only when the request's
  generation options carry `maxTokens` (or temperature/topP/stop); otherwise the
  field is omitted and Bedrock applies its own default output cap (4096 observed).
- `SessionModelRequest.prepare` starts the generation options from an empty
  `options: {}` and only agent/model options can fill them. Nothing derives them
  from the catalog `limit.output`, which V1 did.
- The per-model `body` override is merged into the HTTP request body, so
  `body.inferenceConfig.maxTokens` reaches Bedrock regardless.

## Offline probe

Fake Bedrock HTTP endpoint on 127.0.0.1 (logs path, SigV4 region scope and body),
run inside `unshare -rn` (loopback only, no outbound traffic), fake static AWS keys,
catalog via `OPENCODE_MODELS_PATH` (models.dev snapshot). Command:
`opencode run --standalone --model <ref> -- "say hi"`.

| Case | Config | Result |
| --- | --- | --- |
| A | V1 `provider.amazon-bedrock.options.{region,baseURL}` (today's seed shape) | request sent, no `inferenceConfig` (bug reproduced), SigV4 region eu-west-1 |
| B | A + V2 `providers.amazon-bedrock.models.<id>.body` | V1 block dropped entirely: `baseURL` lost, no request reached the endpoint (region would be lost too) |
| C | pure V2 `providers.amazon-bedrock.settings.{region,baseURL}` + `models.<id>.body` | `inferenceConfig.maxTokens` sent, region kept |
| D | C + V1 `mcp` block + `small_model` (full seed shape) | default model 64000, Opus 5.5 128000 (also with `#high`: thinking/effort merged alongside), title agent on Haiku still resolves, unlisted models send nothing, `aws-docs`/`aws-mcp` spawned, `context7` stays disabled, no config warnings |

Conclusion: V1 and V2 provider shapes must not be mixed for one provider ID
(matches the V2 migration guide: nested provider entries must stay in one format).

## AWS max output (model cards)

| Model | Max output |
| --- | --- |
| Claude Sonnet 4.6 | 64K (models.dev says 128K: wrong for Bedrock) |
| Claude Haiku 4.5 | 64K |
| Claude Opus 5 / Opus 5.5 | 128K |
| Claude Fable 5 / Fable 5.1 | 128K |

Bedrock deducts `input + max_tokens` from the TPM quota at request start
(`quotas-token-burndown.html`), so higher caps cost concurrency, not money.
