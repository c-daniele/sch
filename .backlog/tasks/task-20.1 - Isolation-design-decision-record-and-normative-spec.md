---
id: TASK-20.1
title: 'Isolation design: decision record and normative spec'
status: Done
assignee:
  - '@opencode'
created_date: '2026-09-27 14:50'
updated_date: '2026-09-27 15:25'
labels:
  - security
dependencies: []
parent_task_id: TASK-20
priority: high
type: docs
ordinal: 15000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Slice 1 of TASK-20. Write the design that the other subtasks implement: bound identities per entry form, the per-principal plane stack, the storage layout, the resource-policy lock, the execution and access roles, the bucket policy, the threat model and the residual risks. Context, decisions, defaults, lessons and constraints live in the parent TASK-20; read it first and follow it. Headless run: apply the defaults, record every assumption in the implementation notes, and stop if the dependency is not Done. Work on `feat/task-20`, commit at the end, never push.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 A decision record (backlog decision create) records the final defaults of TASK-20, including one stack per principal and unverified sso usernames, with the reasons
- [x] #2 A normative spec under docs/specs/security/ describes the bound identities, the deploy-time planes, the storage layout, the threat model (who administers the boundary) and the residual risks (ReadOnlyAccess configuration reads, plane creation window, unverified sso usernames, simulator limits)
- [x] #3 docs/specs/README.md and the existing security specs link the new spec, and statements they make that the design changes are marked or corrected
- [x] #4 The implementation plans of TASK-20.2 to TASK-20.5 are written in those tasks, consistent with the spec
<!-- AC:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [x] #1 Relevant suites pass (cli, infra, tunnel, image-side tests with sandboxed paths) and bin/verify-docs.sh when docs changed
- [x] #2 Implementation notes list every assumption and the verification results; a journal entry is created and MASTERPLAN.md updated as AGENTS.md requires
- [x] #3 Work committed on feat/task-20 with conventional commits, never pushed
<!-- DOD:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Research current code paths (template, deploy.sh, registry handler, shim S3 keys, CLI reads) and AWS facts (AgentCore resource policies, service authorization actions, CFN ResourcePolicy type, IAM policy variables).
2. Record the final TASK-20 defaults and the design choices they leave open in a decision record (backlog decision create).
3. Write docs/specs/security/per-principal-isolation.md (Proposed): bound identities, owner key, planes, runtime locks, roles, boundary, bucket policy, storage layout, registry and CLI contract, threat model, residual risks.
4. Link it from docs/specs/README.md and the existing security specs; mark statements the design changes; correct false isolation claims in docs/security.md and SECURITY.md.
5. Write the implementation plans of TASK-20.2 to TASK-20.5.
6. Run suites and bin/verify-docs.sh, journal entry, masterplan, commit on feat/task-20.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Assumptions (headless run, defaults of TASK-20 applied):
- Storage layout: owner segment o.<16 hex> inserted after each top-level prefix (checkpoints/o.<k>/<ws>/...) rather than a top-level owners/ tree, so the existing prefix-based lifecycle rules and the watchdog IAM patterns keep applying unchanged.
- Owner key = first 16 hex of sha256('<kind>:<boundId>'); registry ownerId = the full hash, so isolated namespaces never collide with today's sha256(callerArn) records.
- Cross-owner S3 denial is one constant bucket policy keyed on the plane-role name pattern and the role tag sch-owner via ${aws:PrincipalTag/sch-owner}, not per-owner statements (20 KB limit at about two dozen principals, and no runtime-stack change per added principal). Plane role trusts never allow sts:TagSession (a session tag would override the role tag).
- Registry finds a plane through one SSM parameter per plane (/<p>/<e>/planes/<ownerKey>), created by the plane stack after the locks. This adds read-only ssm:GetParameter to the registry role; TASK-20.3 AC #4 was reworded accordingly (only change to another task's criteria).
- Owner trees are denied to every non-SCH principal, deletes included; a removed owner's storage is purged by the owner before removal, by a boundary administrator, or by sch destroy.
- Telegram settings are refused by deploy.sh with isolation on (default 9 says not available to user runtimes; with the shared runtime locked nothing could use it).
- The boundary also denies reads of the owner's own runtime log group (pattern covers all SCH runtimes); deny of lambda:ListFunctions account-wide because it returns environment variables and cannot be resource-scoped.
- Shared policies become customer managed policies in every mode (one definition); runtime-capability-tuning R1/I1 byte-identity wording is marked as a planned change. Managed-policy size 6,144 vs 10,240 inline is residual risk X9.
- requestContext.identity.user as the principal-ID source is marked unverified in the spec (live check).
- Corrected on sight: SECURITY.md and docs/security.md claimed per-user isolation that is not enforced; runtime-provisioning I2 claimed every S3 write outside checkpoint prefixes fails, which is false with the data bucket or escape hatch.

Verification:
- Access Analyzer ValidatePolicy (placeholder account) on the spec's lock and bucket policies: no findings after dropping s3:ListBucketMultipartUploads from the s3:prefix statement (ERROR UNSUPPORTED_ACTION_FOR_CONDITION_KEY); spec updated.
- simulate-custom-policy (custom mode, explicit context): bucket policy - plane own get/put/list allowed; other owner tree, flat layout, untagged plane role, other-prefix and no-prefix listing denied; human on owner tree denied, human on flat layout allowed; registry delete on owner tree allowed. Lock policy (sso owner) - owner invoke allowed; same permission set other user invoke and stop denied; IAM user WebSocket invoke denied; registry stop allowed, registry invoke denied.
- Suites: cli 595 OK, infra 127 OK, tunnel npm test OK, image-side 505 run with sandboxed paths: only the 7 known TASK-22 failures. bin/verify-docs.sh passed.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Design slice of TASK-20. decision-14 records the final defaults (allow-list with user:/sso:/role: entries, unverified sso usernames, owners from unique IDs, one CloudFormation stack per principal instead of Fn::ForEach) and the choices made within them (constant tag-based bucket policy, one SSM plane-mapping parameter per plane, o.<key> owner segment, deletes denied outside SCH roles). New Proposed spec docs/specs/security/per-principal-isolation.md (R1-R48, I1-I8): bound identities, planes, runtime locks, execution/access roles, boundary, bucket policy, storage layout, registry/shim/CLI contract, threat model with boundary administrators, residual risks X1-X9 (ReadOnlyAccess reads, creation window, unverified sso usernames, simulator limits and more). Spec index and the security specs link it and mark planned changes; SECURITY.md, docs/security.md, docs/workspaces.md and runtime-provisioning I2 corrected. Plans written into TASK-20.2 to TASK-20.5; TASK-20.3 AC #4 reworded for the SSM read. Verified: Access Analyzer and custom-mode simulator on the spec's lock and bucket policy shapes (one invalid condition caught and fixed), cli/infra/tunnel suites green, image-side suite with sandboxed paths shows only the 7 known TASK-22 failures, bin/verify-docs.sh passed.
<!-- SECTION:FINAL_SUMMARY:END -->
