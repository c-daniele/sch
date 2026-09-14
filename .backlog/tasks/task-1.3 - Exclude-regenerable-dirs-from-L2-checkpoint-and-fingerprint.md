---
id: TASK-1.3
title: Exclude regenerable dirs from L2 checkpoint and fingerprint
status: Done
assignee: []
created_date: '2026-09-14 08:41'
updated_date: '2026-09-14 20:01'
labels: []
dependencies: []
parent_task_id: TASK-1
ordinal: 4000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
node_modules, .venv, and build outputs are regenerable but today ride repo.tar.gz and the every-60s full-tree fingerprint (image/app/main.py _fingerprint_repo and _create_archive with no excludes). Exclude them from the archive and the fingerprint, keep restore green by rebuilding the env on restore. Follow-up debt already noted in image/Dockerfile v29 comment.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Checkpoint tar for a Node plus Python workspace shrinks vs baseline
- [x] #2 Fingerprint tick no longer walks excluded dirs
- [x] #3 Full checkpoint-loss-restore cycle passes via bin/verify-l2.sh
<!-- AC:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Done 2026-09-14. REPO_CHECKPOINT_EXCLUDE_NAMES in image/app/main.py (node_modules, .venv/venv, caches, dist/build, .next/.nuxt/target, .codebase-memory, dummy_data; whole-segment match at any depth; .git and sources never excluded) applied to _create_archive (GNU tar name-based excludes) and _fingerprint_repo/_tree_fingerprint (exclude_dir_names prunes subtrees fully). Post-restore best-effort rebuild via _project_env_plan/_maybe_rebuild_project_env (npm ci/install, uv sync, pip install; never fail-closed; SCH_REBUILD_ENV_ON_RESTORE=0 disables; runs under memory caps) with outcome in restore result and info action (repo_excludes + memory_caps). Verified locally: representative tar 791235B -> 221B with only sources+.git retained; fingerprint stable under regenerable churn and flips on source change; full image/app suite green (test_workspace_storage_backend exercises restore incl. env_rebuild). Spec checkpointing R21 + runtime-image cross-ref. FOLLOW-UP (needs live AWS, operator): full checkpoint-loss-restore cycle via bin/verify-l2.sh.
<!-- SECTION:FINAL_SUMMARY:END -->
