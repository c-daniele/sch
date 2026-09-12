---
id: decision-6
title: >-
  Use managed AWS MCP Server via pinned proxy instead of self-hosted aws-api
  server
date: '2026-09-05 04:22'
status: accepted
---
## Context

The harness pinned self-hosted `awslabs.aws-api-mcp-server` 1.3.46. AWS declared it superseded by the managed AWS MCP Server (end of development 2026-07-15, removal 2027-07-15; it depends on AWS CLI v1, also entering maintenance mode). Keeping it meant permanent FastMCP-3/MCP-1 constraint pinning (TASK-21), a 440MB baked embedding model, and a frozen tool shape.

## Decision

Reach the managed AWS MCP Server through `mcp-proxy-for-aws-cli`, pinned like every other image tool (1.6.5, `ARG MCP_PROXY_VERSION`; endpoint `ARG AWS_MCP_ENDPOINT`, default eu-central-1). Seed key renamed `aws-api` to `aws-mcp`, `--read-only`, operation region via `--metadata AWS_REGION`, no env block. IAM remains the enforcing boundary.

## Consequences

Drops the constraints file and the embedding bake; agents see the managed tool shape (`run_script` family) instead of `call_aws`. Pre-existing workspaces keep the old seed until the operator deletes the config. First promoted deploy must verify `opencode mcp list` live (egress plus IMDS credentials).

