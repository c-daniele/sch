---
id: TASK-5
title: Preserve authenticated OpenCode model on headless continue
status: Done
assignee: []
created_date: '2026-09-16 10:06'
updated_date: '2026-09-16 10:17'
labels: []
dependencies: []
type: bug
ordinal: 8000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
A model selected in an OpenCode TUI can be usable through persisted provider authentication while absent from the seeded provider config and staged API-key set. The continue-model guard currently classifies that provider as unavailable and replaces the selected model with the Bedrock default, so a handed-off GitHub Copilot session using github-copilot/gpt-5.6-sol changes model when resumed with sch task --continue.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 An OpenCode continuation keeps a stored model whose provider has a valid persisted auth.json entry
- [x] #2 A provider absent from config, staged keys, and persisted authentication still falls back to the runtime default
- [x] #3 Tests cover GitHub Copilot OAuth credentials, malformed auth data, and unauthenticated fallback
- [x] #4 The normative headless-task specification describes persisted provider authentication as an availability source
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Extend the shim provider-availability calculation with validated top-level provider IDs from the workspace OpenCode auth.json. 2. Add focused regression coverage for github-copilot OAuth, malformed auth data, and genuine unavailability while retaining staged-key behavior. 3. Update headless-task-execution R8a and run shim plus documentation verification.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Root cause confirmed against pinned OpenCode 1.18.31: provider credentials from /connect and auth login are stored in $XDG_DATA_HOME/opencode/auth.json keyed by provider ID. The shim availability union omitted this persisted credential channel, so github-copilot was replaced with the Bedrock default despite being usable. Implemented auth-aware availability and focused regression coverage; targeted tests pass.

Verification: focused task/session tests passed (47); full image/app shim suite passed (459); full CLI suite passed (586); bin/verify-docs.sh passed; git diff --check passed. Live AWS end-to-end was not run because it requires deploying a new runtime image and mutating an active workspace.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
OpenCode headless continuation now treats valid persisted auth.json credentials as a provider-availability source. A GitHub-connected session using github-copilot/gpt-5.6-sol therefore keeps its stored model and variant instead of being replaced by the Bedrock default; malformed or absent credentials retain the safe fallback. Added regression and storage-path tests, updated R8a, and corrected stale runtime-version values. Verified with 459 shim tests, 586 CLI tests, documentation checks, and diff checks.
<!-- SECTION:FINAL_SUMMARY:END -->
