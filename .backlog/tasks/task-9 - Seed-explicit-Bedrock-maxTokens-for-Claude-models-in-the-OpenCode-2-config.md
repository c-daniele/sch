---
id: TASK-9
title: Seed explicit Bedrock maxTokens for Claude models in the OpenCode 2 config
status: Done
assignee:
  - '@opencode'
created_date: '2026-09-26 17:39'
updated_date: '2026-09-26 18:10'
labels: []
dependencies: []
references:
  - docs/specs/platform/runtime-image.md
  - image/scripts/init-workspace.sh
  - 'https://opencode.ai/v2/docs/migrate-v1/'
  - >-
    https://docs.aws.amazon.com/bedrock/latest/userguide/quotas-token-burndown.html
priority: high
type: bug
ordinal: 10000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
On OpenCode 2 (2.0.18, the image pin) Bedrock invocations of Anthropic models often hang: the maintainer observed every request going out with a 4096-token output cap. Root cause (verified in the 2.0.18 bundle and with an offline fake-Bedrock probe): the Bedrock Converse request builder only sends `inferenceConfig.maxTokens` when the request generation options carry one, and the session layer never sets it by default, so Bedrock applies its own default. The maintainer worked around it locally with per-model `providers.amazon-bedrock.models.<id>.body.inferenceConfig.maxTokens` overrides in ~/.config/opencode/opencode.json; the remote runtime needs the same override in the config init-workspace.sh seeds. Constraint found by the probe: the seed still uses the V1 `provider.amazon-bedrock.options.region` shape, and a native V2 `providers.amazon-bedrock` entry next to it makes OpenCode drop the V1 block entirely (region and endpoint lost), so the seeded provider block must move to the V2 shape as a whole. Maintainer decisions (2026-09-26): cover the maintainer global profiles (Fable 5 64000, Fable 5.1 128000, Opus 5 64000, Opus 5.5 128000), their EU profiles (Fable 5, Opus 5, Opus 5.5 with the same values), and the seeded default model eu.anthropic.claude-sonnet-4-6 at 64000 (its AWS max output; models.dev wrongly says 128K); fresh workspaces only — the never-overwrite seeding contract (runtime-image R19/R24) stays, existing workspaces need a manual edit. Values above a model maximum make Bedrock reject the request, and Bedrock reserves input + maxTokens from the TPM quota at request start, so values are pinned per model rather than maximized.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 A fresh opencode workspace seeds `providers.amazon-bedrock` in the native V2 shape: `settings.region` equals the seed region and no V1 `provider` key remains in the seeded file
- [x] #2 The seeded models map sets `body.inferenceConfig.maxTokens` for the agreed Claude profiles and values, including the seeded default model, and every value is within the model AWS max output
- [x] #3 The seeded config, run through the real OpenCode 2.0.18 binary against a fake Bedrock endpoint, sends `inferenceConfig.maxTokens` for the default model, keeps the seed region, and still loads the MCP entries
- [x] #4 An existing opencode.json is never overwritten, and the shim continue-model availability check reads providers declared under both the V1 `provider` and the V2 `providers` key
- [x] #5 Specs (runtime-image R20, provider-api-keys R12), docs/mcp-and-bedrock.md and image/test-local.sh describe the V2 seed shape; relevant test suites and verify-docs pass
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Probe (done, notes in .backlog/brainstorming/2026-09-26.BedrockMaxTokens/): confirm root cause in the 2.0.18 bundle; offline fake-Bedrock probe of V1 baseline, mixed V1+V2, and pure V2 shapes; AWS model-card max outputs.
2. init-workspace.sh: seed providers.amazon-bedrock (V2) with settings.region and the agreed models map; keep mcp, model, small_model, default_agent unchanged; update comments and the seed log line.
3. main.py: _opencode_available_providers reads both provider (V1) and providers (V2) keys; unit test.
4. Tests: container-free OpenCode seed test (shape, region, map covers default model, values within AWS max, no V1 provider key, never-overwrite); update image/test-local.sh section 9 assertions.
5. Verify the real seeded file with the 2.0.18 binary against the fake Bedrock endpoint.
6. Specs R20/R12, docs/mcp-and-bedrock.md, Dockerfile v34 banner; propose a manual-edit doc example for existing workspaces and wait for confirmation.
7. Run image/app and cli suites, verify-docs; journal, decision, masterplan.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Implemented: init-workspace.sh seeds providers.amazon-bedrock in the native V2 shape (settings.region + 8-model maxTokens map); main.py _opencode_available_providers reads both provider and providers keys. Tests: new image/app/test_opencode_workspace_seed.py (6 tests, all fail on the HEAD seed script and pass now), 3 new OpencodeContinueModelTests (V1 key, V2 key, malformed providers). End-to-end: the file produced by the real init-workspace.sh, run through the OpenCode 2.0.18 binary against a fake Bedrock endpoint in a loopback-only network namespace, sends inferenceConfig.maxTokens 64000 for the default model (also with no --model), 128000 for eu Opus 5.5 and global Fable 5.1, SigV4 region eu-west-1 from settings.region, aws-docs/aws-mcp spawned, context7 disabled, no config warnings.

Docs: specs runtime-image R20 (V2 shape, no V1 block, maxTokens rule) + Behavior table of seeded caps + cross-reference, provider-api-keys R12 key path; docs/mcp-and-bedrock.md stale key paths fixed, new "Bedrock output cap on OpenCode 2" section, maintainer-approved examples for workspaces seeded by an older image (hand edit verified: the old seed matches the documented before-block, and the edited file sends maxTokens 64000 with region eu-west-1 on 2.0.18; re-seed path backed by the boot code: init-workspace.sh runs on every boot after any L2 restore, and _verify_l2_restore does not require opencode.json, not live-verified). Dockerfile v34 banner. image/test-local.sh section 9 asserts V2 region, no V1 key, explicit default-model maxTokens (assertions run against the real seeded file; the full script needs Docker, unavailable to this user: socket permission denied). Validation: image/app 479 OK, cli 590 OK, verify-docs PASSED, bash -n on all CI-parsed scripts OK.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Root cause: OpenCode 2 (2.0.18) sends no inferenceConfig.maxTokens to Bedrock unless configured (the Converse builder only includes it when generation options carry one, and the session layer never sets it), so Bedrock applied its 4096-token Claude default and long turns stalled. The seeded opencode.json (init-workspace.sh) now uses the native V2 providers.amazon-bedrock shape (settings.region) with per-model body.inferenceConfig.maxTokens for the default eu Sonnet 4.6 (64000, its Bedrock max) and the maintainer-agreed global and EU Fable/Opus profiles. The V1 provider block is gone because a V2 entry next to it makes OpenCode drop the V1 block, region included (found by probe). The shim continue-model availability check now reads both provider and providers keys. Fresh workspaces only (never-overwrite contract kept); docs/mcp-and-bedrock.md carries the maintainer-approved fix paths for older workspaces. Verified: new test_opencode_workspace_seed.py (6 tests, all fail on the previous seed script) + 3 continue-model tests; image/app 479 OK, cli 590 OK, verify-docs PASSED; the real seeded file run through the 2.0.18 binary against a fake Bedrock endpoint in a loopback-only network namespace sends maxTokens 64000/128000 with SigV4 region eu-west-1 and loads the MCP entries; the documented hand edit verified the same way. Not verified here (operator-side): docker build + image/test-local.sh (Docker socket not accessible to this user) and a live Bedrock call from a deployed runtime.
<!-- SECTION:FINAL_SUMMARY:END -->
