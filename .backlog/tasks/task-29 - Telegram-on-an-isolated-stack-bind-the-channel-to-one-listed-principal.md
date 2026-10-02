---
id: TASK-29
title: 'Telegram on an isolated stack: bind the channel to one listed principal'
status: Done
assignee:
  - '@claude'
created_date: '2026-10-02 16:02'
updated_date: '2026-10-02 16:24'
labels:
  - security
  - telegram
dependencies: []
references:
  - docs/specs/security/per-principal-isolation.md
  - docs/specs/access-surfaces/telegram-notifications.md
  - .backlog/decisions/decision-14
priority: medium
type: feature
ordinal: 26000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Since TASK-20, infra/deploy.sh refuses TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID and ENABLE_TELEGRAM_INTERACTION together with ISOLATED_PRINCIPALS (per-principal-isolation R44, decision-14 point 9), because Telegram was wired as a runtime-level feature: the shared runtime carried the token and the table names, the plane runtimes receive the shared environment minus every SCH_TELEGRAM_* variable (R21), the plane boundary denies every SCH DynamoDB table (R20) and the Telegram session policy is never attached to a plane role (R18). On an isolated stack the maintainer therefore lost every Telegram hook: milestones, remote approvals, follow-ups. The maintainer chose on 2026-10-02 the smallest safe option: Telegram stays a single-operator feature, but on an isolated stack it binds to exactly one listed principal (`TELEGRAM_PRINCIPAL=<entry>`), whose plane alone receives the token, the chat id, the table names and the Telegram policy. Per-owner Telegram (one bot per principal) stays a follow-up. Context the code does not show: the webhook accepts one chat id, so a second Telegram user is not expressible without per-owner bots; the bound owner agent can read the bot token, exactly as the operator agent does on an isolation-off stack; the watchdog must not fall back to the plain chat for owner-tree workspaces, otherwise a stale task of an unbound owner would be announced in the bound chat.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 With isolation on and Telegram configured, the deploy refuses the combination unless TELEGRAM_PRINCIPAL names one ISOLATED_PRINCIPALS entry; it also refuses TELEGRAM_PRINCIPAL without Telegram, without isolation, or naming an unlisted entry, before any stack changes
- [x] #2 Only the bound plane runtime receives SCH_TELEGRAM_BOT_TOKEN, SCH_TELEGRAM_CHAT_ID and, with interaction enabled, SCH_TELEGRAM_COMMANDS_TABLE and SCH_TELEGRAM_ROUTING_TABLE; every other plane keeps the shared environment minus SCH_TELEGRAM_* plus SCH_OWNER_PREFIX
- [x] #3 Only the bound plane execution role attaches the Telegram interaction policy, and its boundary denies the registry table while still denying it on every other plane together with the Telegram tables; template tests pin both shapes
- [x] #4 The task watchdog never uses the plain-chat fallback for an owner-tree workspace without a topic mapping, so unbound owners are never announced in the bound chat; a unit test covers it
- [x] #5 bin/verify-telegram-*.sh skip on an isolated stack only when TELEGRAM_PRINCIPAL is not exported, and say that the check must run as that principal otherwise
- [x] #6 Spec per-principal-isolation (R6, R18, R20, R21, R44, deploy flow, threat model, residual risks), telegram-notifications (watchdog fallback), decision record, deploy.sh header, docs/deploy.md, docs/security.md, docs/workspaces.md, docs/telegram.md, docs/telegram-setup.md and CHANGELOG state the new behavior; no document still says Telegram is refused with isolation on
- [x] #7 infra and cli suites pass; bin/verify-docs.sh passes
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
Spec: docs/specs/security/per-principal-isolation.md (R6, R18, R20, R21, R44), telegram-notifications R3/R10. Branch feat/telegram-principal.

1. infra/isolation_plan.py: TELEGRAM_PRINCIPAL switch in check_switches (R44): with isolation on, Telegram needs TELEGRAM_PRINCIPAL naming one listed entry (parsed with parse_entries, matched on the entry text); TELEGRAM_PRINCIPAL without Telegram or unlisted is refused. Plan lines gain an 8th column telegram=true|false per plane.
2. infra/agent_runtime.yaml: TelegramInteractionSessionPolicy becomes AWS::IAM::ManagedPolicy TelegramInteractionSessionManagedPolicy (same name, statements, role), new output TelegramInteractionPolicyArn (interaction only). SharedRuntimePolicyArns unchanged.
3. infra/user_plane.yaml: parameters TelegramBinding, TelegramBotToken (NoEcho), TelegramChatId, TelegramCommandsTable, TelegramRoutingTable, TelegramPolicyArn; conditions TelegramBound and TelegramInteractionBound; bound plane: SCH_TELEGRAM_* env, Telegram policy appended to ManagedPolicyArns, boundary DenySchTables on the registry table only; every other plane unchanged.
4. infra/deploy.sh: TELEGRAM_PRINCIPAL default and header, passed to the helper; refused with isolation off (bash, before the bootstrap stack); deploy_planes reads the telegram column and the new runtime outputs and passes the Telegram parameters to the bound plane only (empty elsewhere); summary names the bound entry.
5. infra/task_watchdog_handler.py: no plain-chat fallback for an owner-tree workspace without a topic mapping (send nothing, log).
6. bin/verify-telegram-*.sh: on an isolated stack skip unless TELEGRAM_PRINCIPAL is exported, else note that the caller must be that principal.
7. Tests: test_isolation_plan (switch cases, plan column), test_isolation_templates (managed Telegram policy, bound and unbound plane env, role policies, boundary), test_isolation_deploy (plane parameters, refused combinations, bash refusal), test_task_watchdog_handler (owner-tree fallback), test_runtime_tuning if it counts managed policies.
8. Docs: spec amendments (scope, R6, R18, R20, R21, R44, deploy flow step 5, threat model assets, X10), telegram-notifications R3 sentence, new decision superseding decision-14 point 9, docs/deploy.md, docs/security.md, docs/workspaces.md, docs/telegram.md, docs/telegram-setup.md, deploy.sh header, setenv.sh.example, CHANGELOG; doc examples proposed, not written.
9. Suites (infra, cli), bin/verify-docs.sh, bash -n; journal, masterplan, Done.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Decisions and assumptions:
- Matching TELEGRAM_PRINCIPAL happens on the parsed entry text (same parser as ISOLATED_PRINCIPALS), before any AWS call, so every refusal leaves the stub call log empty.
- The bound plane's boundary names the registry table (and its sub-resources) instead of every SCH table only when the inbound channel is on; a notifications-only binding keeps the broad deny, because the notifier needs no DynamoDB.
- The Telegram session policy moved from an inline AWS::IAM::Policy to AWS::IAM::ManagedPolicy (new logical ID TelegramInteractionSessionManagedPolicy, same name and document) with output TelegramInteractionPolicyArn; SharedRuntimePolicyArns is unchanged. The first deploy swaps the policy on the shared role (seconds of AccessDenied on the tables for a running isolation-off session).
- Plane parameters: TelegramBinding, TelegramBotToken (NoEcho), TelegramChatId, TelegramCommandsTable, TelegramRoutingTable, TelegramPolicyArn. Token and chat id come from the deploy environment (NoEcho parameters cannot be read back); tables and policy from the runtime stack outputs. Empty on every other plane. Half bindings are inert.
- Watchdog: an owner-tree workspace without a topic mapping is never announced (no plain-chat fallback); its pending promise ages out unsent. Chosen over passing the bound owner prefix to the watchdog, which would have added a runtime-stack parameter for the same effect.
- Verify scripts: the binding is a plane-stack parameter a listed user may not be able to read, so the exported TELEGRAM_PRINCIPAL is the signal for the skip.
- deploy.sh honors the helper's verdict when TELEGRAM_PRINCIPAL is set even with isolation off (the helper failure is otherwise a warning on isolation-off stacks).
- Doc examples (a setenv snippet with TELEGRAM_PRINCIPAL, a verify-script run as the bound principal) proposed to the maintainer, not written.

Verification: infra suite 226 OK (new and updated: test_isolation_plan 4 Telegram cases, test_isolation_templates 4, test_isolation_deploy 3 plus refused combinations, test_task_watchdog_handler 2), cli suite 678 OK (verify-target preflight), bin/verify-docs.sh OK, bash -n on deploy.sh and the verify scripts. Not done here: a live deploy with TELEGRAM_PRINCIPAL and the Telegram verify scripts as the bound principal (operator-side).
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Telegram works again on an isolated stack for one listed principal. New deploy variable TELEGRAM_PRINCIPAL=<entry>: the preflight (infra/isolation_plan.py) refuses the Telegram switches without it, and it without Telegram, without isolation, with several entries or an unlisted entry, before any stack changes; the plan marks the bound plane. infra/user_plane.yaml: TelegramBinding, TelegramBotToken (NoEcho), TelegramChatId, TelegramCommandsTable, TelegramRoutingTable, TelegramPolicyArn; the bound plane gets the shared Telegram environment, the Telegram policy on its role and a boundary that names the registry table instead of every SCH table; every other plane and every half binding is unchanged. infra/agent_runtime.yaml: the Telegram session policy is a customer managed policy with the TelegramInteractionPolicyArn output, outside SharedRuntimePolicyArns. infra/deploy.sh passes the values to the bound plane only and names it in the summary. infra/task_watchdog_handler.py never falls back to the plain chat for an owner-tree workspace without a topic mapping. bin/verify-telegram-*.sh skip on an isolated stack unless TELEGRAM_PRINCIPAL is exported. Spec per-principal-isolation (R6, R18, R20, R21, R44, X10, I9), telegram-notifications R3, telegram-interaction R1, decision-17 (supersedes decision-14 point 9), deploy.sh header, setenv example, docs/deploy.md, security.md, workspaces.md, telegram.md, telegram-setup.md, CHANGELOG and the spec index updated. Verified: infra suite 226 OK, cli suite 678 OK, bin/verify-docs.sh OK, bash -n, CloudFormation ValidateTemplate accepts the plane template (24 parameters). Operator-side: a live deploy with TELEGRAM_PRINCIPAL and the Telegram verify scripts as the bound principal.
<!-- SECTION:FINAL_SUMMARY:END -->
