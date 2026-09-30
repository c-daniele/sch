---
id: doc-15
title: 2026-09-27 Post-restore env rebuild no longer changes the repository
type: other
created_date: '2026-09-27 13:33'
updated_date: '2026-09-27 13:33'
tags:
  - journal
---
# 2026-09-27 Post-restore env rebuild no longer changes the repository

Task: TASK-21. Spec: [workspace-checkpointing](../../../docs/specs/workspace-lifecycle/workspace-checkpointing.md) R21. Decision: decision-13.

## Problem

Checkpoints leave out environment directories such as `node_modules` and `.venv`, and the shim rebuilds them after a cold restore. The rebuild used commands that write into the repository. On 2026-09-27, after the SCH workspace restarted on idle timeout, `uv sync` created an untracked `uv.lock` and a `.venv` without the `dev` extras the operator had installed. The same commands could rewrite a committed lockfile (`uv sync` with a stale lock), create `package-lock.json` (`npm install`), and install a `requirements.txt` into the user site of the interpreter the shim runs on. In git-native mode, `sch fetch` commits a dirty worktree, so these stray files would reach the operator's branch.

## What changed

- The rebuild installs only from an existing lockfile: `npm ci` for an npm lockfile, `uv sync --frozen` for `uv.lock`, and a project-local `.venv` built with `uv` for `requirements.txt`. A project without a lockfile is skipped with the reason `no-lockfile`.
- Variables that could point an install at another interpreter (`VIRTUAL_ENV`, `UV_PROJECT_ENVIRONMENT`, `PIP_USER`, ...) are removed from the rebuild environment.
- A guard records the lockfiles and `git status` before the rebuild. Afterwards it puts back lockfiles, deletes new untracked files and restores tracked files that the rebuild changed. Files under the excluded environment directories are not compared.
- The shim `info` response (`checkpoint.env_rebuild`) and `sch status --live` now report what was rebuilt, what was skipped and why, and whether the worktree stayed unchanged.
- The spec and the operator guides say that optional extras and custom install commands are not reproduced.

## Outcome

After a restore, the repository matches the checkpoint apart from the environment directories. New offline tests cover npm, uv and `requirements.txt` projects with and without lockfiles. Some use fake tools that deliberately write into the repository, and some run the real `uv` and `npm` on probe projects with no dependencies. The image-side suite passes with the live-session paths redirected, except for the failures already tracked in TASK-22. A restore in a live microVM has not been run yet.

## Lesson

A recovery step that runs automatically must not have side effects the user did not ask for. Before running an install tool unattended, check whether it writes lockfiles or build metadata. Then choose the mode that does not write them (`--frozen`, `ci`), and check the result against the state before the step instead of trusting the tool.
