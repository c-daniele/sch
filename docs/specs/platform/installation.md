# Installation & Distribution

> Domain: [Platform](../README.md) · Status: Implemented · Source: rationalized from openspec specs (2026-08-28)

## Purpose

Define the distribution, installation, first-run bootstrap, support repository resolution, deploy invocation, and layered teardown contracts for the SCH client CLI.

## Scope

In scope:
- Package distribution via Git and stdlib-only wheel contract (`pyproject.toml`).
- Support repo resolution order (`SCH_REPO_ROOT`, managed checkout, legacy checkout).
- Guided first-run bootstrap via `sch setup`.
- Transparent deploy script delegation via `sch deploy`.
- Cloud infrastructure teardown via `sch destroy`.
- Local state and client removal via `sch uninstall`.

Out of scope:
- Client command-line interface options beyond installation lifecycle (see [cli-cross-platform](../access-surfaces/cli-cross-platform.md)).
- CloudFormation infrastructure provisioning details (see [runtime-provisioning](runtime-provisioning.md)).

## Requirements

### Distribution channel

- **R1.** SCH SHALL be distributed directly from the git repository using standard Python packaging tools (`pipx`, `uv tool`, `pip`). Releases SHALL be tagged on the git repository.
- **R2.** The package definition (`pyproject.toml`) SHALL declare zero install-time and runtime dependencies (`dependencies = []`), target Python `>= 3.8`, and install the console script `sch`.
- **R3.** The repository SHALL remain runnable in-place from a checkout via `bin/sch` / `bin/sch.ps1` without file relocation; the package SHALL map `cli/` to the `sch` import package.

### Support repository resolution

- **R4.** The CLI SHALL resolve a support repo checkout at runtime for tunnel helpers (`tunnel/*.js`) and deployment tooling (`infra/deploy.sh`), validating candidates by the presence of `tunnel/sync.js`.
- **R5.** The support repo resolution order SHALL be:
  1. `SCH_REPO_ROOT` environment variable override;
  2. Managed checkout at `~/.local/share/sch/repo` (POSIX, `$XDG_DATA_HOME/sch/repo`) or `%LOCALAPPDATA%\sch\repo` (Windows);
  3. Legacy checkout (grandparent of the `sch` package directory).
- **R6.** Commands requiring the support repo SHALL fail with an actionable error naming `SCH_REPO_ROOT` and `sch setup` when no checkout is found.

### First-run setup (`sch setup`)

- **R7.** `sch setup` SHALL perform local prerequisite checks, reporting `ok` or `MISSING` for fatal prerequisites (`aws` CLI v2, `agentcore`, `node`, `git`) and `warn` for optional tooling (`docker`, `opencode`).
- **R8.** `sch setup` SHALL clone the support repository into the managed checkout path if absent, honoring `--repo <url>` and `SCH_REPO_URL`.
- **R9.** `sch setup` SHALL fast-forward refresh an existing managed checkout (`git pull --ff-only`), skipping updates if dirty or diverged, and MUST NOT mutate checkouts resolved via `SCH_REPO_ROOT` or legacy checkout.
- **R10.** `sch setup` SHALL ensure tunnel dependencies by executing `npm install` in `tunnel/` when `tunnel/node_modules` is absent.
- **R11.** `sch setup` SHALL probe runtime stack status (`cloudformation describe-stacks`) and, if missing, print `sch setup --deploy`.
- **R12.** `sch setup --deploy` SHALL execute `infra/deploy.sh` from the resolved support checkout on POSIX hosts and print execution steps on Windows.

### Deploy invocation (`sch deploy`)

- **R13.** `sch deploy` SHALL resolve the support repository, forward all options to `infra/deploy.sh`, propagate the exit code, and provide guidance on Windows requiring a bash host.

### Teardown & uninstall (`sch destroy`, `sch uninstall`)

- **R14.** `sch destroy` SHALL delete all AWS deployment resources in a single idempotent pass: runtime stack, ECR repository images, bootstrap stack, legacy ECR stack, build-sources bucket, CloudFormation bootstrap bucket, and checkpoint bucket (unless `--keep-checkpoints`).
- **R15.** `sch destroy` SHALL verify STS caller identity at runtime, require typed confirmation (unless `--yes`), support `--dry-run`, deregister Telegram webhooks, and invalidate local runtime ARN and checkpoint bucket caches.
- **R16.** `sch uninstall` SHALL remove local state (`~/.config/sch`), managed support checkout, per-user keys (`~/.sch/env` unless `--keep-keys`), temporary artifacts, and the installed CLI package (detecting pipx, uv, or pip).
- **R17.** `sch uninstall` SHALL refuse to execute while a runtime stack remains deployed unless `--force` is supplied.

## Behavior

- `pipx install git+https://github.com/c-daniele/sch.git` installs `sch` onto `PATH`.
- `sch setup --deploy` prepares local dependencies, clones the support repo, and deploys the AWS stack.
- `sch destroy` interactively prompts with the resolved account/region before deleting AWS resources.
- `sch uninstall` cleans local configuration and removes the package.

## Invariants

- **I1.** The installed `sch` Python package has zero external third-party package dependencies.
- **I2.** `sch setup` never mutates user-managed checkouts (`SCH_REPO_ROOT` or legacy clones).
- **I3.** `sch uninstall` refuses to delete local deploy tooling while AWS infrastructure is running unless forced.
- **I4.** `sch destroy` always queries STS live before deleting and treats absent AWS resources as success.

## Cross-references

- [cli-cross-platform](../access-surfaces/cli-cross-platform.md) — Cross-platform CLI contract and commands.
- [runtime-provisioning](runtime-provisioning.md) — Infrastructure provisioning, deploy script, and teardown scope.
- [MANIFESTO](../../../MANIFESTO.md)
- Code: `pyproject.toml`, `cli/sch/repo.py`, `cli/sch/commands/setup.py`, `cli/sch/commands/deploy.py`, `cli/sch/commands/destroy.py`, `cli/sch/commands/uninstall.py`, `cli/sch/awsteardown.py`
