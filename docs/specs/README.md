# SCH Specifications

This is the **source of truth for product and architecture behavior**, organized by
domain. Specs are normative: MUST/SHALL statements describe required behavior, and
code that contradicts a spec is wrong (fix one or the other in the same change).

Start here, then jump to a domain:

| Domain | What it covers |
| --- | --- |
| [Platform](platform/) | The compute foundation: runtime provisioning, the version-pinned container image, image rebuild, harness selection, CLI installation & distribution. |
| [Security](security/) | IAM as the real boundary: workspace control API, workspace registry, owner-scoped and selectable storage. |
| [Workspace lifecycle](workspace-lifecycle/) | Keeping state safe: persistence, checkpointing, deletion, session handoff. |
| [Access surfaces](access-surfaces/) | How users reach SCH: CLI, interactive shell, headless tasks, remote UI tunnel, ACP editors, dashboard, Telegram. |
| [Sync & git](sync-and-git/) | Moving code in and out: local workspace sync and the git-native parallel-session workflow. |
| [Providers & models](providers-models/) | Inference configuration: provider keys, per-user keys, model selection. |

## Spec files

Status values: **Implemented** (verified against the code), **Partially verified**
(implemented; the file lists what still needs a live check), **Proposed** (written before
the code; names the backlog task).

### Platform

| Spec | Status | Covers |
| --- | --- | --- |
| [`installation.md`](platform/installation.md) | Implemented | git-install distribution (`sch` from `git+URL`, tag pinning), support repo resolution order, `sch setup`, `sch deploy`, `sch destroy`, `sch uninstall`. |
| [`runtime-provisioning.md`](platform/runtime-provisioning.md) | Implemented | Bootstrap stack + CodeBuild + ECR + AgentCore Runtime + checkpoint bucket + execution-role IAM + deploy/rollback. |
| [`runtime-image.md`](platform/runtime-image.md) | Implemented | Hardened, version-pinned image: tooling pins, dispatcher, seeding, shim actions. |
| [`session-image-rebuild.md`](platform/session-image-rebuild.md) | Implemented | CodeBuild-delegated arm64 image rebuilds from session state. |
| [`harness-selection.md`](platform/harness-selection.md) | Implemented | `--harness opencode\|claude\|pi` persistence, change rules, mutual exclusivity. |

### Security

| Spec | Status | Covers |
| --- | --- | --- |
| [`iam-workspace-control-api.md`](security/iam-workspace-control-api.md) | Implemented | IAM-authenticated registry API contract (resolve/list/rotate/delete, error contract). |
| [`iam-workspace-registry.md`](security/iam-workspace-registry.md) | Implemented | CLI-side registry behavior, owner scoping, no-cache-fallback, legacy mode. |
| [`workspace-registry.md`](security/workspace-registry.md) | Implemented | Implemented Lambda + API Gateway + DynamoDB control plane. |
| [`owner-scoped-workspace-storage.md`](security/owner-scoped-workspace-storage.md) | Implemented | Per-principal S3 prefixes, identity determinism, no cross-user reach. |
| [`selectable-workspace-storage.md`](security/selectable-workspace-storage.md) | Implemented | `--storage s3\|session` semantics, epoch fencing. |
| [`runtime-capability-tuning.md`](security/runtime-capability-tuning.md) | Implemented | Deploy-time shaping of the runtime execution role: capability catalog, Bedrock model allow-list, ReadOnlyAccess toggle, escape hatch. |

### Workspace lifecycle

| Spec | Status | Covers |
| --- | --- | --- |
| [`workspace-persistence.md`](workspace-lifecycle/workspace-persistence.md) | Implemented | Durability contract across stop/resume/idle/recreation, per backend. |
| [`workspace-checkpointing.md`](workspace-lifecycle/workspace-checkpointing.md) | Implemented | Checkpoint mechanics: timing, manifests, restore-always-wins, retention. |
| [`workspace-deletion.md`](workspace-lifecycle/workspace-deletion.md) | Implemented | Irreversible, verified deletion of every stored version and prefix. |
| [`session-handoff.md`](workspace-lifecycle/session-handoff.md) | Implemented | Export/import of agent sessions between workspaces and machines. |

### Access surfaces

| Spec | Status | Covers |
| --- | --- | --- |
| [`cli-cross-platform.md`](access-surfaces/cli-cross-platform.md) | Implemented | One stdlib CLI, macOS/Linux/Windows parity, shim delegation. |
| [`interactive-shell-access.md`](access-surfaces/interactive-shell-access.md) | Implemented | TUI sessions, detach/reconnect, presence, multi-workspace. |
| [`headless-task-execution.md`](access-surfaces/headless-task-execution.md) | Implemented | Detached tasks, staleness/watchdog safety, `--model`. |
| [`remote-ui-tunnel.md`](access-surfaces/remote-ui-tunnel.md) | Implemented | `sch attach` / `sch web` / tunnel framing and transport. |
| [`acp-editor-integration.md`](access-surfaces/acp-editor-integration.md) | Implemented | Zed & ACP editors as remote agent front-ends. |
| [`acp-file-locality.md`](access-surfaces/acp-file-locality.md) | Implemented | Where ACP-editable files live and how paths map. |
| [`dashboard-tui.md`](access-surfaces/dashboard-tui.md) | Implemented | Workspace overview TUI. |
| [`telegram-notifications.md`](access-surfaces/telegram-notifications.md) | Implemented | Milestone notifications from running sessions. |
| [`telegram-interaction.md`](access-surfaces/telegram-interaction.md) | Implemented | Remote approval and follow-up prompts. |

### Sync & git

| Spec | Status | Covers |
| --- | --- | --- |
| [`local-workspace-sync.md`](sync-and-git/local-workspace-sync.md) | Implemented | `--sync` mirroring: bindings, bootstrap, conflicts, convergence. |
| [`git-native-workflow.md`](sync-and-git/git-native-workflow.md) | Implemented | `--branch` seeding via git bundle, snapshot/fetch delivery, local-only credentials. |

### Providers & models

| Spec | Status | Covers |
| --- | --- | --- |
| [`provider-api-keys.md`](providers-models/provider-api-keys.md) | Implemented | Key staging into the microVM, per-harness mapping, claude Bedrock↔Anthropic switch. |
| [`user-provider-keys.md`](providers-models/user-provider-keys.md) | Implemented | `~/.sch/env` per-user key source and its allowlist. |
| [`run-model-selection.md`](providers-models/run-model-selection.md) | Implemented | `--model` ephemerality and per-harness argv mapping. |

## Conventions

- **Template.** Every spec uses the same skeleton:
  1. Title and one-line purpose.
  2. `Status:` one of **Implemented**, **Partially verified** (implemented, some
     requirements not yet checked live; say which), **Proposed** (spec written before the
     code; names the backlog task).
  3. **Scope**: what is in and what is explicitly out.
  4. **Requirements**: numbered **R1, R2, …**, one normative statement each (MUST /
     SHOULD / MAY). At most one or two `WHEN … THEN …` scenarios per requirement, only
     where they remove ambiguity.
  5. **Behavior**: flows, payload shapes, event names, error codes, table keys, S3
     prefixes. Concrete and path-based.
  6. **Invariants**: numbered **I1, I2, …**, properties that must hold across all flows.
  7. **Cross-references**: the real code paths that implement the spec, related specs,
     and the backlog task or journal entry that verified the behavior.
- **No duplication.** A rule is stated once, in the most specific spec; related specs
  cross-link. If two specs disagree, the more specific one wins and the conflict is a bug.
- **Code paths are evidence.** Cross-references cite real files. Requirements are checked
  against the code before they are written; a statement that cannot be verified is marked
  `(unverified)` rather than asserted.
- **Same change.** When behavior changes, the spec changes in the same commit or PR.
- **Numbering is stable.** Requirements and invariants are never renumbered; a retired
  one is marked *retired* with the reason.
- **History.** These specs were rationalized from the retired OpenSpec system.
  Every mention of git tag `pre-openspec-retirement` (here and in the specs'
  cross-references) points at the last commit that still contained the old
  `openspec/` folder. That tag lives in the maintainer's private pre-publication
  history, not in this repository, which was published as a fresh history in
  September 2026; the originals are available on request.

Related documents: [MANIFESTO](../../MANIFESTO.md) (constitution) ·
[coding standards](../coding-standards.md) · [masterplan](../../.backlog/masterplan/MASTERPLAN.md).
