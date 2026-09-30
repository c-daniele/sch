---
id: TASK-20.5
title: 'Isolation: verify scripts and documentation'
status: Done
assignee: []
created_date: '2026-09-27 14:50'
updated_date: '2026-09-27 20:48'
labels:
  - security
dependencies:
  - TASK-20.4
parent_task_id: TASK-20
priority: high
type: docs
ordinal: 19000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Slice 5 of TASK-20. Make bin/verify-*.sh usable on isolation stacks, add bin/verify-isolation.sh, update the guides and SECURITY.md, then set the parent In Progress with only the live check open. Context, decisions, defaults, lessons and constraints live in the parent TASK-20; read it first and follow it. Headless run: apply the defaults, record every assumption in the implementation notes, and stop if the dependency is not Done. Work on `feat/task-20`, commit at the end, never push.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 The bin/verify-*.sh scripts run against an isolation-enabled stack as a listed user
- [x] #2 bin/verify-isolation.sh checks two principals end to end: join denied even with a known session ID, cross-owner checkpoint reads denied from the CLI and from inside the agent, own workflow works, unlisted caller refused
- [x] #3 docs/workspaces.md, docs/deploy.md, docs/security.md and SECURITY.md explain enabling the feature, adding and removing principals, caller permissions, Telegram and ReadOnlyAccess behavior, the AgentCore runtime quota and the residual risks, and claim only what the tests and the simulator prove
- [x] #4 Parent TASK-20 has its first criterion checked and stays In Progress with the live-check criterion open
<!-- AC:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [x] #1 Relevant suites pass (cli, infra, tunnel, image-side tests with sandboxed paths) and bin/verify-docs.sh when docs changed
- [x] #2 Implementation notes list every assumption and the verification results; a journal entry is created and MASTERPLAN.md updated as AGENTS.md requires
- [x] #3 Work committed on feat/task-20 with conventional commits, never pushed
<!-- DOD:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
Spec: docs/specs/security/per-principal-isolation.md (whole spec, especially Threat model and Residual risks). Decision: decision-14. Check first that TASK-20.4 is Done.

1. Audit bin/verify-*.sh for assumptions that break on an isolation stack: the shared runtime ARN from stack outputs (use the registry plane instead), flat checkpoint keys (use the owner segment and the access role), Telegram checks (skip with a clear message when isolation is on). Each script detects isolation from the registry response and adapts; registry-off behavior stays unchanged.
2. New bin/verify-isolation.sh (operator-side, two AWS profiles A and B that are listed, one unlisted profile C, optional Identity Center profile): A creates a workspace and starts a task; B tries to invoke and stop A's session with A's runtime ARN and session ID and must get AccessDenied; B reads A's task-status and manifest keys directly and through A's access role and must be denied; A's agent (sch task on A's workspace) tries to read B's owner tree and the registry table and must be denied; A's own status/list/dashboard reads work; C's resolve gets HTTP 403 naming the identity to add. The script is read-only apart from its own test workspaces, which it deletes at the end. No account IDs in output.
3. Guides: docs/workspaces.md (enabling, entries, owner mapping, no migration), docs/deploy.md (ISOLATED_PRINCIPALS, deploy flow, adding and removing principals, retained storage and purge, runtime quota, sch destroy), docs/security.md and SECURITY.md (what isolation guarantees, boundary administrators, residual risks X1-X9, Telegram refused, ReadOnlyAccess behavior), docs/getting-started.md (caller permissions: execute-api:Invoke, bedrock-agentcore data-plane on the own plane, sts:AssumeRole on the own access role; deploy-principal permissions for plane stacks). Claim only what tests and simulator output prove; propose examples to the maintainer as AGENTS.md requires instead of inserting them unasked (headless: record them in the task notes).
4. Flip per-principal-isolation.md to "Partially verified" (live check open) and update docs/specs/README.md.
5. Check TASK-20 AC #1 once all subtasks are Done; leave AC #2 (live check) open and TASK-20 In Progress.
6. Suites, bin/verify-docs.sh, journal, masterplan, commit on feat/task-20.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Assumptions (headless run, defaults of TASK-20 applied; TASK-20.4 was Done):
- Verify scripts detect the mode through a new helper, not by parsing sch output: cli/sch/verify_support.py (run by path; subcommands probe, workspace, owner-exec, dashboard) wrapped by bin/lib/verify-target.sh. It is not a sch command. Registry-off stacks keep every script's existing local-index path unchanged; the helper then only reads the IsolationStatus stack output.
- Registry-on stacks (isolated or not): scripts resolve the workspace through the registry before reading the local index (sch mirrors the record there) and send the registry workspace identity as the payload `workspace` (the shim requires it on isolated runtimes). This also fixes the scripts on non-isolated registry stacks, where they sent the logical name.
- Direct invoke payloads in the scripts do not add owner_prefix: R30 accepts a payload without it and the runtime environment stays authoritative. verify-isolation.sh sends it in its positive control, as sch does.
- verify-runtime-iam-tuning.sh was stale since TASK-20.3 (it expected inline capability/base policies). Fixed: attached managed policies are checked, the allow-list is read from the default version of the base managed policy, and the rollback snapshot compares customer managed policies by document (AWS managed ones by version ID). With isolation on it refuses to start without ISOLATED_PRINCIPALS (a deploy without it deletes every plane), simulates every plane execution role and fails if a plane runtime version changes. Spec runtime-capability-tuning R13 amended.
- verify-multi-harness.sh: the legacy-index upgrade checks run in local index mode on every stack (SCH_WORKSPACE_REGISTRY_URL cleared), since that upgrade is a local-index feature; the harness-mutex check also accepts the registry's 409 "harness is immutable".
- verify-remote-ui-tunnel.sh: its offline claude-rejection check runs in local index mode; the target library is sourced only on the live path, so --skip-live still needs no AWS.
- verify-workspace-deletion.sh with isolation on: the local-mode checks are skipped (local index mode is unavailable) and the operator cannot seed objects in owner trees, so it deletes a real workspace through the registry and checks through the access role that no current object remains under the owner-segment checkpoint prefix and writer claim. Object versions and checkpoint-generations/ are not visible to an owner; stated in the script.
- verify-session-image-rebuild.sh with isolation on adds two checks: build sources land under builds/<owner prefix>/, and a write into another owner's builds/ segment is denied.
- Telegram scripts print SKIP and exit 0 when the runtime stack reports IsolationStatus=true (read from the stack, so no registry or listed caller is needed).
- verify-isolation.sh: the in-agent probes run as shell commands in A's microVM through `agentcore exec` (A's own session), not through an LLM prompt: every tool of A's agent runs with those credentials, so this is the agent's upper bound and it is deterministic. A denial counts only on an authorization error (AccessDenied, not authorized, explicit deny, Forbidden); each denied read has a positive control. It is read-only apart from one test workspace per listed principal, deleted at the end (--keep leaves them); 12-digit numbers are masked in all output. The Identity Center profile is optional and checks join and cross-reads against A.
- Test hooks SCH_VERIFY_SCH, SCH_VERIFY_SUPPORT and SCH_TARGET_SUPPORT let cli/tests/test_verify_scripts.py run the scripts against a fake aws/agentcore/sch/helper world that enforces the isolation rules.
- Guides claim only what tests, Access Analyzer and the simulator show, and say the live check has not been run. Deploy-principal and caller permissions are derived from the templates and the CLI calls and are labeled as not verified with least-privilege principals.
- Spec per-principal-isolation.md flipped to Partially verified with the list of what only the live check covers; docs/specs/README.md updated; CHANGELOG entry added under Unreleased/Security.

Proposed documentation examples (AGENTS.md: propose, do not write; headless, so recorded here for the maintainer):
1. docs/deploy.md, Per-principal isolation: an infra/setenv.sh snippet `export ENABLE_WORKSPACE_REGISTRY=true` / `export ISOLATED_PRINCIPALS="user:alice, sso:Developers/bob@example.com"` followed by `sch deploy -s`, with the per-entry summary lines the deploy prints (to be copied from a real run).
2. docs/workspaces.md, Checking a stack: `bin/verify-isolation.sh --profile-a alice --profile-b bob --profile-c carol [--profile-sso dev-bob]` with a trimmed PASS listing from the first live run.
3. docs/getting-started.md, Caller permissions: a minimal identity policy JSON for one listed user (registry invoke ARN, plane runtime ARN pattern, access role ARN), once validated live.
4. docs/workspaces.md: the `sch info` output with the `isolation : on (...)` line.

Verification:
- cli suite 657 OK (new test_verify_support 18 tests; test_verify_scripts 13 tests: verify-isolation.sh passes on an isolated fake world with 41 PASS lines, fails on a join leak, an in-agent read leak and a bucket-policy leak, refuses a non-isolated stack, masks account IDs and deletes its test workspaces; the target library resolves the plane, session, identity and owner keys, is inert on registry-off stacks and stops on an unlisted caller; both Telegram scripts SKIP on isolated stacks; bash -n on every bin/verify-*.sh and bin/lib/*.sh; every script that addresses the runtime or checkpoint keys uses the library).
- infra suite 215 OK; tunnel npm test pass; image-side 534 tests with sandboxed paths: only the 7 known TASK-22 failures, live-session files unchanged (md5 before/after).
- bin/verify-docs.sh passes.
- Not verified: any live run. The scripts were not run against AWS (read-only microVM, no isolated stack exists); bin/verify-isolation.sh is the operator-side live check of TASK-20 AC #2.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Tooling and docs slice of TASK-20. New cli/sch/verify_support.py (probe, workspace, owner-exec, dashboard) and bin/lib/verify-target.sh let every live bin/verify-*.sh address a workspace like sch: unchanged on registry-off stacks; registry session and workspace identity on registry stacks; the caller's plane runtime and access-role reads of owner-segment keys with isolation on. Adapted: headless-tasks, persistence, l2, remote-ui-tunnel, user-provider-keys, git-native, github-access, session-image-rebuild (owner builds/ checks), acp-editor, multi-harness (legacy checks in local index mode, registry 409 accepted), iam-workspace-registry, workspace-deletion (registry deletion checked through the access role), telegram-* (SKIP on isolated stacks), runtime-iam-tuning (fixed for the TASK-20.3 managed policies; requires ISOLATED_PRINCIPALS on isolated stacks, simulates plane roles, guards plane runtime versions). New bin/verify-isolation.sh: two listed principals, one unlisted, optional Identity Center user; join and stop denied with a known session ID, shared runtime refused, cross-owner reads denied from the CLI and from inside A's microVM, 403 for the unlisted caller, own workflow (task, status, list, remote-check, dashboard data path) working; authorization-error matching with positive controls, account IDs masked, test workspaces deleted. Guides (workspaces, deploy, security, getting-started) and SECURITY.md document enabling, entries, owner mapping, no migration, adding/removing principals, retained storage and purge, runtime quota, teardown, caller and deploy-principal permissions, Telegram and ReadOnlyAccess behavior, boundary administrators and residual risks, claiming only test/Access Analyzer/simulator evidence. Spec Partially verified; runtime-capability-tuning R13 amended; CHANGELOG entry. TASK-20 AC #1 checked, TASK-20 stays In Progress. Verified: cli 657 OK (31 new tests, incl. the scripts against a fake world that enforces the rules and injected leaks), infra 215 OK, tunnel pass, image-side only the 7 known TASK-22 failures, verify-docs pass. Not run against AWS; the live check is operator-side (TASK-20 AC #2).
<!-- SECTION:FINAL_SUMMARY:END -->
