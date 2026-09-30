---
id: doc-11
title: 2026-09-26 Bump the OpenCode pin to 2.0.18
type: other
created_date: '2026-09-26 04:18'
updated_date: '2026-09-26 04:19'
tags:
  - journal
---
# 2026-09-26 Bump the OpenCode pin to 2.0.18

Task: TASK-7. Continues [2026-09-26 Upgrade harness pins: OpenCode 2.0.16, Pi 0.87.1, Claude Code 2.1.282](doc-10%20-%202026-09-26-Upgrade-harness-pins-OpenCode-2.0.16-Pi-0.87.1-Claude-Code-2.1.282.md).

## Problem

The harness-pin upgrade landed the image on OpenCode 2.0.16. Right after, the maintainer pointed out that the newest OpenCode release is 2.0.18 and asked to take that one. He named the npm package `@opencode/ai`, which is not where the CLI lives.

## What changed

The OpenCode pin moves from 2.0.16 to 2.0.18 in `image/Dockerfile` and the version table in the runtime-image spec. The CLI ships in the `@opencode/cli` npm package, which the image already installs correctly; `@opencode/ai` 2.0.18 is the OpenCode SDK and ships no `opencode` binary, so it cannot be pinned as "the tool". Everything that quoted the pinned version's output shape (the build assert, comments, test fixtures, the pin assertion test) now quotes 2.0.18. One stale doc statement was fixed on the way: the harness guide called Pi 0.84.2 "the pinned version" (the pin is 0.87.1); it now says what was measured when.

## Outcome

2.0.18 is surface-identical to the validated 2.0.16 for everything SCH depends on. Evidence: the help output of `run`, `serve`, `acp`, `session export`, `session import`, `debug paths` and `plugin` diffs clean between the two versions (only version strings and one unrelated description differ), and an ACP `initialize` handshake against 2.0.18 answers `agentInfo {OpenCode, 2.0.18}` at protocolVersion 1. The arm64 image rebuilds with the pin and surface asserts green on the real 2.0.18 binary, `image/test-local.sh` is 138 passed / 0 failed at `pinned=2.0.18`, and the image/app (470), cli (590) and tunnel (12) suites plus the documentation checks all pass.

## Lesson

Before promising a version bump, check the registry for the exact package and version. The OpenCode org publishes several npm packages (`opencode-ai` 1.x, `@opencode/cli` 2.x CLI, `@opencode/ai` SDK); a version number alone does not identify an artifact. `npm view <pkg> versions` answers "does it exist", and `npm view <pkg> bin` answers "is this the tool" — the `bin` field is what a pinned binary install depends on.
