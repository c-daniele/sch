---
id: decision-16
title: >-
  Claude model pins must not be older than the pinned Claude Code's Bedrock
  alias targets, enforced at image build
date: '2026-10-02 12:36'
status: accepted
---
## Context

On a fresh workspace Claude Code compares the image's Opus, Sonnet and Haiku alias pins (`ANTHROPIC_DEFAULT_<TIER>_MODEL`) with the model catalog baked into its binary. When a pin is older it shows "Newer <tier> model available", and accepting restarts Claude Code, which ended the AgentCore session on 2026-10-02 (TASK-26). Probing Claude Code 2.1.285 showed that the reference is the tier's Bedrock alias target (`aliases.<tier>.per_provider.bedrock`, else `aliases.<tier>.default`), not the newest model of the tier: Sonnet 4.6 is accepted while Sonnet 5.5 is known. Every Claude Code bump can move those targets.

Claude Code is installed by root through npm and pinned on purpose, so its self-updater cannot work for the runtime user. It still queried the registry and showed a warning on every start.

## Decision

- The image Opus, Sonnet and Haiku pins are kept at or above the Bedrock alias targets of the pinned Claude Code. `image/scripts/check-claude-model-pins.mjs` extracts the catalog from the installed binary and fails the image build when a pin is older, unknown, or of the wrong family. It runs both on the container ENV and on a clean login shell (`/etc/profile.d/sch-env.sh`). Models newer than the target are reported as notes, not failures.
- The model pins and `DISABLE_AUTOUPDATER=1` are defined identically in the Dockerfile ENV, the generated `sch-env.sh` and `harness-wrapper.sh`, always as fallbacks, so an operator's environment value or workspace `settings.json` still wins.
- SCH does not hide the dialog with `CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST`, because that also switches Bedrock credentials away from the execution role.

## Consequences

- A Claude Code bump that moves an alias target fails the image build with a message naming the pin to use, instead of reintroducing the dialog in production.
- The default Opus alias needs Bedrock access to the pinned model in the operator's account. A deployment without it must override the pin (workspace `settings.json` env or an image rebuild), as documented in `docs/harnesses.md`.
- The check depends on the catalog layout inside the Claude Code binary. If the anchor disappears, the build fails with exit code 2, and the check must be updated along with the bump.
- Claude Code never updates itself in the image. New versions arrive only through the pinned version in the Dockerfile.
