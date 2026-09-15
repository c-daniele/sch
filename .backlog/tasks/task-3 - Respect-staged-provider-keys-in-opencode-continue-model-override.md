---
id: TASK-3
title: Respect staged provider keys in opencode continue-model override
status: Done
assignee: []
created_date: '2026-09-14 14:40'
updated_date: '2026-09-14 14:44'
labels: []
dependencies: []
type: bug
ordinal: 6000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
The headless continue-model preservation (TASK-2) routes every non-explicit continue through the unavailable-provider override, which checks only the provider block in the seeded opencode.json (amazon-bedrock alone). Providers that work at runtime via staged API keys (opencode, anthropic, openrouter, kilo) therefore still fall back to the default model on every continue, defeating the preservation for exactly the key-based models it was built for.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 A resumed session whose stored provider is backed by a staged key (or amazon-bedrock) keeps its stored model instead of being overridden
- [x] #2 A resumed session naming a provider with no staged key and no config entry still overrides to the runtime default
- [x] #3 Unit tests cover staged-key preservation, genuine-unavailable override, and key removal mid-life; full shim suite passes
- [x] #4 Spec R8a wording updated to describe key-aware availability
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Shim: make provider-availability check key-aware (_opencode_resume_model or its caller consults _read_staged_provider_keys with SCH_*-to-provider mapping; amazon-bedrock always available via execution role). 2. Tests: staged-key provider preserved, unstaged+unconfigured still overrides, key removed after session creation degrades to override. 3. Docs: update R8a/R10 wording. 4. Verify: targeted + full shim suite, verify-docs.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Availability is now config entries + staged-key providers (SCH_*-to-provider mapping mirroring harness-wrapper.sh) + amazon-bedrock via execution role. Verified: 419 image/app tests OK (incl. 3 new: staged-key preservation with opencode/muse-spark-1.3-contributor-free+xhigh, key-removal fallback, genuine-unavailable override), 564 cli/tests OK, verify-docs.sh PASSED.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
The opencode continue-model override now treats staged-key providers as available: _opencode_available_providers() unions config-file entries, staged SCH_* key mapping, and amazon-bedrock; _opencode_resume_model uses it. Key-based TUI selections (e.g. opencode/muse-spark with OPENCODE_API_KEY staged) are preserved on headless continue instead of overridden; genuinely unavailable providers still fall back to default. Spec R8a updated.
<!-- SECTION:FINAL_SUMMARY:END -->
