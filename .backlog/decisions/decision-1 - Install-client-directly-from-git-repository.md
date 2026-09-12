---
id: decision-1
title: Install client directly from git repository
date: '2026-09-02 19:35'
status: accepted
---
## Context

SCH was packaged as a standard Python package in TASK-7. Users need a reliable method to install the `sch` CLI onto their system `PATH` via tools like `pipx` or `uv tool`, without depending on third-party package registry indexing during pre-release development.

## Decision

Install the client CLI directly from the git repository using `pipx install git+<repo-url>[@tag]`, using git release tags for version pinning. All standard packaging definitions (`pyproject.toml`, console scripts) remain intact and stdlib-only.

## Consequences

Installation requires `git` and `pipx` (or `uv`). Distribution is directly coupled to tagged commits in the repository. Users can install release tags (e.g. `@v0.1.0`) or development branches. PyPI publication remains possible with zero code changes.
