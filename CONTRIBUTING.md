# Contributing to SCH

Thanks for your interest in contributing. This document explains how work flows,
what "done" means, and the rules for AI-assisted contributions.

SCH runs AI coding agents in disposable, checkpointed cloud microVMs. It is
maintained by a small team, so the golden rule is: **a contribution should create
more value for the project than it costs to review.**

## Getting started

1. Read the [README](README.md) and the [MANIFESTO](MANIFESTO.md) — the project
   constitution. Decisions that contradict it will be rejected.
2. Skim [AGENTS.md](AGENTS.md) (development workflow) and
   [docs/coding-standards.md](docs/coding-standards.md).
3. Set up your environment (nothing here needs AWS credentials):
   - Python 3.11 or newer (3.11 is the interpreter inside the runtime image,
     3.12 the Lambda runtime), in a project-local virtual environment with the
     `dev` extra — it brings the two packages the `infra/` suite needs
     (`pyyaml`, `boto3`); the CLI itself stays stdlib-only:
     ```bash
     python3 -m venv .venv && . .venv/bin/activate
     pip install -e ".[dev]"          # or: uv venv .venv && uv pip install -e ".[dev]"
     ```
   - Node >= 20 for the tunnel (`tunnel/`): `cd tunnel && npm ci`
   - Only if you will deploy the AWS side: copy `infra/setenv.sh.example` to
     `infra/setenv.sh` (gitignored) and fill in your values.
4. Verify your setup — these are the suites CI runs on every pull request
   ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)):
   ```bash
   (cd cli && python3 -m unittest discover -s tests)
   (cd infra && python3 -m unittest discover)
   (cd tunnel && npm test)
   bash bin/verify-docs.sh
   ```
   The image-side suite (`image/app/test_*.py`) checks the runtime image's
   contract and is run inside the image build (`image/test-local.sh`, needs
   Docker); most of its files also run directly with `python3.11 -m unittest`.

## How work flows

- **Trunk-based**: one `main` branch; short-lived feature branches
  (`feat/<name>`, `fix/<name>`, `docs/<name>`) squash-merged into `main`.
- **Issue first.** Open an issue (or claim an existing one) before writing code,
  except for trivial fixes. Feature work without an agreed issue will be closed.
- **Conventional commits**: `feat:`, `fix:`, `docs:`, `chore:`, `refactor:`,
  `test:`.
- **Definition of done**: tests added/updated, the narrowest relevant suite green
  (`cli/tests`, `infra/test_*`, `tunnel` tests), the relevant `bin/verify-*.sh`
  script passes when the capability's end-to-end behavior changed, and docs
  (`docs/specs/`, the guide under `docs/`, README) updated to match reality.
- **No private data.** Never commit AWS account IDs, credentials, personal
  home paths or e-mail addresses — not in code, not in test fixtures, not in
  Backlog notes. `bin/verify-docs.sh` and the CI secret scan enforce it; use
  `<account-id>` and the AWS documentation sample IDs (`111122223333`) in
  examples and fixtures.
- **Specs are normative.** `docs/specs/` describes required behavior. If your
  change alters behavior, change the spec in the same PR.

## Reporting bugs

Open a GitHub issue with: what you ran (exact command), what you expected, what
happened, and environment details (OS, CLI version, harness). Redact secrets,
account IDs, and tokens — issues are public.

## AI-assisted contributions

AI-assisted contributions are welcome **when they reduce work for the project
rather than transferring verification work to maintainers.** This project is
itself a tool for running coding agents, so we hold AI contributions to a clear
bar instead of banning them.

Before opening a pull request with AI assistance:

1. Start from an **approved, open issue** assigned to you.
2. **Disclose** AI use in the PR template (`AI_USE: none | assisted | generated`,
   plus the tool and which areas were AI-generated).
3. Provide **reproducible test evidence** (test output for the suites above).
4. Keep the change **small and focused**; one concern per PR.
5. **Read and understand the complete diff.** You must be able to explain every
   line — review questions are answered by the human contributor, personally.
6. Have **no other open PR** at the same time.

Pull requests will be closed without review when: no linked issue, missing
disclosure, missing test evidence, failing CI, duplicated work, the contributor
cannot explain the implementation, or the expected review cost exceeds the value
to the project. Repeated noncompliant or automated submissions may result in
being blocked from the repository.

We follow the spirit of the
[LLVM AI tool policy](https://llvm.org/docs/AIToolPolicy.html): unreviewed agent
output that transfers design and review labor to maintainers is an extractive
contribution, and extractive contributions are rejected regardless of quality.

## Code of conduct

By participating you agree to the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Security

Do not open public issues for security vulnerabilities. See
[SECURITY.md](SECURITY.md).
