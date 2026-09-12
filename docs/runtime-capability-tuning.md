# Runtime capability tuning — operator guide

How to shape what the SCH AgentCore Runtime (the microVM running your agent) is allowed
to do in your AWS account, at deploy time — without editing the CloudFormation template
and without losing session data.

Normative spec: [`docs/specs/security/runtime-capability-tuning.md`](specs/security/runtime-capability-tuning.md).

## The idea in one paragraph

By default the runtime execution role can invoke **every** Bedrock model, holds the AWS
managed **ReadOnlyAccess** policy, and has **no** access to other AI services. Deploy-time
environment variables let you widen it (opt in to Transcribe, Textract, Rekognition,
Polly, Comprehend) or narrow it (allow-list Bedrock models, remove ReadOnlyAccess, remove
Bedrock entirely). Tuning changes deploy as an ordinary stack update — the runtime image
is untouched and **no session storage is reset**.

## The knobs

All knobs are environment variables read by the deploy — `sch deploy` and
`./infra/deploy.sh` are the same script, and `sch setup --deploy` passes your
environment through, so every example below works from a packaged install too.
On an existing deployment, apply them with `-s` (stacks only): a deploy without
`-s` rebuilds the image, and a rebuilt image is what creates a new runtime version
and resets session storage — tuning itself never does ([What creates a new
runtime version](deploy.md#what-creates-a-new-runtime-version)).
Unset = inert default; the role is then byte-identical to a deployment without
this feature.

Like every deploy-time switch, these are re-read on each deploy: a knob you stop
passing reverts to its default on the next run. Put the ones you want to keep in
`infra/setenv.sh` (see [Deploy-time
switches](deploy.md#deploy-time-switches-optional-features)).

| Variable | Values | Default | Effect |
| --- | --- | --- | --- |
| `RUNTIME_CAPABILITIES` | comma list from `transcribe,textract,rekognition,polly,comprehend` | *(empty)* | Grants each listed service's API actions to the runtime role. |
| `RUNTIME_BEDROCK_ACCESS` | `true` / `false` | `true` | `false` removes **every** Bedrock model grant (both the classic and the OpenAI-compatible "Mantle" plane) — for bring-your-own-credentials deployments. |
| `RUNTIME_BEDROCK_MODEL_ALLOWLIST` | comma-separated model-ID patterns (may contain `*`) | *(empty = all models)* | Allows only the matching models. Each pattern is applied to both the inference-profile and the foundation-model ARN form, in any region. |
| `RUNTIME_AWS_API_READ` | `true` / `false` | `true` | `false` removes the `ReadOnlyAccess` managed policy (the aws-mcp MCP server loses its read access). |
| `RUNTIME_DATA_BUCKET_ARN` | S3 bucket ARN | *(empty)* | When set, enabled capabilities may read objects from that bucket and write under `transcribe/` and `textract/` (needed for async Transcribe/Textract jobs). |
| `RUNTIME_EXTRA_POLICY_JSON` | an IAM policy document (JSON) | *(empty)* | Escape hatch: attaches one extra policy with anything the catalog does not cover. You own it. |

## Use cases

### a) "Everything as before, no extra tool access"

Deploy plainly:

```bash
./infra/deploy.sh
```

All models available, ReadOnlyAccess attached, no capability policies. This is the
default — nothing to set.

### b) "All Haiku and Sonnet versions (eu profiles), plus Transcribe and Textract, ReadOnly kept"

```bash
RUNTIME_CAPABILITIES=transcribe,textract \
RUNTIME_BEDROCK_MODEL_ALLOWLIST='*anthropic.claude-sonnet*,*anthropic.claude-haiku*' \
./infra/deploy.sh -s
```

One wildcard pattern covers every version of the family, every regional profile prefix
(`eu.`, `us.`, `global.`), and both ARN forms — new model releases are allowed
automatically without a redeploy. If you prefer strict version pinning, list exact IDs
instead (e.g. `eu.anthropic.claude-sonnet-4-6`) — then a new version requires a redeploy.

If you use **async** Transcribe/Textract jobs (audio/document files in S3), also set:

```bash
RUNTIME_DATA_BUCKET_ARN=arn:aws:s3:::my-media-bucket
```

Without it, inline-bytes APIs still work (Rekognition detection, Textract sync, Polly,
Comprehend); only the S3-based job legs are denied.

### c) "No Bedrock at all (my own OpenCode credentials), no ReadOnly, but Textract"

```bash
RUNTIME_BEDROCK_ACCESS=false \
RUNTIME_AWS_API_READ=false \
RUNTIME_CAPABILITIES=textract \
./infra/deploy.sh -s
```

The role then holds only: the image/ECR and logging plumbing, the checkpoint-bucket
access, and Textract. Model inference runs on the per-user API keys configured in
`~/.sch/env` (see [`provider keys`](specs/providers-models/user-provider-keys.md)).

### d) "A service the catalog does not know"

Use the escape hatch with an IAM policy document (trusted-deployment decision):

```bash
RUNTIME_EXTRA_POLICY_JSON='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["translate:TranslateText"],"Resource":"*"}]}' \
./infra/deploy.sh -s
```

### Combinations with the earlier features

All knobs compose with the existing `ENABLE_*` switches
(`ENABLE_SESSION_IMAGE_REBUILD`, `ENABLE_TELEGRAM_INTERACTION`, ...) — they touch
different permissions.

## What you should know

- **Typos fail silently.** A misspelled model pattern or an invalid JSON ARN simply
  matches nothing: the deploy succeeds and the session gets `AccessDenied` at runtime.
  Capability names are the exception — they are validated and the deploy fails fast.
- **Debugging denials.** `bin/verify-runtime-iam-tuning.sh` runs
  `iam:SimulatePrincipalPolicy` against the real role to show exactly what is allowed
  and denied for a given tuning.
- **ReadOnlyAccess overlap.** The attached read-only managed policy already grants
  read-classed catalog actions — `polly:SynthesizeSpeech` and the whole
  `comprehend:Detect*` family among them — so those calls can succeed even with the
  capability disabled. Enable/disable verification therefore uses actions outside that
  overlap (e.g. `textract:StartDocumentTextDetection`), and
  `bin/verify-runtime-iam-tuning.sh` preflights its probe against the attached
  `ReadOnlyAccess` version.
- **Mantle plane.** OpenAI-family models (GPT, `openai.*`) are served by a separate
  Bedrock plane. A Sonnet/Haiku allow-list removes it automatically; a pattern
  containing `openai.` or `gpt` keeps it.
- **No reset.** Tuning never bumps the runtime version: applied with `-s`, running
  sessions survive and the change applies to new invocations. (Without `-s` the deploy
  also rebuilds the image, and the rebuild does bump it.)
- **Rollback.** Re-run the deploy with the variables unset (or set to their defaults)
  to return to the standard posture.

## Cost note

Enabled capabilities and model access have per-call AWS costs. Budgets and alarms are a
separate hardening workstream — enabling `textract` does not, by itself, add any cost
control.
