---
id: TASK-7
title: >-
  Upgrade pinned coding-agent versions (OpenCode 2.0.16, Pi 0.87.1, Claude Code
  2.1.282)
status: Done
assignee:
  - '@opencode'
created_date: '2026-09-25 19:28'
updated_date: '2026-09-26 04:20'
labels: []
dependencies: []
ordinal: 9000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
The maintainer asked to bump the three harness pins in the runtime image: OpenCode 1.18.31 -> 2.0.16 (a MAJOR upgrade of a tool whose CLI surface SCH depends on end to end: 'opencode run --session/--model/--variant/--agent/--auto' for headless tasks, 'opencode web/serve' for the tunnel backend, 'opencode acp' for the ACP editor path, plus the session storage layout the model-forwarding (R8a) and session import/export read), Pi 0.85.1 -> 0.87.1 (pre-1.0: breaking changes between bumps are expected, which is why the image asserts CLI capabilities at build time), Claude Code 2.1.272 -> 2.1.282 (patch bump; the /model picker lineup is baked per-binary-version). The pins live in image/Dockerfile (OPENCODE_VERSION, PI_VERSION, CLAUDE_CODE_VERSION) and are restated in docs/specs/platform/runtime-image.md and docs/harnesses.md; some doc comments still quote older versions (e.g. 2.1.210) and must not drift further. A major OpenCode bump can break the build-time capability asserts, the headless argv contract, the session-layout resolver, the seeded plugin/opencode-templates, and the R8a stored-model forwarding, so the bump must be verified against the real binary rather than assumed compatible.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 image/Dockerfile pins OPENCODE_VERSION=2.0.18, PI_VERSION=0.87.1, CLAUDE_CODE_VERSION=2.1.282, and every build-time capability assert in the image still passes on the new binaries (opencode version-prefix assert, 2.x surface asserts, claude --agent flag, pi print/session flags)
- [x] #2 Every SCH code path that depends on the opencode CLI surface or session storage (headless argv construction, opencode serve supervision, acp adapter, session import/export, R8a stored-model-and-variant forwarding) is checked against OpenCode 2.0.18 and adjusted where the surface changed, with tests covering any adjustment
- [x] #3 All documentation and spec references to harness versions (docs/specs/platform/runtime-image.md, docs/harnesses.md, Dockerfile comments) quote the new pins; stale version quotes corrected on sight
- [x] #4 The relevant test suites pass (image/app tests, cli tests, tunnel tests) and any empirical findings about the new versions are recorded
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Phase 0 — empirical probe (brainstorming/2026-09-25.AgentVersionUpgrade): install @opencode/cli@2.0.16 (NB: V2 moved from the opencode-ai npm package to @opencode/cli; bins opencode+opencode2), pi 0.87.1, claude 2.1.282 in an isolated prefix; probe every surface SCH depends on: opencode subcommands (run/serve/web/acp/import/export/session list/db path vs debug paths), run flags (--session --model --variant --agent --auto), root TUI flags (--session --model), web --hostname/--port, OPENCODE_DB honoring + session-table schema (directory/time_updated/model), auth.json path+shape, V1-shaped opencode.json acceptance, plugin API (V1 sch-telegram.js expected NOT to run — migration guide), server API endpoints used by _opencode_inject_text; pi flags + session layout; claude --agent assert.
2. Phase 1 — from probe results, list required code changes beyond the pin bump (main.py argv/DB/API, tunnel attach/acp path map, sch-telegram plugin port to V2 plugin API, seeded opencode.json migration, test-local.sh db-path check, Dockerfile install command + version-floor assert for 2.x). Present material behavior changes to maintainer before implementing.
3. Phase 2 — implement: Dockerfile pins (OPENCODE_VERSION=2.0.16 via @opencode/cli, PI_VERSION=0.87.1, CLAUDE_CODE_VERSION=2.1.282), stale comment cleanup (2.1.210, 1.18 floor text), code adjustments with tests.
4. Phase 3 — verify: image/app unittests, cli/tests, tunnel node --test; local docker build + image/test-local.sh if feasible; bin/verify-docs.sh.
5. Phase 4 — docs/specs: runtime-image.md version table, harnesses.md, headless-task-execution/remote-ui-tunnel/session-handoff specs where surface changed; journal entry + masterplan update.

6. DECISIONS (maintainer, 2026-09-26): (a) single task, fresh installation — V2-only code paths, no V1 session/credential compatibility or dual-schema fallbacks; existing workspace sessions are discarded. (b) Keep V1 process topology: TUI and headless tasks run as --standalone processes sharing OPENCODE_DB; the supervised 'opencode serve' on :4096 (password pinned via OPENCODE_SERVER_PASSWORD, persisted 0600 on local disk) serves only attach/web/injection; clients authenticate with OPENCODE_PASSWORD / basic auth. Shared-server topology deferred (would break per-task provider-key staging, decision-7).

Phase 3 — retarget OpenCode 2.0.16 -> 2.0.18 (maintainer correction: the newest 2.x release is 2.0.18; it ships in the @opencode/cli npm package, @opencode/ai is the SDK with no binary). Bump the pin and every version quote (Dockerfile banner/comments, test-local.sh, acp-path-map.test.js, runtime-image.md table, main.py/deps.py docstrings, test fixtures, test_runtime_dependency_pins.py pin assert); verify 2.0.18 CLI surface against the 2.0.16-validated assumptions (diff help output of both versions + ACP initialize handshake); re-run image/app, cli, tunnel suites and verify-docs; rebuild the arm64 image and run image/test-local.sh; update masterplan and journal.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Phase 0 (empirical probe) done; full findings in .backlog/brainstorming/2026-09-25.AgentVersionUpgrade/findings.md. Pi 0.87.1 and Claude 2.1.282 verified safe (all asserted flags present, pi session layout unchanged). OpenCode 2.0.16 verified REAL but is a porting project, not a bump: npm package renamed to @opencode/cli (postinstall required), --version output format changed, web->serve (authenticated: basic auth opencode:<pw>, OPENCODE_SERVER_PASSWORD/OPENCODE_PASSWORD), attach -> --server, root --model gone (OPENCODE_CONFIG_CONTENT works), run --variant merged into --model p/m#variant, session list/export/import moved under 'session' (same JSON shapes), db: session->session_v2 (auto-migrates V1 in place, columns SCH reads survive, variant now inline), auth.json migrated into DB credential table, V1 sch-telegram plugin FAILS to load (V2 plugin API port required), server API moved to /api/* (prompt_async gone; /api/session/{id}/prompt, permission reply, /api/event SSE exist), run --agent unknown now hard-errors, boolean flags swallow y/n/true/false prompt tokens (use '--auto -- <prompt>'). V1-shaped seeded config + agent files load unchanged. Awaiting maintainer decision on scope split and in-VM server topology before implementing.

Maintainer decisions: one task + fresh install (no V1 compat); keep V1 topology (standalone TUI/tasks + supervised authenticated serve). Starting Phase 2 implementation.

Phase 2 implemented (36 files). Dockerfile: pins 2.0.16/0.87.1/2.1.282, @opencode/cli install, version-prefix assert, 2.x surface asserts, v33 banner. main.py: serve supervisor (web->serve, OPENCODE_SERVER_PASSWORD pinned+persisted 0600 on local disk, auth in serve-ensure response), _opencode_api basic auth + /api/*, injector -> POST /api/session/{id}/prompt, session->session_v2 (parent_id filter), credential table replaces auth.json, session import argv, headless argv (--standalone, --model p/m#variant, --agent only if seeded file exists, '--auto -- <prompt>'), version prefix strip. sch-run-profile.sh: OPENCODE_CONFIG_CONTENT model merge + --standalone. Plugin sch-telegram.js ported to V2 API (default {id,setup}; event.subscribe, tool.hook, permission.hook evaluate; typed session.context fallback for turn-end text). CLI: attach --server + OPENCODE_PASSWORD via env, web URL with basic-auth userinfo, handoff session export/list --standalone, shared version normalizer in deps.py. tunnel/attach.js --server + password env. verify scripts: /api/* + auth, --standalone. Specs: runtime-image R4/R8/R40/table, headless-task-execution R8/R8a/R8b/R23, remote-ui-tunnel R13/R13a, session-handoff R1/R5, run-model-selection R2a/R5/R6/R10, telegram-interaction R5; guides updated. Tests: image/app 470 OK, cli 590 OK, tunnel 12 OK, verify-docs PASSED. Local docker image build running (test-local.sh next).

Validation: docker build linux/arm64 rc=0 (Steps 28/29 opencode install+version+surface asserts passed; claude 2.1.282; pi asserts ok); image/test-local.sh 138 passed / 0 failed (pinned=2.0.16 installed=2.0.16, debug paths db, pi 0.87.1); ported sch-telegram.js loads in the real image with no 'failed to load plugin' WARN; image/app 470 OK; cli 590 OK; tunnel 12/12 OK; verify-docs PASSED. Live-AWS follow-ups (operator-side): Bedrock via execution role on OpenCode 2, Telegram plugin end-to-end (permission relay/idle/keep-alive), bin/verify-remote-ui-tunnel.sh + verify-handoff.sh + verify-headless-tasks.sh, memory profile of 'opencode serve' under AgentCore billing.

Phase 3 in progress. Registry checks: @opencode/cli@2.0.18 is the current latest 2.x (opencode-ai stops at 1.18.x; @opencode/ai 2.0.18 is the SDK, no bin). Pi 0.87.1 and Claude 2.1.282 confirmed current on npm. Surface diff 2.0.16 vs 2.0.18 over run/serve/acp/session export/session import/debug paths/plugin help: identical apart from version strings and the 'pair' description; ACP initialize handshake on 2.0.18 returns agentInfo {OpenCode, 2.0.18}, protocolVersion 1.

Phase 3 validation: docker build linux/arm64 rc=0 (all 74 steps; the @opencode/cli@2.0.18 install, the prefix-stripped version assert and the 2.x surface asserts all pass on the real 2.0.18 binary); image/test-local.sh 138 passed / 0 failed (pinned=2.0.18 installed=2.0.18); image/app 470 OK; cli 590 OK; tunnel 12/12 OK (node --test *.test.js; test/fake-zed.js is the ACP client harness for verify-acp-editor.sh, not a test file); verify-docs PASSED. Stray '2.0.16' quotes only remain in dated .backlog records (doc-10, findings.md, task history).
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Retargeted the OpenCode pin from 2.0.16 to 2.0.18 (maintainer correction: the newest 2.x release is 2.0.18). Package clarification: the CLI ships in @opencode/cli — @opencode/ai 2.0.18 is the OpenCode SDK with no binary — so the image's install line was already right; the change is the pin plus every version quote (image/Dockerfile ARG + banner/comments, runtime-image.md version table, image/test-local.sh, tunnel/acp-path-map.test.js, main.py/deps.py docstrings, test fixtures, test_runtime_dependency_pins.py pin assert). Fixed a stale doc statement on the way (docs/harnesses.md called Pi 0.84.2 'the pinned version'; the pin is 0.87.1). Verified 2.0.18 against the 2.0.16-validated assumptions: the help output of run/serve/acp/session export/session import/debug paths/plugin diffs clean between the two versions (only version strings and one 'pair' description differ) and an ACP initialize handshake answers agentInfo {OpenCode, 2.0.18} at protocolVersion 1. Verified: docker build linux/arm64 rc=0 with the version-prefix assert and 2.x surface asserts passing on the real 2.0.18 binary; image/test-local.sh 138 passed / 0 failed (pinned=2.0.18); image/app 470 OK; cli 590 OK; tunnel 12/12 OK; verify-docs PASSED. Pi 0.87.1 and Claude Code 2.1.282 confirmed current on npm. Journal doc-11.
<!-- SECTION:FINAL_SUMMARY:END -->
