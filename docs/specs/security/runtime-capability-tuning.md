# Runtime capability tuning

> Domain: [Security](../README.md) · Status: Implemented

## Purpose

Let an operator shape the AgentCore Runtime execution role's permissions at deploy time —
both to GRANT additional AWS service access (Transcribe, Textract, Rekognition, Polly,
Comprehend) and to RESTRICT existing broad grants (Bedrock model invocation, the
`ReadOnlyAccess` managed policy) — without editing the CloudFormation template and without
resetting session storage.

## Scope

In scope:
- Deploy-time parameters that shape the runtime execution role (`AgentRuntimeRole`).
- The v1 curated capability catalog (five AI enrichment services) and its action sets.
- Bedrock model allow-listing across both the classic inference plane and the
  bedrock-mantle OpenAI-compatible plane.
- The raw policy-JSON escape hatch for anything outside the catalog.
- The deploy surface: `infra/deploy.sh` env-var switches and `sch setup --deploy` flags.

Out of scope:
- The base execution-role policy itself ([runtime-provisioning](../platform/runtime-provisioning.md)).
- A curated least-privilege read-replacement for `ReadOnlyAccess` (deferred to the
  Hardening roadmap item "security review of IAM scoping").
- Cost controls for enabled capabilities (budgets/alarms, Hardening roadmap).
- Usage observability (CloudTrail covers it; nothing extra is built).

## Requirements

**R1.** The stack SHALL expose the tuning as CloudFormation parameters with inert
defaults: with every parameter at its default, the deployed execution role SHALL be
byte-identical to a deployment of the template before this feature existed.

**R2.** Because tuning only changes IAM policies (never the runtime image or
`ApplicationVersion`), applying a tuning change through a stack-only deploy SHALL be
an ordinary in-place stack update with no runtime-version bump and no session-storage
reset. (A deploy that also rebuilds the image bumps the version because of the
rebuild, not the tuning: [runtime-provisioning](../platform/runtime-provisioning.md)
R15c.)

**R3. — Capability catalog.** The stack SHALL support a comma-separated
`RuntimeCapabilities` parameter whose entries are drawn from the v1 catalog:
`transcribe`, `textract`, `rekognition`, `polly`, `comprehend`. Each enabled capability
SHALL attach one dedicated conditional `AWS::IAM::Policy` to the execution role,
following the existing conditional-policy pattern: with a capability off, its policy
resource does not exist and the role is unchanged. Because CloudFormation condition
functions cannot test whether a comma list contains an entry, `deploy.sh` validates the
list and passes one derived boolean parameter per capability
(`RuntimeCap<Name>Enabled`, default `'false'`); the template conditions gate on those.

**R4.** Each capability's policy SHALL grant a fixed, documented action set (below) with
`Resource: "*"`, because these service APIs do not support resource-level permissions.
The v1 action sets:

| Capability | Actions |
| --- | --- |
| `transcribe` | `transcribe:StartTranscriptionJob`, `GetTranscriptionJob`, `ListTranscriptionJobs`, `CreateVocabulary`, `GetVocabulary`, `ListVocabularies`, `UpdateVocabulary`, `DeleteVocabulary` |
| `textract` | `textract:DetectDocumentText`, `AnalyzeDocument`, `AnalyzeExpense`, `StartDocumentTextDetection`, `GetDocumentTextDetection`, `StartDocumentAnalysis`, `GetDocumentAnalysis`, `StartExpenseAnalysis`, `GetExpenseAnalysis` |
| `rekognition` | `rekognition:DetectLabels`, `DetectModerationLabels`, `DetectText`, `DetectFaces`, `RecognizeCelebrities`, `StartLabelDetection`, `GetLabelDetection`, `StartTextDetection`, `GetTextDetection`, `StartContentModeration`, `GetContentModeration`, `StartFaceDetection`, `GetFaceDetection` |
| `polly` | `polly:SynthesizeSpeech`, `DescribeVoices` |
| `comprehend` | `comprehend:DetectDominantLanguage`, `DetectEntities`, `DetectKeyPhrases`, `DetectPiiEntities`, `DetectSentiment`, `DetectSyntax`, `DetectTargetedSentiment`, `DetectToxicContent`, `ClassifyDocument`, `ListDocumentClassifiers`, `DescribeDocumentClassifier` |

Each action name SHALL be verified against the AWS service authorization reference
during implementation; a mismatch is a spec bug to fix in the same change.

**R5. — Data bucket.** When the optional `RuntimeDataBucketArn` parameter is set, every
enabled capability policy SHALL additionally grant `s3:GetObject` on the bucket's
objects (`<bucket-arn>/*`) and `s3:PutObject` on `<bucket-arn>/transcribe/*` and
`<bucket-arn>/textract/*` — the S3 legs of async Transcribe/Textract jobs. Sync APIs
(inline bytes: Rekognition detect, Textract sync, Polly, Comprehend) SHALL work with no
data bucket configured. When unset, no capability policy SHALL contain any S3 statement.

**R6. — Bedrock model allow-list.** The `RuntimeBedrockModelAllowlist` parameter SHALL
accept a comma-separated list of model or inference-profile ID patterns. Each entry is
inserted verbatim into BOTH resource ARN forms
(`arn:aws:bedrock:*:*:inference-profile/<entry>` and
`arn:aws:bedrock:*::foundation-model/<entry>`), region-wildcarded, preserving the
cross-region routing correctness documented on the base policy. The inference-profile
form is ACCOUNT-wildcarded (the pre-tuning broad grant is account-scoped) because
CloudFormation's `Fn::Join` only accepts literal delimiters, so the per-entry ARN prefix
must be a literal string; this grants nothing extra — Bedrock model invocation cannot
cross accounts, and the runtime only ever calls its own. An entry MAY contain IAM
wildcards (`*`), which selects every version/variant matching the pattern — e.g.
`*anthropic.claude-sonnet*` allows ALL Sonnet versions across ALL regions and BOTH ARN
forms with one entry (regional `eu.`/`us.`/`global.` profile prefixes included). A
pattern therefore never needs per-region or per-plane repetition. When the parameter is
empty (default), the Bedrock grants SHALL be exactly today's (all inference profiles,
all foundation models, any region). An entry of bare `*` is equivalent to the default
broad grant and SHOULD be written as an unset parameter instead.

**R7. — Bedrock access toggle.** The `RuntimeBedrockAccess` parameter ('true' = the
default; 'false') SHALL control whether the execution role holds ANY Bedrock model
invocation grants. With 'false', the role SHALL carry no Bedrock invoke grant on either
plane — no classic `bedrock:Invoke*` statements, no bedrock-mantle statements —
regardless of the allow-list. This is the shape for a deployment that runs inference
exclusically on per-user provider keys (`~/.sch/env`), never on the execution role.

**R8. — Mantle gating.** When `RuntimeBedrockAccess` is 'true' (default), the
bedrock-mantle statement (`bedrock-mantle:*` on `project/default`) SHALL be granted when
the allow-list is empty, and SHALL be withheld when the allow-list is non-empty but
contains no Mantle-served (OpenAI-family) model ID. A claude-only allow-list therefore
blocks OpenAI-family models on BOTH planes. Because entries may be wildcard patterns,
the OpenAI-family detection SHALL handle patterns too (a pattern is OpenAI-family when
its text matches an OpenAI-family marker such as `openai.` or `gpt`), and the detection
rule SHALL be documented in the template next to the condition. With
`RuntimeBedrockAccess='false'`
the Mantle plane is absent per R7; the allow-list plays no role. Because CloudFormation
condition functions cannot test substrings either, `deploy.sh` evaluates the marker
detection (an entry is OpenAI-family when its text contains `openai.` or `gpt`,
case-insensitive) and passes it as the derived boolean parameter
`RuntimeBedrockAllowlistHasOpenAIFamily` (default `'false'`); the rule is documented in
the template next to the Mantle condition.

**R9. — ReadOnlyAccess toggle.** The `RuntimeAwsApiRead` parameter SHALL control the
`ReadOnlyAccess` managed policy ('true' = attached, the default; 'false' = not attached).
The spec consumer is warned in template comments: with it off, the aws-mcp MCP server's
read operations fail unless the escape hatch or a future curated read capability
back-fills them.

**R10. — Escape hatch.** The `RuntimeExtraPolicyJson` parameter (default `''`) SHALL, when
non-empty, be attached as ONE additional `AWS::IAM::Policy` on the execution role, in
addition to all other tuning. It is an operator-owned trust decision; the template and
this spec SHALL say so. CloudFormation validates its JSON syntax; no semantic validation
is performed.

**R11. — Deploy surface.** `infra/deploy.sh` SHALL expose each parameter as an env-var-only
switch following the existing convention (e.g. `RUNTIME_CAPABILITIES`,
`RUNTIME_BEDROCK_ACCESS`, `RUNTIME_BEDROCK_MODEL_ALLOWLIST`, `RUNTIME_AWS_API_READ`,
`RUNTIME_DATA_BUCKET_ARN`, `RUNTIME_EXTRA_POLICY_JSON`). `sch setup --deploy` SHALL gain
NO parameters: it remains a thin wrapper that invokes `infra/deploy.sh`, and env vars set
by the caller flow through to it unchanged (the CLI adds no deploy surface and no
flag→env mapping code).

**R12. — Validation.** deploy.sh SHALL validate `RUNTIME_CAPABILITIES` entries against the
known catalog and fail fast, listing the valid names. Model IDs and the JSON escape hatch
SHALL NOT be validated: a misspelled model ID or capability ARN silently matches nothing
and IAM denies at runtime. This silent-deny failure mode SHALL be documented (template
comments + this spec), with `iam:SimulatePrincipalPolicy` (verify script) as the
debugging tool.

**R13. — Verification.** A live capability check (`bin/verify-runtime-iam-tuning.sh`)
SHALL update the stack with tuning set, inspect the effective role policies, and run
`iam:SimulatePrincipalPolicy` against the real role to assert at minimum: an enabled
capability is allowed; a non-allowlisted Bedrock model is denied; the Mantle plane is
denied under a claude-only allow-list; and a disabled capability is denied. Assertions
on capability denials SHALL use actions outside the `ReadOnlyAccess` overlap: the
attached managed-policy version already grants read-classed catalog actions —
`polly:SynthesizeSpeech` and the whole `comprehend:Detect*` family (verified against
the live policy v188, 2026-08-29) — so those are allowed even with their capability
disabled. That overlap is expected behavior, not a tuning bug; the verify script
preflights its chosen denial probe against the attached `ReadOnlyAccess` version so an
AWS policy update cannot silently invalidate the assertion.

## Behavior

- Default deploy: nothing changes — the role is byte-identical to today's.
- `RUNTIME_CAPABILITIES=transcribe,rekognition ./deploy.sh` adds the two capability
  policies; with `RUNTIME_DATA_BUCKET_ARN` also set, async Transcribe jobs can read
  inputs from and write transcripts to that bucket.
- `RUNTIME_BEDROCK_MODEL_ALLOWLIST=eu.anthropic.claude-sonnet-4-6` narrows model
  invocation to that one model ID (both ARN forms) and drops the Mantle plane.
- `RUNTIME_AWS_API_READ=false` removes `ReadOnlyAccess` (aws-mcp MCP read stops working
  unless back-filled via the escape hatch).
- `RUNTIME_BEDROCK_MODEL_ALLOWLIST='*anthropic.claude-sonnet*,*anthropic.claude-haiku*'`
  narrows model invocation to every Sonnet and Haiku version, in any region, on both
  ARN forms — and drops the Mantle plane.
- `RUNTIME_BEDROCK_ACCESS=false RUNTIME_CAPABILITIES=textract RUNTIME_AWS_API_READ=false`
  strips every Bedrock grant (both planes) for a bring-your-own-credentials deployment,
  keeps only the Textract capability — no ReadOnlyAccess either.
- Any combination redeploys in place; running sessions are not reset.

## Invariants

- **I1.** Defaults are inert: the default-deployed role is byte-identical to the
  pre-feature role.
- **I2.** Tuning never resets session storage: no `ApplicationVersion` bump is triggered
  by, or required for, a tuning change.
- **I3.** The template is the single source of truth for every granted permission; no
  permission is granted outside it (capabilities, allow-list, and escape hatch are all
  expressed in the template).
- **I4.** A claude-only (non-OpenAI) allow-list leaves no OpenAI-family model reachable
  through either Bedrock plane.
- **I5.** With `RuntimeBedrockAccess='false'`, no Bedrock invocation is possible through
  the execution role on either plane, whatever the allow-list says.

## Cross-references

- [Operator guide](../../runtime-capability-tuning.md) — use cases and worked deploy examples for this feature.
- [runtime-provisioning](../platform/runtime-provisioning.md) — the base execution role,
  conditional-policy pattern, deploy/rollback flow.
- [session-image-rebuild](../platform/session-image-rebuild.md) — precedent for
  opt-in session capability + conditional IAM policy.
- Code: `infra/agent_runtime.yaml` (parameters, capability policies, allow-list
  expansion), `infra/deploy.sh` (env-var switches, capability validation),
  `bin/verify-runtime-iam-tuning.sh` (live check).
