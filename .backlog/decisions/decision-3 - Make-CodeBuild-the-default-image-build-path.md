---
id: decision-3
title: Make CodeBuild the default image build path
date: '2026-09-02 19:35'
status: accepted
---
## Context

Building the ARM-based runtime container image locally required local Docker/Colima daemon setup, slow emulation on x86_64 machines, and corporate CA certificate handling. An earlier architecture had circular dependencies between image builds and runtime CloudFormation stacks.

## Decision

Make AWS CodeBuild the default image build path in `infra/deploy.sh`, powered by an independent bootstrap stack (`infra/bootstrap.yaml`) created before the runtime stack. Retain local image building via `-l` / `--local` as an opt-in path for corporate proxy / custom CA environments.

## Consequences

Standard deployments run without local container engines or ARM hardware. CodeBuild runs in AWS under an isolated IAM build role. Building via CodeBuild no longer grants `ENABLE_SESSION_IMAGE_REBUILD` permissions to the runtime agent execution role.

