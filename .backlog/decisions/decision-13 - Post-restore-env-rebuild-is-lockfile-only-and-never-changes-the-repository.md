---
id: decision-13
title: Post-restore env rebuild is lockfile-only and never changes the repository
date: '2026-09-27 13:33'
status: accepted
---
## Context

Environment directories (`node_modules`, `.venv`, ...) are not checkpointed and
are rebuilt after an L2 restore (workspace-checkpointing R21). The first
version ran `npm install`, `uv sync` and `pip install -r` with the shim's own
interpreter. After a restart it wrote an untracked `uv.lock` into the SCH
repository, could rewrite committed lockfiles, and could install project pins
into the user site of the interpreter the shim itself runs on. In git-native
mode those stray files end up in the service commit made by `sch fetch`
(TASK-21).

## Decision

The rebuild only installs from an existing lockfile and only into
project-local directories: `npm ci`, `uv sync --frozen`, or a project-local
`.venv` built with `uv` for `requirements.txt`. A project without a lockfile
is skipped with the reason `no-lockfile` instead of resolved afresh. A guard
puts back any change a rebuild step makes to the repository, so `git status`
after the rebuild equals the checkpointed state. Optional extras, other
package managers and custom install commands are not reproduced.

## Consequences

- A restore never changes the repository or dependency pins, and never
  touches the shim's interpreter.
- Projects without a lockfile, and projects that rely on extras such as
  `.[dev]`, need a manual install after a cold restore. The outcome (shim
  `info` `checkpoint.env_rebuild`, `sch status --live`) says which env was
  skipped and why.
