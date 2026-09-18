---
id: doc-8
title: 2026-09-16 Preserve authenticated OpenCode model on headless continue
type: other
created_date: '2026-09-16 10:16'
updated_date: '2026-09-16 10:16'
tags:
  - journal
---
# 2026-09-16 Preserve authenticated OpenCode model on headless continue

## Problem

A headless OpenCode continuation could replace a model selected in the TUI with the runtime default. The guard that decides whether the stored provider can run recognized seeded configuration, staged API keys, and Bedrock IAM credentials, but not provider credentials created by OpenCode itself. GitHub Copilot therefore looked unavailable even after a successful GitHub connection.

## What changed

See task TASK-5 and headless-task-execution rule R8a. Provider availability now includes valid entries in the workspace OpenCode credential store, in addition to the existing sources. Invalid credential data is ignored, and providers with no usable authentication still fall back to the runtime default.

## Outcome

A continued GitHub-connected session keeps github-copilot/gpt-5.6-sol and its selected reasoning variant. The full shim and CLI suites and documentation checks pass. Live AWS verification still requires deploying the rebuilt runtime image.

## Lesson

Any availability decision must account for every authentication channel the harness itself supports. Persisted OAuth credentials are distinct from both static provider configuration and invocation-time API keys.
