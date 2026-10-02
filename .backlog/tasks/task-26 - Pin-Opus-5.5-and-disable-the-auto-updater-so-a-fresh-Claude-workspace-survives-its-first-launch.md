---
id: TASK-26
title: >-
  Pin Opus 5.5 and disable the auto-updater so a fresh Claude workspace survives
  its first launch
status: Done
assignee:
  - '@claude'
created_date: '2026-10-02 11:54'
updated_date: '2026-10-02 19:30'
labels:
  - claude
  - image
dependencies: []
references:
  - image/Dockerfile
  - image/scripts/harness-wrapper.sh
  - image/scripts/sch-run-profile.sh
  - image/app/test_provider_api_keys.py
  - docs/specs/platform/runtime-image.md
  - docs/harnesses.md
priority: high
type: bug
ordinal: 23000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
## Problem

On 2026-10-02 the first interactive Claude launch in a fresh workspace ended by itself seconds after the first-run dialogs. `sch run <ws> --harness claude --sync .` showed the TUI, the operator answered the first-run dialogs, and the remote session closed before the prompt was usable. It happened three times on two deployments (05:19 and 09:34 local time with `sch run`, and once with `sch shell` followed by `claude --debug-file`), always with Claude Code 2.1.285 and always on a workspace without Claude state. Launches after the first-run state existed ran for minutes (3 and 7.5 minutes the same morning).

## Cause (established)

The image pins `ANTHROPIC_DEFAULT_OPUS_MODEL=eu.anthropic.claude-opus-5`. The pinned Claude Code knows Opus 5.5 (2.1.282 and 2.1.285 both do) and the Bedrock account can call it, so on a fresh workspace Claude Code asks once: "Newer Opus model available - Currently pinned: Opus 5 - Latest available: Opus 5.5 (eu.anthropic.claude-opus-5-5) - Update settings to use Opus 5.5? Claude Code will restart to apply." Accepting writes `env.ANTHROPIC_DEFAULT_OPUS_MODEL` into `~/.claude/settings.json` (so the question is not asked again in that workspace) and restarts Claude Code: the running process spawns a second `claude` on the same terminal and stays alive as a wrapper around it. In the AgentCore microVM that restart ends the session. Under `sch run` the first process is the login shell itself (`exec` in `image/scripts/sch-run-profile.sh`), so its exit closes the remote session.

The `claude --debug-file` log of the `sch shell` run proves the dialog and the restart: two processes (pid 153, then pid 170 370 ms later), `settings.json` growing from 1754 to 1822 bytes (exactly the 68 bytes of the new pin line), and the second process starting with the Opus 5.5 model. The two `sch run` failures (runtime CloudWatch logs only) show the same signature: shell exit status 0 right after the first-run dialogs.

## Not established: why the restart kills the session

In the `sch shell` run the first shutdown line appears exactly 30.0 s after the first process started, and the `sch run` session ended 135.14 s after it started (4 x 30 s + 15 s). The Claude Code source (read in 2.1.287) has a 30 s shutdown watchdog that fires when stdin is not readable, with fixed 5 s and 15 s drains, and the first process closes its own stdin when it hands over. That fits both timings but is not proven: in a local container with the same Claude build and SCH image the restart works and both processes stay alive for 90 s, so something specific to the AgentCore terminal is involved. Setting `CLAUDE_CODE_DIAGNOSTICS_FILE=/tmp/x.jsonl` makes Claude record the shutdown reason (`shutdown_signal` entries) on a fresh workspace.

## Second nuisance: the auto-updater

Claude Code is installed system-wide by root through npm, so the runtime user cannot update it. On every start it still queries the npm registry, tries `npm install -g` and shows "Auto-update failed: no write permission to npm prefix - Run claude doctor" (the "cannot /update" warning the operator saw just before the session closed). The image pins Claude Code on purpose (runtime-image spec), so self-update is unwanted even where it could work, and the registry call is unsolicited outbound traffic. `DISABLE_AUTOUPDATER=1` turns it off; with it the update attempt and the warning disappeared in the local container run.

## Context for the fix

- The staleness check covers the Sonnet, Opus and Haiku tiers; only Opus was stale.
- The Claude model environment is defined in three places that must stay consistent, because login shells opened by `agentcore exec` do not inherit the container ENV: the Dockerfile ENV block, `/etc/profile.d/sch-env.sh` (generated in the Dockerfile) and the dispatcher `image/scripts/harness-wrapper.sh`. The updater switch has the same constraint.
- The Global Opus row of the `/model` picker (`ANTHROPIC_CUSTOM_MODEL_OPTION`, currently "Opus 5 (Global)") exists to offer the global profile of the same Opus; the working assumption is that it follows the alias to Opus 5.5.
- Pinning Opus 5.5 means the default Opus alias needs Bedrock access to Opus 5.5 in the operator's account and region; other deployments may not have it enabled yet.
- Do not silence the dialog with `CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST`: besides skipping the model checks it switches Bedrock credentials to host-provided ones and would break the execution-role credentials.
- Out of scope: finding out why a Claude Code restart ends an AgentCore session. Other restart paths may trigger it too; a follow-up should capture the diagnostics file on a fresh workspace. Related closed work: TASK-7 (harness pins).
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 A fresh Claude workspace opened with `sch run <ws> --harness claude` (and with `sch shell` followed by `claude`) shows only the first-run theme and trust dialogs: no "Newer ... model available" dialog, no Claude Code restart, and the TUI stays open at the prompt
- [x] #2 The Opus alias resolves to Opus 5.5 (`eu.anthropic.claude-opus-5-5`) and the Global Opus picker row offers Opus 5.5 (`global.anthropic.claude-opus-5-5`) on every Claude launch path: interactive login shell, `sch run` autostart and headless `sch task`
- [x] #3 Existing behavior is preserved: an explicit `ANTHROPIC_DEFAULT_OPUS_MODEL` in the environment or in the workspace `~/.claude/settings.json` still wins, and no existing workspace file is rewritten
- [x] #4 Claude Code makes no update attempt (no registry query, no `npm install`) and shows no auto-update warning on any launch path
- [x] #5 A repeatable check fails when a Claude model pin (Opus, Sonnet or Haiku) in the image is older than the newest model of that tier known to the pinned Claude Code, so a future Claude Code bump cannot bring the dialog back unnoticed
- [x] #6 Specs, guides and tests that quote the Claude model pins or the Claude launch environment are updated (runtime-image R12, docs/harnesses.md, image-side tests); the docs state that the default Opus alias needs Bedrock access to Opus 5.5 and how to override it; `bin/verify-docs.sh` passes
- [x] #7 The change is verified on a real image: the image-side and CLI test suites pass, and a fresh workspace stays open through the first-run dialogs (container run against a stand-in Bedrock endpoint, or an operator-side live check)
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Pin ANTHROPIC_DEFAULT_OPUS_MODEL=eu.anthropic.claude-opus-5-5 ("Opus 5.5 (EU)") and ANTHROPIC_CUSTOM_MODEL_OPTION=global.anthropic.claude-opus-5-5 ("Opus 5.5 (Global)") in the Dockerfile ENV, the generated sch-env.sh and harness-wrapper.sh; keep the :- fallbacks so an explicit env or settings.json value still wins; no init-workspace change (no workspace file rewritten).
2. Add DISABLE_AUTOUPDATER=1 in the same three places (fallback form).
3. Add image/scripts/check-claude-model-pins.mjs: extract the baked model catalog from the pinned Claude Code binary and fail when an Opus/Sonnet/Haiku pin is older than the Bedrock alias target the upgrade dialog uses (aliases.<tier>.per_provider.bedrock, else default; rule established empirically, see notes). Run it at image build time against the image ENV and a login shell.
4. Tests: new image/app/test_claude_model_pins.py (three definitions agree, checker logic on synthetic catalogs, checker against the installed binary when present); update test_provider_api_keys fixtures.
5. Docs: runtime-image R12 (+ updater requirement), docs/harnesses.md (table, Bedrock access note, override), docs/deploy.md row; verify-docs.
6. Verify: image + CLI suites; pty-driven fresh-launch run of the real 2.1.285 binary through the modified wrapper (no dialog, no restart, no updater attempt, TUI at prompt).
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Investigation 2026-10-02 (inside the SCH image, Claude Code 2.1.285, pty-driven fresh CLAUDE_CONFIG_DIR):
- Reproduced the dialog with the current pins: 'Newer Opus model available ... Opus 5.5 (eu.anthropic.claude-opus-5-5)' about 6 s after the trust dialog.
- The binary embeds a plain-JS model catalog (anchor: the '//' key 'Hand-maintained baked-in model catalog'). aliases.opus.per_provider.bedrock=claude-opus-5-5, aliases.sonnet.per_provider.bedrock=claude-sonnet-4-5, aliases.haiku.default=claude-haiku-4-5; latest_per_family also lists sonnet 5.5 and fable 5.1.
- Discriminating runs: Sonnet pinned to 4.6 -> no dialog; Sonnet 4 -> 'Newer Sonnet model available ... Sonnet 4.5'; Opus 5.5 + Sonnet 5 + Fable 5 + custom option Opus 5 -> no dialog. So the dialog compares each Opus/Sonnet/Haiku pin with the tier's Bedrock alias target, not with the newest model of the tier; Fable and ANTHROPIC_CUSTOM_MODEL_OPTION are not checked.
- Assumption for AC5: 'newest model of that tier known to the pinned Claude Code' is read as that Bedrock alias target (what triggers the dialog). Newer models beyond it (Sonnet 5.5) are reported as an informational note, not a failure.
- DISABLE_AUTOUPDATER=1: debug log shows no 'AutoUpdater: Using global update method' / 'Insufficient permissions for global npm install' and the TUI shows no 'Auto-update failed' line; without it both appear.

Verification 2026-10-02 (this session, no image build possible: SCH_IMAGE_REBUILD_PROJECT unset, no Docker):
- Old pins, pty fresh launch of the real 2.1.285 binary: dialog reproduced at about 5.7 s.
- New pins through a simulated login shell (env -i, new sch-env.sh lines, new wrapper, real binary): no dialog, settings.json untouched, one claude pid, TUI at the prompt after 120 s, no update attempt or warning.
- Headless claude -p --model opus through the wrapper with the image ENV: rc 0, model eu.anthropic.claude-opus-5-5, debug log 'auto-updater disabled'.
- settings.json env pin to eu.anthropic.claude-opus-5: no dialog in two launches; headless used Opus 5 (AC3).
- check-claude-model-pins.mjs: passes the new pins on the real binary, fails Opus 5 (also fails the currently deployed image's login shell).
- Suites: image test_claude_model_pins + test_provider_api_keys 54 OK; clean env image suite 546 OK plus one unrelated test_db_backup_wal module error (pre-existing); CLI 677 OK (3 skipped); bin/verify-docs.sh passed; bash -n image/test-local.sh ok.
- Bedrock access: the sch-dev execution-role account cannot call Opus 5.5 ('not available for this account'); the staged Bedrock API key account can. Documented in docs/harnesses.md.
Open (AC1, AC7): build the image (sch-build-image or CodeBuild), run image/test-local.sh section 10a, then a live 'sch run <fresh-ws> --harness claude' on a deployment whose account has Opus 5.5 access.
Follow-ups recorded without starting them (unattended session): TASK-27 restart-ends-session diagnostics, TASK-28 stale TASK-26 GitHub-access references.

Live check 2026-10-02 (operator, on the built TASK-26 image, Claude Code 2.1.287): fresh workspace (Claude state created 19:02:25, claude started 19:02:32 by the runtime); no upgrade dialog accepted (no env.ANTHROPIC_DEFAULT_OPUS_MODEL in ~/.claude/settings.json), a single claude process alive for more than 25 minutes, session at the prompt. Image ENV and /etc/profile.d/sch-env.sh carry the Opus 5.5 pins and DISABLE_AUTOUPDATER=1. /usr/local/lib/sch/check-claude-model-pins.mjs passes on 2.1.287 with the container ENV and a login shell. The deployment account can call Opus 5.5. image/test-local.sh is a docker-run harness for a Docker host and cannot run inside the image; AC7 closed by the operator-side live check per its wording. Unit suites were last run before the 2.1.287 bump and were not re-run in the image (AC7 checked at the maintainer's request).
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Pinned the Claude Opus alias to Opus 5.5 (eu.anthropic.claude-opus-5-5) and the Global picker row to global.anthropic.claude-opus-5-5, and set DISABLE_AUTOUPDATER=1, in all three launch definitions (Dockerfile ENV, sch-env.sh, harness-wrapper.sh) with :- fallbacks so explicit values win. Added image/scripts/check-claude-model-pins.mjs, which reads the model catalog baked into the pinned Claude Code binary and fails the image build when an Opus/Sonnet/Haiku pin is older than the Bedrock alias target that drives the upgrade dialog. Tests in image/app/test_claude_model_pins.py and image/test-local.sh 10a; docs in runtime-image R12/R12a/R12b/R15, docs/harnesses.md, docs/deploy.md. Verified with pty-driven fresh launches of the real binary, headless runs, unit suites and verify-docs, then a live fresh-workspace session on the built image (Claude Code 2.1.287): no dialog, no restart, no updater attempt, pin check passing in the image.
<!-- SECTION:FINAL_SUMMARY:END -->
