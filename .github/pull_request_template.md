## Problem and approval

APPROVED_ISSUE: #<issue number — required; "none" only for trivial doc/typo fixes>

Explain the problem this solves:

Explain why this implementation was chosen:

## AI use

AI_USE: none | assisted | generated
AI_TOOL:
AI_GENERATED_AREAS:

## Evidence

- [ ] Tests added or updated
- [ ] Narrowest relevant suite green (`cli/tests`, `infra/test_*`, `tunnel` tests) — paste or link output
- [ ] Relevant `bin/verify-*.sh` passes (if end-to-end behavior changed)
- [ ] `docs/specs/` updated to match the new behavior (if behavior changed)
- [ ] I have read and can explain every line of this diff

## Checklist

- [ ] Conventional commit message(s)
- [ ] No secrets, AWS account IDs, or credentials in the diff
- [ ] No other open PR from me
