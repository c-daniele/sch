---
id: TASK-20.2
title: 'Isolation: owner-scoped storage and shim'
status: To Do
assignee: []
created_date: '2026-09-27 14:50'
updated_date: '2026-09-27 15:20'
labels:
  - security
dependencies:
  - TASK-20.1
parent_task_id: TASK-20
priority: high
type: feature
ordinal: 16000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Slice 2 of TASK-20. Image-side changes: owner prefix from the runtime environment, per-owner layout, no early writes, prompts out of the logs, per-owner sch-build-image key, watchdog listing of the new layout. Context, decisions, defaults, lessons and constraints live in the parent TASK-20; read it first and follow it. Headless run: apply the defaults, record every assumption in the implementation notes, and stop if the dependency is not Done. Work on `feat/task-20`, commit at the end, never push.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 The shim takes the owner prefix only from the runtime environment and rejects a payload that disagrees with it
- [ ] #2 Checkpoints, generations, writer claims, task status and build sources use the per-owner layout of the spec; the registry-off layout is unchanged
- [ ] #3 Nothing is written into the workspace root before the bootstrap restore completes; tests cover s3 and session restores with concurrent invokes
- [ ] #4 Task prompts no longer appear in runtime logs
- [ ] #5 sch-build-image uploads build sources under the owner prefix when it is set, and the task watchdog lists both layouts
- [ ] #6 Runtimes without the owner variable behave exactly as before; existing image-side and infra tests pass
<!-- AC:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [ ] #1 Relevant suites pass (cli, infra, tunnel, image-side tests with sandboxed paths) and bin/verify-docs.sh when docs changed
- [ ] #2 Implementation notes list every assumption and the verification results; a journal entry is created and MASTERPLAN.md updated as AGENTS.md requires
- [ ] #3 Work committed on feat/task-20 with conventional commits, never pushed
<!-- DOD:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
Spec: docs/specs/security/per-principal-isolation.md (R26-R35). Decision: decision-14. Check first that TASK-20.1 is Done.

1. Owner prefix (R29, R30, R34). In image/app/main.py read SCH_OWNER_PREFIX once at import, validate ^o\.[0-9a-f]{16}$. Invalid: every invoke returns an explicit error before any S3 call or workspace write. Unset: no change anywhere. In invoke(), reject a payload whose owner_prefix differs from the env value, as the first check after payload parsing. Never read the prefix from payload or marker files. With the prefix set, ignore the session-id fallback for the workspace name (registry session IDs carry no name) and require the payload workspace to match the registry identity pattern.
2. Layout (R26, R28). Route every key builder through one helper: _s3_key (checkpoints/), the generation keys (checkpoint-generations/, two call sites near lines 1629 and 2977), the writer-claim keys (workspace-writers/, lines 2040 and 2100), the task-status and telegram-topic objects. Prefix set: insert "<prefix>/" after the top-level folder (writer claim: workspace-writers/o.<k>/<ws>.json). Unset: keys byte-identical to today. Grep for any other literal "checkpoints/" in image/.
3. No early writes (R31). Audit every write under WORKSPACE_ROOT reachable from invoke() before the bootstrap restore finishes (mount marker state/.sch-initialized, harness marker, prepare-run marker, presence files, env rebuild). With the prefix set, defer them until the restore gate is open, or answer without writing. Tests: s3 and session restores with concurrent invokes (noop, info, task) during the bootstrap, asserting the root is empty until promotion. If the same defect exists without the prefix, record it and create a follow-up task rather than changing registry-off behavior silently.
4. Prompts out of logs (R32). Replace the argv log at the task start (logger.info "task %s running", near line 5424) and any other prompt log with a redacted argv (<prompt: N chars>). Test with a captured logger that the prompt text never appears.
5. sch-build-image (R33). In image/scripts/sch-build-image.sh, SOURCE_KEY becomes builds/${SCH_OWNER_PREFIX}/${SCOPE}/source.zip when SCH_OWNER_PREFIX is set and valid; an invalid value fails fast. Test the key selection with the existing script tests (or add a bash test harness under image/tests).
6. Watchdog (R35). infra/task_watchdog_handler.py: list checkpoints/ with delimiter; a common prefix matching ^o\.[0-9a-f]{16}/$ is listed one level deeper for workspaces. Keys for task-status and telegram-topic follow the nested path. IAM unchanged. Extend infra/test_task_watchdog_handler.py with both layouts; check list_objects_v2 request shapes with botocore ParamValidator.
7. Tests and constraints. Image-side tests with SCH_TELEGRAM_ENABLED_MARKER, SCH_PROVIDER_KEYS_FILE, SCH_GIT_CREDENTIALS_FILE, SCH_CHECKPOINT_TMP_DIR and SCH_WORKSPACE_ROOT in a temp dir, hard-coded roots patched. Run cli, infra, tunnel and image-side suites (TASK-22 lists the seven known out-of-container failures; compare against that baseline).
8. Docs in the same change: workspace-checkpointing and headless-task-execution specs mention the owner segment and the log redaction; per-principal-isolation cross-references updated. Journal, masterplan, commit on feat/task-20.
<!-- SECTION:PLAN:END -->
