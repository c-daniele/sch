---
id: doc-13
title: 2026-09-26 Bedrock Claude requests capped at 4096 output tokens on OpenCode 2
type: other
created_date: '2026-09-26 18:09'
updated_date: '2026-09-26 20:41'
tags:
  - journal
---
# 2026-09-26 Bedrock Claude requests capped at 4096 output tokens on OpenCode 2

Task: TASK-9. Spec: [runtime-image](../../../docs/specs/platform/runtime-image.md) R20. Guide: [MCP and Bedrock](../../../docs/mcp-and-bedrock.md).

## Problem

After the move to OpenCode 2, Bedrock sessions on Claude models often looked hung. The maintainer noticed that every request went out with a 4096-token output cap, and worked around it by adding a per-model output cap to a local OpenCode config. The remote runtime seeds its own config, so it still had the problem.

The cause is a gap in OpenCode 2. It only sends Bedrock an output cap when something configures one, and by default nothing does. Bedrock then applies its own default, 4096 tokens for Claude. A long answer or a large file edit gets cut off at that point, and the agent loop stalls. OpenCode 1.x derived the cap from the model catalog, so this is a regression, not an SCH bug.

## What changed

The config that `init-workspace.sh` seeds for new OpenCode workspaces now sets an explicit output cap for the default model (EU Sonnet 4.6, 64K, its Bedrock maximum) and for the Fable and Opus profiles the maintainer uses most, in their global and EU variants. The values are the maintainer's; each stays within the model's Bedrock maximum.

The seeded provider block also had to change shape. It used the old OpenCode 1.x layout, and a probe showed that adding a new-layout entry for the same provider next to it makes OpenCode silently ignore the old one, region included. So the whole provider block now uses the OpenCode 2 layout. The shim's check for "which providers can serve a resumed session" now reads both layouts.

Existing workspaces keep their config: the seeding rule never overwrites an operator's file. The guide explains how to fix one by hand, or how to re-seed it.

## Outcome

With the seeded file, OpenCode 2.0.18 sends the configured cap and keeps the seed region. This was checked offline: the real binary ran against a fake Bedrock endpoint in a network-isolated sandbox. The new container-free seed tests fail on the old script and pass on the new one. All suites and the documentation checks pass. A Docker build with `image/test-local.sh` and a live Bedrock call from a deployed runtime were not run here, so they remain operator follow-ups.

## Lesson

A config that "loads without warnings" can still be wrong. The OpenCode 2 migration check in TASK-7 confirmed the 1.x-shaped seed loaded cleanly, but it never checked what actually went over the wire. A fake endpoint that logs the request body answered in minutes both why requests were capped and why simply appending the fix would have dropped the region. For provider config, check the outgoing request, not just the parsed config.

Also: model catalogs are not authoritative for provider limits. models.dev lists 128K output for Sonnet 4.6, but the Bedrock model card says 64K, and Bedrock rejects anything above the real maximum. Take limits from the provider's own model card.
