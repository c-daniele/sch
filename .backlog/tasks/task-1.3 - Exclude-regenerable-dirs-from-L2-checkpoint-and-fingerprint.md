---
id: TASK-1.3
title: Exclude regenerable dirs from L2 checkpoint and fingerprint
status: To Do
assignee: []
created_date: '2026-09-14 08:41'
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
- [ ] #1 Checkpoint tar for a Node plus Python workspace shrinks vs baseline
- [ ] #2 Fingerprint tick no longer walks excluded dirs
- [ ] #3 Full checkpoint-loss-restore cycle passes via bin/verify-l2.sh
<!-- AC:END -->
