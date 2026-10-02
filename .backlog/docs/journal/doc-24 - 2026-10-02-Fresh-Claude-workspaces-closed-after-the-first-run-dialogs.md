---
id: doc-24
title: 2026-10-02 Fresh Claude workspaces closed after the first-run dialogs
type: other
created_date: '2026-10-02 12:37'
updated_date: '2026-10-02 12:37'
tags:
  - journal
---
# 2026-10-02 Fresh Claude workspaces closed after the first-run dialogs

Task: TASK-26. Decision: decision-16. Spec: `docs/specs/platform/runtime-image.md` (R12, R12a, R12b, R15).

## Problem

The first interactive Claude Code launch in a new workspace closed the remote session a few seconds after the theme and trust dialogs. Later launches in the same workspace worked. The image pinned the Opus alias to Opus 5, but the pinned Claude Code (2.1.285) treats Opus 5.5 as the Bedrock Opus target. On a fresh workspace it asked "Newer Opus model available ... Claude Code will restart to apply". Accepting restarted Claude Code, and in an AgentCore session that restart ended the session. Separately, every start tried to self-update Claude Code, which the runtime user cannot do, and showed an "Auto-update failed" warning.

## What changed

- The Opus alias now points to Opus 5.5 (EU profile), and the Global row of the model picker points to Opus 5.5 (Global). Claude Code's self-updater is turned off. All three places that define the Claude launch environment carry the same values: the image environment, the login-shell profile and the harness wrapper. In each, an operator's own value still wins.
- A new build-time check reads the model catalog inside the installed Claude Code binary. It fails the image build when an Opus, Sonnet or Haiku pin is older than the model Claude Code would offer to switch to. A future Claude Code bump therefore cannot bring the dialog back unnoticed.
- `docs/harnesses.md` now explains that the default Opus alias needs Bedrock access to Opus 5.5, and how to override the pin per workspace or per image.

## Outcome

With the old pins, a pty-driven fresh launch of the real binary reproduced the dialog. With the new environment it showed no dialog, no restart and no update attempt, and the TUI stayed at the prompt for two minutes. A headless run used Opus 5.5. A workspace override to Opus 5 still won and triggered no dialog. Unit suites and the documentation check pass.

No image could be built from this session, so the in-image test (`image/test-local.sh`, section 10a) and a live `sch run` on a fresh workspace remain operator-side. The development account's execution role cannot call Opus 5.5 yet. A deployment in that situation needs model access, or an Opus override, before the new default works.

Why a Claude Code restart ends an AgentCore session is still unknown. It is tracked in TASK-27, because other restart prompts could hit it too. TASK-28 cleans up older code references that cite "TASK-26" for the GitHub access work, which belongs to decision-7.

## Lesson

The dialog does not compare a pin with the newest model Claude Code knows. It compares it with the tier's Bedrock alias target in the baked catalog: Sonnet 4.6 passes even though Sonnet 5.5 is known. Reading the catalog was not enough to establish this. A few discriminating fresh launches of the real binary settled it, and the build check encodes that observed rule rather than a guess. When a vendor tool decides something on first run, probe it with a fresh config before writing a guard for it.
