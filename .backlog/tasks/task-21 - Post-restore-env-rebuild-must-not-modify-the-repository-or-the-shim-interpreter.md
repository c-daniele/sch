---
id: TASK-21
title: >-
  Post-restore env rebuild must not modify the repository or the shim
  interpreter
status: Done
assignee: []
created_date: '2026-09-27 10:19'
updated_date: '2026-09-27 13:33'
labels: []
dependencies: []
references:
  - image/app/main.py
  - image/app/test_memory_footprint.py
  - docs/specs/workspace-lifecycle/workspace-checkpointing.md
  - docs/deploy.md
priority: medium
type: bug
ordinal: 13000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
## Problem

TASK-1.3 keeps regenerable directories (`.venv`, `node_modules` and others in `REPO_CHECKPOINT_EXCLUDE_NAMES`) out of the checkpoint and rebuilds them after an L2 restore (`_project_env_plan` and `_maybe_rebuild_project_env` in `image/app/main.py`; spec workspace-checkpointing R21). The rebuild commands have side effects on the user's repository and on the image:

1. `uv sync` on a project with `pyproject.toml` but no `uv.lock` writes a new `uv.lock` into the repository.
2. `uv sync` with a stale, tracked `uv.lock` rewrites it, changing dependency pins nobody asked to change.
3. `npm install` without `package-lock.json` writes `package-lock.json`.
4. The rebuilt `.venv` holds only the default dependencies, not optional extras. A workspace whose environment was built with, for example, `pip install -e ".[dev]"` silently loses those packages, and the rebuild still reports success.
5. `pip install -r requirements.txt` runs with the shim's own interpreter (`sys.executable`, the system `python3.11`, no virtual environment). The system site-packages is not writable by `sch` and the interpreter has no `EXTERNALLY-MANAGED` marker, so pip falls back to the user site (`/home/sch/.local/lib/python3.11/site-packages`). That directory is on the path of the interpreter the shim and the image scripts use, so the project's pins can shadow the shim's own dependencies. It also breaks the image rule of never installing into the system interpreter.

## Why it matters

- After every cold restore, `git status` of the workspace differs from what was checkpointed.
- An agent that runs `git add -A` commits the stray files.
- In git-native mode, `sch fetch` triggers the shim's `git-snapshot` action. When the worktree is dirty, that action makes a service commit (`git add -A` in `_handle_git_snapshot`), so the stray files land in the branch the operator imports.
- Lockfile rewrites change dependency versions silently.

## Evidence

- Observed on 2026-09-27 in the SCH repository workspace. After the microVM restarted on idle timeout, the post-restore rebuild ran `uv sync`: it created an untracked `uv.lock` (09:48 UTC) and a `.venv` without the `dev` extras, which the operator had installed (`import yaml` then failed).
- Reproduced in a throwaway directory:
  - `uv sync` in a project with only `pyproject.toml` leaves `?? uv.lock`.
  - With a stale, committed `uv.lock`, `uv sync` leaves ` M uv.lock`.
  - `uv sync --frozen` without a lockfile refuses and writes nothing.
  - `npm install` without a lockfile creates `package-lock.json`.

## Expected outcome

- After an L2 restore the repository content is exactly what was checkpointed, apart from the excluded environment directories.
- The rebuild installs from existing lockfiles without modifying them.
- When no lockfile exists, the rebuild does not create one in the repository. It either skips with a logged reason or builds the environment without writing to the repository: the worker decides and documents the choice.
- No rebuild step installs into the shim's interpreter.
- The reported outcome says what was rebuilt, what was skipped, and why.
- The `SCH_REBUILD_ENV_ON_RESTORE=0` escape hatch and the never-fail-closed contract stay.

## Constraints for the worker

- Image-side tests import `image/app/main.py`, which deletes the live `/tmp/sch-telegram-enabled` marker; `invoke()` rewrites `/run/sch/provider-keys.env`. When running inside a live SCH session, point `SCH_TELEGRAM_ENABLED_MARKER`, `SCH_PROVIDER_KEYS_FILE`, `SCH_CHECKPOINT_TMP_DIR` and `SCH_WORKSPACE_ROOT` at a temporary directory.
- Tests must not depend on network installs: use fake runners or dependency-free probe projects.
- Work on `fix/post-restore-rebuild-side-effects` from `dev`, with conventional commits. Never push. Do not commit `uv.lock`, `.venv/`, or the intentionally untracked TASK-10 file.
- No AWS access is needed.

## Out of scope

- Changing the exclude list itself.
- Reproducing arbitrary custom install commands. Document that they are not reproduced.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 After an L2 restore the rebuild leaves git status --porcelain exactly as it was at checkpoint time and never rewrites a tracked file; a test compares the worktree before and after the rebuild
- [x] #2 A project with a lockfile is rebuilt from it without changing it (uv and npm cases); a project without a lockfile is handled without writing a lockfile into the repository, and the chosen behavior is logged
- [x] #3 No rebuild step installs packages into the shim's interpreter or its user site; requirements.txt projects go into a project-local virtual environment or are skipped
- [x] #4 The rebuild outcome reported by the shim info action and by sch status names what was rebuilt, what was skipped and why (for example, a missing lockfile)
- [x] #5 SCH_REBUILD_ENV_ON_RESTORE=0 still disables the rebuild, and a rebuild failure never fails the restore
- [x] #6 Spec workspace-checkpointing R21 and the operator docs describe the new behavior, including that optional extras and custom install commands are not reproduced automatically
- [x] #7 Tests cover uv, npm and pip projects with and without lockfiles without network access, and the image-side suite passes with live-session paths redirected
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Replace the rebuild plan with lockfile-only steps: npm ci (package-lock/shrinkwrap), uv sync --frozen (uv.lock), uv venv .venv + uv pip install --python .venv (requirements.txt); no lockfile -> skip with a logged reason (never npm install / uv sync without --frozen).
2. Scrub env (VIRTUAL_ENV, UV_PROJECT_ENVIRONMENT, PIP_USER...) so nothing targets the shim interpreter.
3. Worktree guard: snapshot git status + lockfile bytes before, restore stray changes after, report worktree state.
4. Structured outcome (status, per-env steps with reasons, worktree) in ENV_REBUILD_STATE; exposed by info (checkpoint.env_rebuild) and sch status --live.
5. Tests: plan, fake runners, git before/after comparison, real offline uv/npm probes; update spec R21, deploy.md, harnesses.md.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Decision: no lockfile -> skip with reason no-lockfile (not an unlocked build): an unlocked install cannot reproduce the checkpointed versions and npm install / uv sync write lockfiles; setuptools editable installs can also drop egg-info in the repo.
Python requirements.txt projects: uv venv .venv + uv pip install --python .venv/bin/python (uv only; no python -m venv fallback, uv is pinned in the image).
Guard: snapshot root manifests/lockfiles + git status before; afterwards restore lockfiles, delete new untracked files, git checkout tracked files that were clean; paths under REPO_CHECKPOINT_EXCLUDE_NAMES are ignored by the comparison so a non-gitignored node_modules is kept.
Outcome: ENV_REBUILD_STATE -> info checkpoint.env_rebuild {status, steps[env,label,result,reason], summary, worktree}; sch status --live renders an env_rebuild line (live only; the offline S3 status does not carry it). New overall statuses: rebuilt-partial, skipped-no-lockfile, skipped.
Validation: image/app test_env_rebuild (25 tests incl. real offline uv/npm probes and git before/after comparison) + test_memory_footprint pass; full image suite with SCH_TELEGRAM_ENABLED_MARKER/SCH_PROVIDER_KEYS_FILE/SCH_CHECKPOINT_TMP_DIR/SCH_WORKSPACE_ROOT redirected: 505 tests, only the 7 pre-existing TASK-22 failures; cli suite 595 OK; bin/verify-docs.sh passes. Git identity was not configured in the VM; commits use sch-agent via -c.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
The post-restore env rebuild is now lockfile-only and repo-neutral: npm ci (package-lock/shrinkwrap), uv sync --frozen (uv.lock), or a project-local .venv via uv for requirements.txt; projects without a lockfile are skipped with reason no-lockfile. Interpreter-redirecting variables are scrubbed, nothing touches the shim interpreter. A guard restores lockfiles and reverts stray repo changes so git status equals the checkpointed state. The outcome (status, per-env result/reason, worktree verdict) is in info checkpoint.env_rebuild and on the sch status --live env_rebuild line. SCH_REBUILD_ENV_ON_RESTORE=0 and never-fail-closed kept. Spec R21, deploy.md and workspaces.md updated. Verified by new offline tests (fake and real uv/npm, git before/after), the full image suite with live paths redirected (only TASK-22 pre-existing failures) and the CLI suite. Not verified in a live microVM restore.
<!-- SECTION:FINAL_SUMMARY:END -->
