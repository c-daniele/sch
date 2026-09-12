# SCH Coding Standards

Normative. Every change must comply. The guiding rule: **stdlib first, frameworks
only when they pay rent.** SCH is maintained by a small team — boring, explicit,
testable code wins over clever code.

General rules that apply to every language:

- Plain English in comments, docstrings, and docs. Comments explain *why*, not *what*.
- No secrets, AWS account IDs, or credentials in source, tests, or docs — use
  `infra/setenv.sh.example` as the template for local environment setup.
- Every behavior change ships with tests in the narrowest relevant suite (see
  [AGENTS.md](../AGENTS.md) workflow step 5).
- Prefer deleting code over configuring it away; prefer one way of doing things.
- Markdown docs: link, don't copy. Duplication of normative content across files
  is a defect.

## Python

Applies to: the CLI (`cli/sch/`), Lambda handlers (`infra/*.py`), and image-side
Python (`image/app/`).

- **Version**: Python 3. Target the runtime Lambda supports; do not require the
  newest stdlib features.
- **Dependencies**: stdlib first. If a third-party package is unavoidable, isolate
  it (Lambda layers or vendored dirs) and justify it in the PR.
- **Style**: PEP 8 as a baseline; 4-space indent; type hints on public function
  signatures where they add clarity (don't annotate for annotation's sake).
- **Structure**: modules with clear, single purposes; handlers stay thin — parse
  the event, call a function, return a response. Business logic lives in
  importable functions so tests don't need to invoke Lambda.
- **CLI output**: user-facing messages are lowercase, terse, prefixed with context
  (`sch:` style); errors go to stderr and set non-zero exit codes.
- **Testing**: stdlib `unittest` (no pytest dependency). Tests live beside their
  component: `cli/tests/` (a package, with `__init__.py`) and `infra/test_*.py`
  next to their handlers. Tests must run without network or AWS credentials —
  inject fakes/stubs; never hit real buckets or bots from a unit test.
  ```bash
  cd cli && python3 -m unittest discover -s tests -v
  cd infra && python3 -m unittest discover -v
  ```
- **Naming**: `snake_case` modules/functions, `PascalCase` classes, `SCREAMING_SNAKE`
  module-level constants. Test files `test_<topic>.py`.

## Node.js

Applies to: the local tunnel client (`tunnel/`), invoked by `bin/sch`, never published.

- **Version**: Node >= 18 (ES modules, `"type": "module"`).
- **Dependencies**: minimal and justified — currently only `ws` and the AWS SigV4
  signing libraries. No web frameworks, no build step, no TypeScript.
- **Style**: plain JavaScript; 2-space indent; small modules exporting explicit
  named functions; no classes where a closure or object literal suffices.
- **Testing**: the built-in `node --test` runner. Test files sit beside the module
  (`sync.js` / `sync.test.js`). Register new test files in the `package.json`
  `test` script — it chains every test file explicitly.
  ```bash
  cd tunnel && npm test
  ```
- **Runtime posture**: the tunnel is the *local* end of remote connections. It must
  never persist state, spawn daemons, or write outside its mirror directory.

## Bash

Applies to: `bin/`.

- **`bin/sch` is a shim only.** It locates a Python interpreter and delegates
  everything (`cli/sch`). No application logic, no argument parsing beyond
  interpreter selection. Same intent for the PowerShell variant `bin/sch.ps1`.
- **`bin/verify-*.sh`** are end-to-end capability checks: one script per capability,
  named after it. They may use AWS CLI + `jq`/`python3` helpers, must be safe to
  re-run, and must clean up resources they create (or state clearly why not).
- **Style**: `#!/bin/bash`, `set -euo pipefail`, lowercase local variables,
  `SCREAMING_SNAKE` for env/exported vars, quote every expansion. No silent `rm -rf`
  of user paths; no `sudo`.
- Platform: scripts must work on macOS and Linux (the CLI claims cross-platform
  support — see the `cli-cross-platform` spec).

## Markdown

Applies to: `README.md`, `docs/`, `.backlog/`, and root documents.

- Plain English. Short sentences. No filler, no marketing tone in normative docs.
- **Normative docs** (MANIFESTO, specs, standards) use RFC-style verbs: *must*,
  *should*, *may*.
- Specs (`docs/specs/`) follow the template in `docs/specs/README.md` — Purpose,
  Scope, Requirements, Behavior, Invariants, Cross-references.
- `README.md` is a landing page (what, why, tour, warnings, install, map): new
  long-form content goes into the topic guide under `docs/` that owns the topic
  (index: `docs/README.md`), never into the README. Point-in-time findings go
  to `docs/history/`, marked non-normative.
- No duplication: each fact lives in exactly one file; everywhere else links to it.
- Relative links between repo files so docs work on GitHub and in editors.
- `.backlog/` conventions:
  - `MASTERPLAN.md` — current state, roadmap, active work, durable decisions, journal index. Updated after every task.
  - `.backlog/docs/journal/` — plain-English post-task records created with `backlog doc create` and `backlog doc update`. Written for future teammates.
  - `.backlog/decisions/` — architecture decision records created with `backlog decision create`.
  - `brainstorming/YYYY-MM-DD.<IdeaTitle>/` — scratch exploration: notes, diagrams, images. Nothing here is a source of truth; promote findings into specs or the masterplan instead of letting them rot here.

## Repository hygiene

- **Tool configuration**: AI tool trees (`.claude/`, etc.) point at `AGENTS.md` (e.g. `CLAUDE.md` containing `see @AGENTS.md`) rather than carrying their own divergent workflow rules.
- **Clean tracking**: Inactive or empty tool configuration trees are kept out of tracking; local tool cache trees are ignored via `.gitignore`.
- **Zero secrets**: No AWS account IDs, access keys, bearer tokens, or personal identifiers in any tracked file — Backlog task notes and brainstorming included. `bin/verify-docs.sh` scans every tracked file (account IDs, personal home paths) and CI runs gitleaks over the history; use `<account-id>` or the AWS sample IDs (`111122223333`) in evidence and fixtures.
- **Tests are hermetic**: a test that spawns a process passes it an explicit environment (strip `SCH_*` at least); the suites must pass on a host that is itself an SCH workspace.
- **Dev dependencies are declared**: anything a test suite imports beyond the stdlib goes into the `dev` extra of `pyproject.toml`, never into the client's `dependencies`.
