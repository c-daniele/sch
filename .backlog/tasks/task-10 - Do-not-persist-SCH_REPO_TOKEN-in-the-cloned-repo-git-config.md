---
id: TASK-10
title: Do not persist SCH_REPO_TOKEN in the cloned repo git config
status: Done
assignee: []
created_date: '2026-09-26 20:46'
updated_date: '2026-09-27 13:08'
labels:
  - security
dependencies: []
references:
  - image/scripts/init-workspace.sh
  - docs/cli.md
  - SECURITY.md
priority: high
type: bug
ordinal: 11000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
When a workspace starts with `SCH_REPO_URL` and `SCH_REPO_TOKEN` set, `image/scripts/init-workspace.sh` clones from `https://x-access-token:<token>@host/...`. Git saves that URL as the `origin` remote in `/mnt/workspace/repo/.git/config`, so the token stays in plain text inside the workspace: the agent and every tool it runs can read it, and it is copied into every S3 checkpoint because `.git` is part of the checkpointed worktree. This breaks the SECURITY.md posture that secrets are never stored in workspaces. While `git clone` runs, the token is also visible in the process list because it is part of the command line. Checkpoints taken before the fix still contain the token, so affected users must rotate it. Related test hygiene: the container-free seed tests that run init-workspace.sh (test_telegram_seeding.py, test_claude_workspace_seed.py, test_pi_workspace_seed.py) inherit the developer environment, so a set `SCH_REPO_URL` makes them clone with the developer token; test_opencode_workspace_seed.py already drops both variables. Found during the TASK-9 pre-commit review. Keep this task out of the public repository until the fix ships (SECURITY.md: no public disclosure of unfixed vulnerabilities); commit it together with the fix.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 After a first-boot clone with `SCH_REPO_URL` and `SCH_REPO_TOKEN`, no file in the workspace contains the token, and `origin` points at the token-free URL
- [x] #2 The token never appears on a command line or in a log line during the clone
- [x] #3 Cloning a private repository with a token still works, and a failed clone still falls back to an empty repository
- [x] #4 A workspace cloned by an earlier image has the embedded credentials removed from its saved `origin` URL at the next boot
- [x] #5 Tests cover the above, the seed tests no longer inherit `SCH_REPO_URL`/`SCH_REPO_TOKEN` from the developer environment, and `docs/cli.md` tells users of earlier images to rotate the token
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Clone with a token-free URL; hand SCH_REPO_TOKEN (and any credentials embedded in SCH_REPO_URL) to git through an inline credential helper that reads them from the environment, with other credential helpers reset and terminal prompts disabled.
2. Log only the sanitized URL.
3. On every boot of an existing worktree, strip embedded user:password credentials from origin url/pushurl.
4. Tests: local dumb-HTTP server with Basic auth, git argv recorder, grep of the workspace, failed-clone fallback, migration; drop SCH_REPO_URL/SCH_REPO_TOKEN in the seed tests.
5. Docs: docs/cli.md rotation note, runtime-image R22, SECURITY.md check, CHANGELOG.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Design: the clone URL is always credential-free; SCH_REPO_TOKEN (or a user:password embedded in SCH_REPO_URL, percent-decoded) reaches git through an inline credential helper that reads SCH_CLONE_USERNAME/SCH_CLONE_PASSWORD from the clone process environment. An empty credential.helper resets configured helpers (so a global 'store' cannot save the token); GIT_TERMINAL_PROMPT=0 and empty GIT_ASKPASS make an unauthenticated clone fail fast into the empty-repo fallback. Existing worktrees: remote.origin.url/pushurl values with an embedded password are rewritten via --unset-all/--add (no old value on argv); username-only URLs are left alone. Git already anonymizes the URL in the clone reflog, so .git/config was the only file carrying the token.
Verification: image/app/test_repo_bootstrap.py (8 tests; local dumb-HTTP server with Basic auth, GIT_TRACE capturing argv of git and every subprocess, byte grep of workspace and HOME) passes; 4 of its tests fail against the pre-fix script. Full image-side suite with SCH_TELEGRAM_ENABLED_MARKER, SCH_PROVIDER_KEYS_FILE and SCH_WORKSPACE_ROOT under a temporary directory: 488 tests, the same 7 pre-existing environment-dependent failures before and after the change (tracked in TASK-22). bin/verify-docs.sh passes. Not verified: an in-image run (image/test-local.sh needs Docker) and a live clone of a real private repository; both are operator-side.
Note: the task file was already committed in 37f7691 before the fix; the squash-merge ships it together with the fix. Nothing was pushed.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
init-workspace.sh no longer embeds SCH_REPO_TOKEN in the clone URL: the token reaches git through an environment-reading credential helper, origin stays token-free, no token appears on a command line or log line, and existing worktrees have embedded passwords scrubbed from origin at every boot. Failed clones still fall back to an empty repo. Added test_repo_bootstrap.py; the seed tests drop SCH_REPO_URL/SCH_REPO_TOKEN; runtime-image R22, docs/cli.md, docs/security.md and CHANGELOG document the behavior and the need to rotate tokens used with earlier images. Verified with the new tests (which fail on the old script), the full image-side suite (no new failures) and bin/verify-docs.sh.
<!-- SECTION:FINAL_SUMMARY:END -->
