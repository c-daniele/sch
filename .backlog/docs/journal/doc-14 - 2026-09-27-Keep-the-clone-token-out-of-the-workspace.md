---
id: doc-14
title: 2026-09-27 Keep the clone token out of the workspace
type: other
created_date: '2026-09-27 13:07'
updated_date: '2026-09-27 13:08'
tags:
  - journal
---
# 2026-09-27 Keep the clone token out of the workspace

Task: TASK-10 · Spec: [runtime-image R22](../../../docs/specs/platform/runtime-image.md)

## Problem

A workspace started with `SCH_REPO_URL` and `SCH_REPO_TOKEN` cloned from a URL with the token written into it. Git saves the clone URL as the `origin` remote, so the token sat in plain text in the repository's git config: the agent and every tool it ran could read it, and every S3 checkpoint carried a copy. The token was also visible in the process list while the clone ran. This contradicted the security promise that secrets never enter a workspace.

## What changed

- The clone now always uses a URL without credentials. The token is handed to git by a small credential helper that reads it from the environment of the clone process only, so it never lands in a file, on a command line or in a log line. Any credential helper the machine had configured is switched off for the clone, so nothing can store the token on the side.
- Credentials typed directly into `SCH_REPO_URL` are treated the same way instead of being saved.
- A clone that cannot authenticate fails immediately (no password prompt) and the workspace falls back to an empty repository, as before.
- On every boot, a workspace cloned by an earlier image has the embedded password removed from its saved `origin` URL. Old checkpoints cannot be rewritten, so the guides now tell users of earlier images to rotate the token.
- The seed tests that run the workspace script no longer pick up the developer's own `SCH_REPO_URL` and token.

## Outcome

New tests serve a private repository over local HTTP with password authentication and check that the clone works, that no file in the workspace or home directory contains the token, and that git's own trace of every process it started never shows it. Those tests fail on the previous script and pass now. The rest of the image-side suite is unchanged. An in-image run and a live clone of a real private repository remain operator-side checks.

## Lesson

A credential passed "just for one command" is only safe if you check where the tool copies it afterwards. Git persisted the clone URL silently; it did anonymize the same URL in its reflog, which is easy to miss in both directions. Grepping the whole resulting directory for the secret is a cheap, tool-agnostic test worth adding to any credential-handling change.
