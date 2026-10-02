---
id: TASK-28
title: Retag the opt-in GitHub access references that cite TASK-26
status: To Do
assignee: []
created_date: '2026-10-02 12:35'
labels:
  - docs
dependencies: []
priority: low
ordinal: 25000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Code comments, tests, specs and a verify script cite 'TASK-26' for the opt-in GitHub access work (decision-7), an ID from an earlier numbering. On this board TASK-26 is the Opus 5.5 pin and auto-updater fix, so those references point to the wrong task. About 30 places, including image/app/main.py, image/Dockerfile (GitHub CLI sections), image/scripts/harness-wrapper.sh (GH_TOKEN exports), image/app/test_github_access.py, cli/sch/gitnative.py, cli/sch/runtime.py, bin/verify-github-access.sh and docs/specs. Find them with: git grep -n 'TASK-26' | grep -iv 'opus\|updater\|model pin'. Replace them with a reference to decision-7 (opt-in GitHub access).
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 No reference outside .backlog cites TASK-26 for the GitHub access work; they cite decision-7 instead
- [ ] #2 Test suites and bin/verify-docs.sh still pass
<!-- AC:END -->
