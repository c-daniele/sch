---
id: TASK-23
title: >-
  Early checkpoint action can write into the root before the restore (isolation
  off)
status: To Do
assignee: []
created_date: '2026-09-27 16:06'
labels:
  - bug
  - checkpointing
dependencies: []
priority: medium
ordinal: 20000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Found by TASK-20.2. On a runtime without SCH_OWNER_PREFIX, a checkpoint action (sch stop) that arrives while the bootstrap restore is still running calls _do_checkpoint immediately. With a local opencode.db present it writes state/ (DB backup, mount marker) into the empty workspace root, which makes _mount_storage_empty() false and can skip the L2 restore, and the pass can publish a manifest of a not-yet-restored workspace. Reproduced offline with the NoEarlyWritesTests harness of image/app/test_owner_prefix.py run with the prefix unset. Plane runtimes already wait for readiness and answer skipped-not-ready (workspace-checkpointing R10a). Decide whether registry-off runtimes get the same gate (behavior change for sch stop during boot) and update workspace-checkpointing R10a accordingly.
<!-- SECTION:DESCRIPTION:END -->
