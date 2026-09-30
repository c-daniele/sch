---
id: decision-12
title: >-
  Seed an explicit Bedrock output cap per Claude model in the native OpenCode 2
  provider shape
date: '2026-09-26 18:09'
status: accepted
---
## Context

OpenCode 2 (TASK-7 pinned 2.0.18) sends Bedrock no output-token cap (`inferenceConfig.maxTokens`) unless configuration sets one. The Converse request builder only includes the field when the request's generation options carry a value, and the session layer never fills it from the model catalog, which OpenCode 1.x did. Bedrock then applies its own default, 4096 tokens for Claude models, and long turns in SCH sessions stall (TASK-9). The only configuration knob that reliably reaches the request is a per-model `body` override, which OpenCode merges into the HTTP request body.

Two constraints shaped the fix. First, the image seeded the provider block in the OpenCode 1.x layout (`provider.amazon-bedrock.options.region`). A probe against 2.0.18 showed that adding a native V2 `providers.amazon-bedrock` entry next to it makes OpenCode drop the V1 entry entirely, region included; the V2 migration guide also says nested provider entries must stay in one format. Second, the cap has a cost on both sides: Bedrock rejects a value above the model's maximum output, and it reserves input plus `maxTokens` from the tokens-per-minute quota when each request starts, so maximizing every cap reduces concurrency.

## Decision

- The seeded `opencode.json` declares the Bedrock provider in the native OpenCode 2 shape only: `providers.amazon-bedrock` with the region under `settings.region`, and no V1 `provider` block.
- Its `models` map sets `body.inferenceConfig.maxTokens` explicitly for the seeded default model and for a maintainer-chosen list of Claude inference profiles (currently the global and EU Fable/Opus profiles; list and values in runtime-image R20). Each value is bounded by the model's maximum output on its Amazon Bedrock model card, not by the models.dev catalog (which overstates Sonnet 4.6).
- The change applies to fresh workspaces only. The never-overwrite seeding contract (runtime-image R19/R24) is kept; older workspaces are fixed by hand or re-seeded, as documented in `docs/mcp-and-bedrock.md`.

## Consequences

- Sessions on the listed models get their configured cap; a model outside the list still gets the 4096 default until it is added to the seed or the operator's config.
- The list needs maintenance: adding a new default model or a new popular profile means adding its entry, with a value checked against its Bedrock model card. `image/app/test_opencode_workspace_seed.py` fails when a seeded model has no known maximum or exceeds it.
- Any future seeded provider settings must use the V2 layout, never the V1 one, so the two layouts cannot mix for one provider.
- If a later OpenCode release derives the Bedrock cap from the catalog again, the overrides become redundant but stay harmless; removing them is a separate, verified change.
- Filing the regression upstream is left to the maintainer.
