---
id: decision-5
title: Retire OpenSpec in favor of domain specs under docs/specs
date: '2026-09-02 19:35'
status: accepted
---
## Context

OpenSpec originally tracked capability specifications and change proposals under `openspec/`. Maintaining two parallel specification trees (`openspec/` and `docs/specs/`) caused ambiguity, duplicate sources of truth, and confusion for AI coding agents.

## Decision

Retire OpenSpec completely. Product specifications live under `docs/specs/<domain>/<capability>.md` as normative sources of truth. Tag the last commit containing the `openspec/` directory as `pre-openspec-retirement` before deleting the directory.

## Consequences

`docs/specs/` is the single source of truth for behavior. OpenSpec tools, skills, and configuration rules are removed. Historical change proposals and original specs can be inspected via `git show pre-openspec-retirement:openspec/<path>`.

