# SCH — Serverless Coding Harness · Agent Guide

## General Agent Guidelines

- SCH runs AI coding agents (OpenCode, Claude Code, Pi) in disposable, checkpointed
cloud microVMs on AWS Bedrock AgentCore — no servers to manage, near-zero idle cost.
- **Start every session here:** read [`.backlog/masterplan/MASTERPLAN.md`](.backlog/masterplan/MASTERPLAN.md)
to see where the project is right now, then check active tasks with `backlog task list --plain`.
- Use the active Backlog task as the execution plan of record. Use the masterplan
for cross-task context, safety boundaries, completed evidence, durable decisions,
high-level sequencing, and shared vocabulary.
- Keep the masterplan lean: per-task narrative and implementation detail belong in the journal,
not in the masterplan.
- Update the masterplan when work changes a cross-task decision, exposes a shared
dependency or deviation, or produces evidence needed by future tasks. Keep
task-specific plans, progress, and completion evidence in the Backlog task.
- If a term, acronym, profile name, component, pipeline mode, or workstream label
causes misunderstanding, resolve the intended meaning and update the masterplan
glossary before continuing dependent work.
- After completing a task, create a journal entry with `backlog doc create` and
`backlog doc update`. Plain English, written for a future teammate: the problem, what changed,
the outcome, and a *Lesson* section when there is one. No code-level detail; link the task and the
spec instead. Nothing is written by hand under `.backlog/docs/` or `.backlog/decisions/`.
Journal titles start with the date (`YYYY-MM-DD <title>`) and are never changed after creation.

## Language

Every project artifact is written in English: specs, tasks, journal entries, decisions,
masterplan, brainstorming notes, commit messages, code comments, UI copy, documentation. The
conversation with the maintainer may happen in Italian; artifacts never do. When you find
an artifact in another language, translate it in the change that touches it.

## Self-correction

- A spec, standard or reference document that disagrees with the code is a bug. Fix it in
  the same change when it is in scope; otherwise create a Backlog task and say so in the
  task you are working on.
- When a session with the maintainer changes a decision, a scope or a priority, update
  the masterplan and the affected tasks *before* continuing with dependent work.
- Stale statements, dangling file references and wrong defaults in any document are
  corrected on sight.
- Lessons learned go into the journal entry's *Lesson* section and, when they change how
  work is done, into `docs/coding-standards.md` or this file.
- Proposed edits to `AGENTS.md` itself are shown to the maintainer before they are made.
- If a term, component name or workflow label causes a misunderstanding, resolve it and
  update the masterplan glossary before continuing.

## Conversational Style

- Keep answers short and concise
- No emojis in commits, issues, PR comments, or code
- Technical prose only, be direct
- Use concise, clear, simple language. Define unavoidable jargon before using it.
- Explain non-trivial designs and problems as: problem, concrete example or short trace, then solution. State why the solution is necessary and distinguish it from optional complexity.
- Prefer concrete behavior and small illustrations over abstract summaries, dense terminology, or unexplained lists of changes.
- When the user asks a question, answer it first before making edits or running implementation commands.
- When responding to user feedback or an analysis, explicitly say whether you agree or disagree before saying what you changed.

## Document map

| File / folder | What it is |
| --- | --- |
| [`.backlog/masterplan/MASTERPLAN.md`](.backlog/masterplan/MASTERPLAN.md) | Always-current plan: current state, roadmap, decisions, active work. Updated after every completed task. |
| `backlog task list` · `.backlog/tasks/` | The task board (Backlog.md). Edit only through the `backlog` CLI. |
| [`MANIFESTO.md`](MANIFESTO.md) | Project constitution: audience, core loop, source of truth, surface hierarchy, principles, boundaries, risks. All decisions must align with it. |
| [`docs/specs/`](docs/specs/README.md) | Source-of-truth product specs, by domain, normative style. Start from `docs/specs/README.md`. |
| [`docs/coding-standards.md`](docs/coding-standards.md) | Coding standard per language (Python, Node, Bash, Markdown) and repository hygiene. |
| `.backlog/docs/journal/` · `backlog doc list` | Durable, plain-English journal: created with `backlog doc create` and `backlog doc update`. |
| `.backlog/decisions/` · `backlog decision list` | Architecture decision records, created with `backlog decision create`. |
| [`.backlog/brainstorming/`](.backlog/brainstorming/) | Temporary exploration artifacts, one `YYYY-MM-DD.<IdeaTitle>/` folder per idea. Nothing durable lives here. |
| [`docs/README.md`](docs/README.md) | Documentation map: topic guides (getting started, deploy, CLI, workspaces, headless tasks, remote access, harnesses, Telegram, security), `docs/release/` (release checklist, readiness review), `docs/history/` (non-normative findings). |
| [`CONTRIBUTING.md`](CONTRIBUTING.md), [`SECURITY.md`](SECURITY.md), [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) | Open-source community documents. |

## Development workflow

1. **Orient**: read `MASTERPLAN.md` — where we are, what's next.
2. **Plan**: search/create a backlog task (`backlog task create`) for anything that
   needs thinking; small mechanical fixes don't need a task. Set task to `In Progress`
   and write its implementation plan before coding.
3. **Explore**: put investigations, scratch notes, diagrams in
   `.backlog/brainstorming/YYYY-MM-DD.<IdeaTitle>/`. Nothing durable there.
4. **Implement**: follow the specs in `docs/specs/` and the coding standards.
5. **Verify**: run the relevant tests (`cli/tests`, `infra/test_*`, `tunnel` tests),
   the capability's `bin/verify-*.sh` script, and `bin/verify-docs.sh` for documentation changes.
6. **Document**: if the task added or changed something user-facing, propose
   documentation examples per [Documentation examples](#documentation-examples)
   and wait for the user's confirmation before writing them.
7. **Finalize**: after completing a task:
   - check the acceptance criteria and write the final summary (`backlog task edit TASK-N`);
   - create the journal entry:
     `backlog doc create "<YYYY-MM-DD> <task name>" -p journal -t other --plain` followed by
     `backlog doc update doc-N --content "$(cat draft.md)" --tags journal`. Plain English, written
     for a future teammate: problem, what changed, outcome, and a *Lesson* section when applicable;
   - record any durable decision with `backlog decision create "<title>" -s accepted --plain`,
     then fill Context, Decision and Consequences by editing the created file (the CLI has no content
     option; this is the one accepted direct edit under `.backlog/`);
   - update `MASTERPLAN.md` (where we are, active work, roadmap, decisions, journal index);
   - `backlog task edit TASK-N -s Done`.

## Backlog CLI facts & caveats

- `backlog task edit` cannot target files under `.backlog/completed/` (move them back to `.backlog/tasks/` for the edit if needed).
- A title change in `backlog task edit` does not rename the task file on disk (rename by hand with `git mv` if the filename must stay readable).
- Decisions created with `backlog decision create` create an empty Context/Decision/Consequences template without a `--content` option; editing the created decision markdown file is the sole accepted direct file edit under `.backlog/`.

## Documentation examples

- **When it applies**: the task introduced or changed a user-facing feature,
  configuration option, CLI command, flag, or parameter. Internal refactors,
  bug fixes, and test-only changes do not qualify.
- **Propose, don't write**: suggest concrete use cases and real examples derived
  from the new feature (e.g. an actual command invocation, a config snippet, a
  short before/after trace) and list the target doc file(s). Always ask the user
  for confirmation before adding them — never insert examples unprompted.
- **On confirmation**: add only the approved examples, keep them short and
  runnable, and place them in the right surface — operator guides (`docs/`) or
  `README.md` for how-to examples; specs (`docs/specs/`) only when the example
  clarifies normative behavior.
- **Quality bar**: examples must reflect real, verified behavior (ideally taken
  from the task's own verification runs), contain no secrets or account IDs, and
  follow the no-duplication rule — one home per example, link elsewhere.

## Git standard

- Trunk-based: single `main` branch; all work goes through short-lived feature
  branches (`feat/<name>`, `fix/<name>`, `docs/<name>`).
- Squash-merge into `main`; keep commit messages conventional
  (`feat:`, `fix:`, `docs:`, `chore:`, `refactor:`, `test:`).
- Deploy is script-driven (`infra/deploy.sh`) — no release branches.
- Never commit secrets, AWS account IDs, or credentials.

## Coding standards (summary)

Full standard: [`docs/coding-standards.md`](docs/coding-standards.md).

- **Python** (`cli/sch`, `infra/`): Python 3, stdlib-first, `unittest`. CLI in
  `cli/sch`, Lambda handlers in `infra/` with `test_*.py` next to them.
- **Node.js** (`tunnel/`): plain JavaScript + `node --test`; no heavy frameworks.
- **Bash** (`bin/`): `bin/sch` is a thin launcher shim only — no application logic;
  `bin/verify-*.sh` are end-to-end capability checks.
- **Markdown** (docs, specs): plain English, normative specs, no duplication —
  link instead of copying.
- Tests live beside their component; run the narrowest relevant suite first.

<!-- BACKLOG.MD GUIDELINES START -->
<!-- backlog.md-instructions-version: 1.50.1 -->
<BACKLOGMD_GUIDELINES>

## Backlog.md Workflow

This project uses Backlog.md for task and project management.

It's important to track all coding tasks into the backlog system. Whenever you need to accomplish development/fix activities, run `backlog instructions overview` before taking action.

Use the overview to decide whether to search, read, create, or update Backlog tasks.

Before task lifecycle actions, read the matching detailed guide:
- `backlog instructions task-creation` before creating or splitting tasks
- `backlog instructions task-execution` before planning, changing status or assignee, adding a plan or implementation notes, or implementing task work
- `backlog instructions task-finalization` before checking acceptance criteria, writing final summaries, or moving tasks to terminal statuses

Use `backlog <command> --help` before running unfamiliar commands. Help shows options, fields, and examples.

Do not edit Backlog task, draft, document, decision, or milestone markdown files directly. Use the `backlog` CLI so metadata, relationships, and history stay consistent.

</BACKLOGMD_GUIDELINES>
<!-- BACKLOG.MD GUIDELINES END -->
