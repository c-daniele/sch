---
id: decision-7
title: Opt-in GitHub access via provider-keys transport and tmpfs credential store
date: '2026-09-05 04:57'
status: accepted
---
## Context

Git-native mode forbade any git credential in the microVM and seeded clones without an `origin`, so long autonomous runs could not push branches or drive `gh`. The provider-keys pipeline (`~/.sch/env` allowlist, per-invocation transport, tmpfs staging with total replacement, names-only diagnostics) already solved the adjacent problem for model credentials.

## Decision

Reuse the provider-keys pipeline for `GITHUB_TOKEN` instead of building a parallel credential channel. The token value lives only on tmpfs (`/run/sch`); the checkpointed repo config carries just a credential-free `store` helper pointer plus an `schManaged` origin marker. The recorded `originUrl` is bound at seed like the branch. Operator-owned git config is never modified. `gh` ships pinned in the image and authenticates only via the staged environment, never via persisted login state.

## Consequences

The default stays credential-less with no `origin`. Token rotation and withdrawal work through staging replacement plus per-invocation reconciliation. The feature is GitHub-only by construction (non-GitHub origins are dropped at sanitize time). Live verification needs an image rebuild, which resets session storage, and is tracked separately.

