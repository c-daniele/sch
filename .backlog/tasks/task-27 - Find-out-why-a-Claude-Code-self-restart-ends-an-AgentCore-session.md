---
id: TASK-27
title: Find out why a Claude Code self-restart ends an AgentCore session
status: To Do
assignee: []
created_date: '2026-10-02 12:35'
labels:
  - claude
  - agentcore
dependencies: []
priority: medium
ordinal: 24000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
TASK-26 removed the trigger seen on 2026-10-02: the 'Newer Opus model available' dialog, whose Yes answer restarts Claude Code. It did not remove the cause. When Claude Code restarts itself inside an AgentCore exec session, the session ends. A local container running the same image keeps the TUI alive, so the AgentCore terminal path is involved. Other restart prompts (for example after settings or plugin changes) may hit the same failure. Approach: on a fresh workspace in a live microVM, set CLAUDE_CODE_DIAGNOSTICS_FILE, force a restart (for example pin an old Opus model in the process env and answer Yes), and read the shutdown entries; compare with a local container.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 The shutdown reason after a Claude Code self-restart in an AgentCore session is captured and recorded
- [ ] #2 Either a fix keeps the session open across a Claude Code restart, or the limitation and known restart triggers are documented in docs/harnesses.md
<!-- AC:END -->
