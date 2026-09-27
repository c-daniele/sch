---
id: TASK-22
title: Make the image-side suite pass out of container
status: To Do
assignee: []
created_date: '2026-09-27 13:07'
labels:
  - testing
dependencies: []
priority: low
type: bug
ordinal: 14000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Running image/app/test_*.py directly with python3.11 (SCH_TELEGRAM_ENABLED_MARKER, SCH_PROVIDER_KEYS_FILE and SCH_WORKSPACE_ROOT pointed at a temporary directory) fails 7 tests independently of TASK-10: test_db_backup_wal (host SQLite no longer fails the WAL-header re-open premise), five test_permission_hook cases (hook produces no JSON / no approval dir, 8 s latency), and test_user_provider_keys.test_the_staging_path_is_outside_every_checkpointed_root (asserts the default path but reads SCH_PROVIDER_KEYS_FILE from the ambient environment, against the hermetic-test rule). Found while verifying TASK-10.
<!-- SECTION:DESCRIPTION:END -->
