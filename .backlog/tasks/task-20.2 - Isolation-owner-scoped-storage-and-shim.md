---
id: TASK-20.2
title: 'Isolation: owner-scoped storage and shim'
status: Done
assignee: []
created_date: '2026-09-27 14:50'
updated_date: '2026-09-27 16:07'
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
- [x] #1 The shim takes the owner prefix only from the runtime environment and rejects a payload that disagrees with it
- [x] #2 Checkpoints, generations, writer claims, task status and build sources use the per-owner layout of the spec; the registry-off layout is unchanged
- [x] #3 Nothing is written into the workspace root before the bootstrap restore completes; tests cover s3 and session restores with concurrent invokes
- [x] #4 Task prompts no longer appear in runtime logs
- [x] #5 sch-build-image uploads build sources under the owner prefix when it is set, and the task watchdog lists both layouts
- [x] #6 Runtimes without the owner variable behave exactly as before; existing image-side and infra tests pass
<!-- AC:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [x] #1 Relevant suites pass (cli, infra, tunnel, image-side tests with sandboxed paths) and bin/verify-docs.sh when docs changed
- [x] #2 Implementation notes list every assumption and the verification results; a journal entry is created and MASTERPLAN.md updated as AGENTS.md requires
- [x] #3 Work committed on feat/task-20 with conventional commits, never pushed
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

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Assumptions (headless run, defaults applied):
- An empty or whitespace SCH_OWNER_PREFIX counts as unset (isolation off). Only a non-empty malformed value is an error. The bucket policy (R24) denies flat-layout writes by plane roles anyway.
- A payload owner_prefix of null counts as absent. Any other value, "" included, must equal the runtime's prefix. A runtime without the prefix rejects any payload that carries one, so a CLI that believes isolation is on never writes to a shared runtime.
- With the prefix set, the payload workspace must match the registry identity pattern ^ws-[a-z2-7]{40}$. Otherwise the call is rejected before any effect. The session-id fallback is off, and a marker workspace outside the pattern is ignored.
- The same owner gate covers the first WebSocket message (tunnel), because it also sets the storage backend, epoch and workspace. Invocation means every data-plane entry point of the shim.
- Manifest validation, prefix set: artifact keys outside checkpoints/<p>/ and checkpoint-generations/<p>/ make the manifest malformed. This is defense in depth for R28.
- R31 scope: invocation paths of the shim. Audited: checkpoint (it wrote the DB backup, replicas and marker; now it waits up to FS_WORKSPACE_READY_TIMEOUT_S, then answers skipped-not-ready, and _do_checkpoint itself refuses before readiness); GitHub-access reconciliation (writes .git/config; now deferred and run once by the bootstrap before readiness). The other actions do not write the root before readiness: prepare-run, the serve password, command-shell presence, staging and the telegram spool all write outside it, and git-seed, git-snapshot, session-import, fs and exec tunnels wait for readiness. Version probes (info, serve-ensure) go through harness-wrapper.sh, which waits for the ready marker, so the real binary never runs before readiness (by reading the code; no real binary in the tests). Shells the owner opens with sch shell are outside the shim and not covered.
- R32: the prompt element of the headless argv is logged as <prompt: N chars>. A second leak was found and fixed: invoke() logged the action name, which for a payload without an action is the prompt. It is now logged as <unrecognized: N chars>. This applies in every mode (the R34 exception).
- sch-build-image reads SCH_OWNER_PREFIX from its environment, like SCH_CHECKPOINT_BUCKET. From a login shell neither variable exists, so the script fails as it did before.
- Watchdog: list_workspaces returns paths relative to checkpoints/ ("o.<k>/<ws>" for owner trees), so the task-status and topic keys are nested with no other change. IAM is unchanged: s3:prefix checkpoints/* and the Get/Put resource checkpoints/*/task-status.json already cover nested keys.
- The same early-write defect exists with isolation off: it was reproduced offline by running the NoEarlyWrites harness with the prefix unset, and state/ appears in the root. Registry-off behavior was not changed (R34). Follow-up TASK-23 was created.

Verification:
- New image/app/test_owner_prefix.py, 25 tests: prefix parsing; the invalid prefix blocks invoke and bootstrap with no S3 call, no staging, no epoch and no writes; payload mismatch; workspace rules; the WebSocket gate; key layout in both modes, including the real checkpoint pass, writer claim and task status against a fake S3; foreign manifest artifacts; no early writes for the s3 L2, session L2 and session platform restores, with 11 concurrent actions and a local DB present; prompt redaction for the three harnesses, with and without the prefix. Mutation check: with the checkpoint gate removed, the four no-early-writes tests fail on state/ in the root.
- New image/app/test_sch_build_image.py, 4 tests: the real script with fake aws and zip; the key in both modes; an invalid prefix fails before any AWS call.
- infra/test_task_watchdog_handler.py: 6 new tests covering both layouts, look-alike names, nested reconcile and re-send, a paginated owner tree, and every S3 request checked with the botocore ParamValidator.
- Suites: cli 595 OK; infra 133 OK; tunnel npm test all pass; image-side 534 tests with sandboxed paths, and only the 7 known TASK-22 failures, the same as the 505-test baseline before the change; bin/verify-docs.sh passes.
- Live-session files (/home/sch/.sch-workspace.json, /run/sch/provider-keys.env) were not touched by the new tests.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Runtime side of per-principal isolation (spec R26-R35). The shim takes the owner prefix only from SCH_OWNER_PREFIX. Invalid values fail every invocation and the boot before any storage access, and payloads naming another owner are rejected before any effect. Checkpoints, generations, writer claims, task status and build sources use the o.<k> owner segment, and the isolation-off keys are byte-identical to before. Plane runtimes write nothing into the root before the boot restore: the checkpoint waits or skips, and the GitHub reconcile is deferred. Prompts are redacted from the logs in every mode. The watchdog lists both layouts. The specs are updated. The same early-write defect on isolation-off runtimes is tracked as TASK-23. Verification is in the implementation notes.
<!-- SECTION:FINAL_SUMMARY:END -->
