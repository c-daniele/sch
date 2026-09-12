---
id: decision-2
title: Defer PyPI publication
date: '2026-09-02 19:35'
status: accepted
---
## Context

Publishing the `sch` package to PyPI requires finalizing the project repository name, owner namespace, and release pipeline. During pre-release development, publishing premature versions to the global index creates registry pollution and name reservation overhead.

## Decision

Defer publication to PyPI until after the repository name and ownership are final and the open-source release is tagged. Distribute through the git-install channel in the interim while keeping package metadata PyPI-ready.

## Consequences

No package registration or release management overhead on PyPI during active early iterations. The installation workflow uses `pipx install git+...` without loss of functionality.
