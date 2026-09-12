---
id: decision-4
title: Optional features are deploy-time switches with inert defaults
date: '2026-09-02 19:35'
status: accepted
---
## Context

Features like Telegram supervision, IAM capability tuning, user-managed provider keys, and DynamoDB workspace registries extend core functionality. Multiple deploy scripts, separate installers, or dedicated optional stacks add operational complexity and risk configuration drift.

## Decision

Every optional feature must be implemented as a deploy-time switch (in `infra/setenv.sh` and `infra/deploy.sh`) with an inert default posture. When an environment variable or switch is omitted, the feature deploys as disabled without requiring migrations or secondary stacks.

## Consequences

Deployment is unified in a single linear workflow. Deploy switches are re-read on every deployment run; omitted switches deploy as off. The default posture for every optional feature must be explicitly documented alongside the switch.

