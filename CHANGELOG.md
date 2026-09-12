# Changelog

All notable changes to SCH are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is
[Semantic Versioning](https://semver.org/) (pre-1.0: expect breaking changes in
minor versions).

## [Unreleased]

### Added

- Agent-ready documentation set: `MANIFESTO.md` (project constitution),
  `AGENTS.md` hub, `.backlog/` working system (masterplan, journal,
  brainstorming), rationalized normative specs under `docs/specs/`, and
  `docs/coding-standards.md`.
- Open-source community files: Apache-2.0 license, contributing guide with
  AI-assisted contribution policy, security policy, code of conduct, support
  guide, and issue/PR templates.
- PyPI distribution for the client CLI: the package `sch` (stdlib-only,
  Python >= 3.8) installs a `sch` console script on PATH via pip/pipx/uv on
  macOS, Linux, and Windows (`pyproject.toml` at the repo root).
- Distribution channel decision: install directly from the git repository
  (`pipx install git+<repo-url>[@tag]`) instead of publishing to PyPI;
  versioning is by git tags, and PyPI publication remains a future option
  using the same package definition.
- `sch setup`: guided first run — prerequisite checks (AWS CLI v2, agentcore,
  node, git; docker/opencode warn-only), support-repo acquisition (clones the
  checkout carrying `tunnel/` and `infra/` into a well-known user-data
  location; `--repo`/`SCH_REPO_URL` override), runtime-stack status, and
  optional `--deploy`.
- Support-repo resolution (`cli/sch/repo.py`): `SCH_REPO_ROOT` env var, then
  the managed checkout (`~/.local/share/sch/repo` / `%LOCALAPPDATA%\sch\repo`),
  then legacy git-checkout-relative resolution; `zed-config` now prefers the
  installed `sch` executable for the Zed agent-server snippet.
- Normative spec `docs/specs/platform/installation.md` and a README
  Installation section.
- Linear from-scratch installation: a bootstrap stack (ECR repository,
  CodeBuild image-build project, build-sources bucket) owned by the deploy,
  `sch deploy` as the one command for bootstrap stack, image build and runtime
  stack, and `dev` as the default environment name.
- Two-command teardown: `sch destroy` (every AWS resource of one
  project/environment, `--dry-run`, `--keep-checkpoints`) and `sch uninstall`
  (local state, support checkout, the client).
- Deploy-time runtime capability tuning: shape the execution role without
  editing the template (extra AI services, Bedrock model allow-list, removing
  `ReadOnlyAccess`, extra policy JSON), deployed in place.
- `sch info` reports the configured per-user provider key names and the
  expected key-file paths.
- `sch handoff --harness` (opencode only, first-shot workspace creation) and
  `sch task --handoff`: export the local OpenCode session, seed the branch,
  import remotely and submit the headless task with an implied `--continue`.
- `sch run --continue`: resume the harness's latest session in the TUI (all
  harnesses; degrades to a fresh session when none is found).
- Opt-in GitHub access from the remote harness: pinned `gh`, ephemeral
  `GITHUB_TOKEN` staged on tmpfs; the default stays credential-less.
- `sch status` shows whether a task resumed a prior session (continuation
  provenance).
- Repository hygiene for publication: CI workflow (unit suites, docs
  verifier, shell syntax, gitleaks), Dependabot, `.gitleaks.toml`, a `dev`
  extra for the infra test suite, `docs/security.md`, `docs/README.md`
  (documentation map), `docs/release/` (checklist and readiness review).

### Changed

- Tunnel-dependent commands (`--sync`, `acp`, `attach`, `web`, git-native
  bundle transfer) resolve the tunnel helpers through the support-repo
  resolution above; from a git checkout behavior is unchanged.
- openspec workflow retired; capability specs now live in `docs/specs/` and work
  is tracked in Backlog tasks.
- The runtime references its image by digest: every deploy that rebuilds the
  image is a new runtime version; `sch deploy -s` deploys stack changes in
  place without touching the runtime version.
- AWS access inside the image moved from the self-hosted `aws-api` MCP server
  to the managed AWS MCP Server through a pinned local proxy.
- Documentation restructured: the README is a landing page; the former
  long-form sections are topic guides under `docs/` and proof-of-concept notes
  live under `docs/history/`. `bin/verify-docs.sh` now checks heading anchors
  and scans every tracked file for account IDs and personal paths.
- Harness and tool versions in the image bumped (OpenCode, Claude Code, Pi).

### Fixed

- `sch task --continue` and `sch run --continue` no longer start a fresh
  session after `sch stop`: session resolution waits for the cold-boot
  restore (workspace-ready gate).
- A rebuild of an unchanged image tag reaches the runtime (digest pin).
- `deploy.sh` empty-array expansion failure on macOS bash 3.2.
- `aws-api` MCP startup restored by constraining its transitive dependencies
  (superseded by the managed AWS MCP Server migration).
- Watchdog resends terminal Telegram notifications for headless tasks so a
  notification is delivered at least once.
- Test hygiene: the run-profile tests no longer inherit `SCH_*` variables
  from the host; the infra suite's dependencies are declared.

[Unreleased]: https://github.com/c-daniele/sch/commits/main
