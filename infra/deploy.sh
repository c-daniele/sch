#!/bin/bash
# deploy.sh — SCH end-to-end deploy, in one linear pass:
#   bootstrap stack (ECR + CodeBuild build project + build-sources bucket)
#     -> image build/push (CodeBuild by default, -l for local Docker)
#     -> runtime stack (AgentCore Runtime pointing at the image just pushed).
#
# A first deploy on an empty account needs nothing else: no flags, no env vars,
# no preparatory run.
#
# Re-runnable: CloudFormation deploys use --no-fail-on-empty-changeset and the
# image build/push is repeatable. What a re-run does to the RUNTIME depends on
# whether it touches the image — see "What creates a new runtime version".
#
# Usage:
#   ./deploy.sh [options]
#
# Options (env vars with the same names also work):
#   -r REGION        AWS region                 (default: eu-west-1)
#   -p PROJECT_NAME  Project name               (default: sch)
#   -e ENVIRONMENT   Environment dev|stg|prd (default: dev)
#   -v VERSION       Image tag / ApplicationVersion (default: v1). A label for
#                    the build, not what makes it live: every image-building
#                    deploy reaches the runtime, with or without a new tag.
#   -s               Skip image build/push (only deploy CFN stacks). Without -v
#                    the runtime keeps the deployed tag AND digest, so the
#                    runtime version never changes; with -v it points the
#                    runtime at that (existing) tag's current digest.
#   -c               Build on CodeBuild (native arm64). This is the default;
#                    the flag is kept for backwards compatibility.
#   -l               Build locally with the Docker daemon instead of CodeBuild.
#                    CodeBuild is the default because AgentCore Runtime only
#                    accepts linux/arm64 images: CodeBuild builds natively,
#                    while a local build needs an arm64 host (or unusably slow
#                    QEMU emulation on x86_64). The build project is created by
#                    the bootstrap stack (step 1), so no preparation is needed:
#                    one run of this script does everything.
#                    The local corporate-CA base is available only with -l
#                    (image/certs/*.pem is never uploaded to CodeBuild).
#
# Env-var only switches:
#   BUILD_TIMEOUT=<seconds>                   (default: 2700, -c only)
#   ENABLE_WORKSPACE_REGISTRY=true|false      (default: false)
#   TELEGRAM_BOT_TOKEN=<token>                (default: empty = notifications off)
#   TELEGRAM_CHAT_ID=<chat id>                (default: empty = notifications off)
#     Telegram push notifications (add-telegram-notifications): both must be
#     set to activate the feature; they land on the runtime environment as
#     SCH_TELEGRAM_BOT_TOKEN / SCH_TELEGRAM_CHAT_ID (CFN NoEcho parameter for
#     the token). Empty (default) keeps the runtime byte-identical to a
#     deployment without the feature.
#   ENABLE_SESSION_IMAGE_REBUILD=true|false   (default: false)
#     Creates the CodeBuild project that lets a SESSION rebuild this image
#     (add-codebuild-image-rebuild). Independent of how a DEPLOY builds: the
#     deploy build project lives in the bootstrap stack and is always there.
#     Off by default: enabling it grants the runtime execution role three extra
#     permissions (upload build sources, start that one project, read its logs)
#     and creates a build role that can push to the project ECR repository.
#     Enable only where the agent is inside your trust boundary — see the
#     change's design.md, risk R1.
#   ENABLE_TELEGRAM_INTERACTION=true|false    (default: false)
#     Creates the inbound Telegram router (add-telegram-interaction): webhook
#     Lambda + API Gateway route + DynamoDB command/routing tables, plus the
#     runtime permissions to consume its own commands. Requires
#     TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID. deploy.sh generates the webhook
#     secret (or reuses TELEGRAM_WEBHOOK_SECRET if exported) and registers the
#     webhook with Telegram (setWebhook) after the stack deploy. Rollback:
#     redeploy with ENABLE_TELEGRAM_INTERACTION=false — deploy.sh then calls
#     deleteWebhook and the stack removes every router resource (change 1
#     keeps working unchanged).
#   TELEGRAM_WEBHOOK_SECRET=<secret>          (default: generated per deploy)
#   ENABLE_TASK_WATCHDOG=true|false           (default: true)
#     External task watchdog (add-task-liveness-safety): a Lambda invoked every
#     2 minutes by EventBridge that rewrites orphaned `running` task statuses
#     (heartbeat older than TASK_WATCHDOG_STALE_SECONDS) to `interrupted` and
#     notifies the operator when the Telegram parameters are set. It is the
#     only component that can observe the death of a microVM, so it is ON by
#     default; the notification half is optional, the state reconciliation is
#     not. Its IAM is Get/Put/List on the checkpoints/ prefix only, and it
#     never invokes the runtime (probing a dead session would wake it).
#     Rollback: redeploy with ENABLE_TASK_WATCHDOG=false (every watchdog
#     resource disappears, nothing else changes).
#   TASK_WATCHDOG_STALE_SECONDS=<seconds>     (default: 600)
#     Heartbeat age beyond which the watchdog reconciles. Deliberately much
#     larger than the 150s `sch status` uses to merely RENDER a suspect state.
#   TASK_WATCHDOG_NOTIFY_AFTER_SECONDS=<seconds> (default: 300)
#     Grace after a task's end before the watchdog re-sends a terminal
#     Telegram notification the microVM did not confirm as delivered
#     (at-least-once terminal delivery, TASK-28). Only meaningful with the
#     Telegram parameters set; the image must carry the same protocol
#     (records without it are never re-sent).
#   SCH_PI_DEFAULT_MODEL=<model-id>            (default: region-derived)
#     Pi-specific runtime model setting.
#   Runtime capability tuning (docs/specs/security/runtime-capability-tuning.md) —
#   deploy-time shaping of the runtime execution role. All defaults are inert:
#   unset variables deploy a role byte-identical to the pre-tuning one, and any
#   combination redeploys IN PLACE (no runtime version bump, no session reset).
#   RUNTIME_CAPABILITIES=<comma list>          (default: empty)
#     Opt-in AI enrichment capabilities granted to the runtime role. Valid
#     entries: transcribe, textract, rekognition, polly, comprehend — other
#     values fail the deploy fast (R12). Sync (inline-bytes) APIs work with
#     no further setup; async Transcribe/Textract job legs need a data bucket.
#   RUNTIME_DATA_BUCKET_ARN=<s3 bucket arn>    (default: empty)
#     When set, enabled capabilities may GetObject the bucket's objects and
#     PutObject under transcribe/ and textract/ (async job I/O only).
#   RUNTIME_BEDROCK_ACCESS=true|false          (default: true)
#     'false' strips EVERY Bedrock grant on both planes (classic InvokeModel
#     and bedrock-mantle) regardless of the allow-list — the shape for
#     bring-your-own-credentials deployments (per-user keys in ~/.sch/env).
#   RUNTIME_BEDROCK_MODEL_ALLOWLIST=<comma list of model-ID patterns>
#                                              (default: empty = all models)
#     Allows only the listed models. Entries may contain IAM wildcards
#     (e.g. '*anthropic.claude-sonnet*') and are applied to BOTH ARN forms
#     (inference-profile and foundation-model), in any region. A non-empty
#     list without an OpenAI-family entry ('openai.' or 'gpt' in the text)
#     also drops the bedrock-mantle plane. Misspelled entries silently match
#     nothing: IAM denies at runtime (R12) — debug with
#     bin/verify-runtime-iam-tuning.sh (iam:SimulatePrincipalPolicy).
#   RUNTIME_AWS_API_READ=true|false            (default: true)
#     'false' removes the ReadOnlyAccess managed policy — the aws-mcp MCP
#     server loses its read access unless back-filled via the escape hatch.
#   RUNTIME_EXTRA_POLICY_JSON=<IAM policy document (JSON)>  (default: empty)
#     Escape hatch: attaches ONE extra policy with anything the catalog does
#     not cover. Operator-owned trust decision; only its JSON syntax is
#     validated.
#   NOTE (add-user-provider-keys, BREAKING): the provider API keys are NO
#     LONGER deploy variables. ANTHROPIC_API_KEY / OPENCODE_API_KEY /
#     OPENROUTER_API_KEY / KILO_API_KEY left over in the operator's environment
#     are IGNORED here — the deployment manages no provider secret at all, and
#     Bedrock via execution role is the only global provider. Each user
#     configures their own keys in ~/.sch/env (mode 0600) and `sch` forwards them
#     in the payload of every invoke; see docs/cli.md, "Provider API keys". Upside:
#     rotating a key is no longer invasive maintenance (no new runtime version,
#     no session storage reset).
#
# !!! What creates a new runtime version — and what that resets !!!
#   AgentCore resolves the image reference ONCE, when it creates a runtime
#   version, and a tag-only reference (repo:v1) is never re-resolved: a
#   rebuild that keeps the same tag would push a new image that no runtime
#   version ever runs (TASK-30). So this script pins the runtime to the
#   DIGEST of the image it just pushed (repo@sha256:...; the tag stays in the
#   ApplicationVersion parameter and in SCH_IMAGE_VERSION inside the image),
#   and the runtime version changes exactly when:
#     - the image is rebuilt (any deploy without -s; container builds are not
#       reproducible, so rebuilding unchanged sources is a new digest too);
#     - -s -v <tag> points the runtime at another existing tag;
#     - the runtime environment changes (Telegram on/off, in-session rebuild
#       on/off, SCH_PI_DEFAULT_MODEL).
#   It does NOT change on -s without -v (deployed tag and digest reused) or on
#   capability tuning, registry, watchdog changes: those deploy in place.
#   Verify which build a runtime runs with the version and container URI
#   printed at the end, or `printenv SCH_IMAGE_DIGEST` inside a session.
#
#   A version update RESETS the session storage of EVERY session (worktrees,
#   OpenCode state) and the platform retires the microVMs of the superseded
#   version. Since sch-l2-durability-s3-checkpoint, this is no longer data
#   loss: each workspace is repopulated from its S3 checkpoint
#   (checkpoints/<workspace>/) on its first `sch shell <ws>` after the bump —
#   work never checkpointed on the old session is the one thing not
#   recoverable, so `sch stop` the workspaces you care about first.
#   EXCEPTION: the one migration deploy that introduces the checkpoint
#   mechanism itself (bucket + IAM not deployed yet, or no workspace has a
#   checkpoint yet) still resets storage with nothing to restore from — see
#   docs/deploy.md "Session Storage (Preview) Limits" for that one-time caveat.
#
# Rollback / teardown:
#   sch destroy                      # one command: both stacks, images, buckets
#   sch destroy --dry-run            # print the plan first
#   sch destroy --keep-checkpoints   # keep the L2 workspace data
#   (it empties the ECR repository and the buckets itself — a manual
#    delete-stack fails while they are non-empty)
#   !!! DeleteAgentRuntime (triggered by deleting the runtime stack) also
#   !!! DELETES ALL SESSION STORAGE associated with the runtime. The L2
#   !!! checkpoint bucket has DeletionPolicy: Retain and survives independently
#   !!! of the runtime stack (infra/agent_runtime.yaml) — it is not deleted by
#   !!! this teardown and must be removed separately if truly no longer needed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# setenv.sh is operator-local (gitignored: per-deployment secrets) and optional:
# every variable below has a default, so a fresh clone deploys without it.
# Copy setenv.sh.example to setenv.sh to pin deployment-specific values.
if [ -f "${SCRIPT_DIR}/setenv.sh" ]; then
    source "${SCRIPT_DIR}/setenv.sh"
fi

REGION="${REGION:-eu-west-1}"
PROJECT_NAME="${PROJECT_NAME:-sch}"
ENVIRONMENT="${ENVIRONMENT:-dev}"
if [ -n "${VERSION+x}" ]; then
    VERSION="${VERSION}"
    VERSION_SET=1
else
    VERSION="v1"
    VERSION_SET=0
fi
SKIP_IMAGE=0
BUILD_ON_CODEBUILD=1
BUILD_TIMEOUT="${BUILD_TIMEOUT:-2700}"
ENABLE_WORKSPACE_REGISTRY="${ENABLE_WORKSPACE_REGISTRY:-false}"
ENABLE_SESSION_IMAGE_REBUILD="${ENABLE_SESSION_IMAGE_REBUILD:-false}"
TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"
ENABLE_TELEGRAM_INTERACTION="${ENABLE_TELEGRAM_INTERACTION:-false}"
TELEGRAM_WEBHOOK_SECRET="${TELEGRAM_WEBHOOK_SECRET:-}"
ENABLE_TASK_WATCHDOG="${ENABLE_TASK_WATCHDOG:-true}"
TASK_WATCHDOG_STALE_SECONDS="${TASK_WATCHDOG_STALE_SECONDS:-600}"
TASK_WATCHDOG_NOTIFY_AFTER_SECONDS="${TASK_WATCHDOG_NOTIFY_AFTER_SECONDS:-300}"
SCH_PI_DEFAULT_MODEL="${SCH_PI_DEFAULT_MODEL:-}"
RUNTIME_CAPABILITIES="${RUNTIME_CAPABILITIES:-}"
RUNTIME_BEDROCK_ACCESS="${RUNTIME_BEDROCK_ACCESS:-true}"
RUNTIME_BEDROCK_MODEL_ALLOWLIST="${RUNTIME_BEDROCK_MODEL_ALLOWLIST:-}"
RUNTIME_AWS_API_READ="${RUNTIME_AWS_API_READ:-true}"
RUNTIME_DATA_BUCKET_ARN="${RUNTIME_DATA_BUCKET_ARN:-}"
RUNTIME_EXTRA_POLICY_JSON="${RUNTIME_EXTRA_POLICY_JSON:-}"

# Runtime capability tuning helpers (below; sourced verbatim by
# infra/test_runtime_tuning.py): validate RUNTIME_CAPABILITIES and derive the
# boolean template parameters from the two heuristics CloudFormation cannot
# express. The -h usage above is generated from this file's comments, hence
# the prose style.
  # Validates RUNTIME_CAPABILITIES against the catalog and fails fast listing
  # the valid names (spec R12); model IDs and the policy JSON are NOT
  # validated — a misspelled one silently matches nothing and IAM denies at
  # runtime. Derives the boolean parameters the template needs:
  # CloudFormation condition functions can neither test comma-list membership
  # nor match substrings, so the two heuristics are evaluated here (spec
  # R3/R8). Prints KEY='value' lines for eval; the caller checks the return
  # code BEFORE evaluating the output.
  runtime_tuning_resolve() {
    local valid="transcribe textract rekognition polly comprehend"
    local normalized="" entry flag out has_openai='false' allowlist=""
    local old_ifs="${IFS}"
    IFS=','
    for entry in ${RUNTIME_CAPABILITIES}; do
      entry="$(printf '%s' "${entry}" | tr -d '[:space:]')"
      [ -z "${entry}" ] && continue
      case " ${valid} " in
        *" ${entry} "*) ;;
        *)
          printf 'deploy: invalid RUNTIME_CAPABILITIES entry "%s"\n' "${entry}" >&2
          printf '  valid entries: %s\n' "${valid}" >&2
          IFS="${old_ifs}"
          return 2
          ;;
      esac
      normalized="${normalized:+${normalized},}${entry}"
    done
    IFS="${old_ifs}"
    out="TUNING_CAPS='${normalized}'"
    for entry in ${valid}; do
      case ",${normalized}," in
        *",${entry},"*) flag='true' ;;
        *) flag='false' ;;
      esac
      out="${out}
TUNING_CAP_$(printf '%s' "${entry}" | tr '[:lower:]' '[:upper:]')='${flag}'"
    done
    # OpenAI-family marker heuristic (spec R8): an allow-list entry is
    # Mantle-served when its TEXT contains 'openai.' or 'gpt' — case-
    # insensitive, so wildcard patterns are covered ('*openai.gpt-5*' has
    # both markers). Heuristic on purpose: the Mantle plane only serves
    # OpenAI-family models today. Entries are trimmed of surrounding
    # whitespace and empties are dropped; they are otherwise passed through
    # verbatim (spec R6).
    IFS=','
    for entry in ${RUNTIME_BEDROCK_MODEL_ALLOWLIST}; do
      entry="$(printf '%s' "${entry}" | tr -d '[:space:]')"
      [ -z "${entry}" ] && continue
      allowlist="${allowlist:+${allowlist},}${entry}"
      case "$(printf '%s' "${entry}" | tr '[:upper:]' '[:lower:]')" in
        *openai.*|*gpt*) has_openai='true' ;;
      esac
    done
    IFS="${old_ifs}"
    out="${out}
TUNING_HAS_OPENAI='${has_openai}'
TUNING_ALLOWLIST='${allowlist}'"
    printf '%s\n' "${out}"
  }
# (end runtime capability tuning helpers)

# Image digest helpers (below; sourced verbatim by infra/test_deploy_image_pin.py):
# the runtime stack pins the image by digest — see "What creates a new runtime
# version" above. Both functions call the `aws` found on PATH and set globals;
# the callers run under set -e, so a non-zero return stops the deploy.
  # Resolves IMAGE_DIGEST (sha256:...) for the tag ${VERSION} in ${ECR_REPO}.
  # Fails when the tag does not exist or has no digest: the stack about to be
  # deployed pins this exact digest, so a missing image must stop the deploy
  # here rather than surface as a runtime pull failure minutes later.
  resolve_image_digest() {
    local digest
    digest="$(aws ecr describe-images --repository-name "${ECR_REPO}" --region "${REGION}" \
        --image-ids "imageTag=${VERSION}" \
        --query 'imageDetails[0].imageDigest' --output text)" || {
      printf 'deploy: image %s not found in ECR\n' "${IMAGE_URI}" >&2
      return 1
    }
    case "${digest}" in
      sha256:*) IMAGE_DIGEST="${digest}" ;;
      *)
        printf 'deploy: could not resolve the digest of %s (got: %s)\n' "${IMAGE_URI}" "${digest}" >&2
        return 1
        ;;
    esac
  }
  # A stack-only deploy without an explicit version (-s, no -v/VERSION) must
  # not move the runtime: reuse the deployed ApplicationVersion AND ImageDigest
  # (one describe-stacks call). No stack leaves both untouched; a stack that
  # predates the ImageDigest parameter yields an empty digest, which keeps
  # its tag-only ContainerUri exactly as it is.
  reuse_deployed_image() {
    local stack="${PROJECT_NAME}-${ENVIRONMENT}-runtime" key value
    while IFS=$'\t' read -r key value; do
      [ -n "${value}" ] && [ "${value}" != "None" ] || continue
      case "${key}" in
        ApplicationVersion) VERSION="${value}" ;;
        ImageDigest) IMAGE_DIGEST="${value}" ;;
      esac
    done <<EOF
$(aws cloudformation describe-stacks --stack-name "${stack}" --region "${REGION}" \
    --query "Stacks[0].Parameters[?ParameterKey=='ApplicationVersion' || ParameterKey=='ImageDigest'].[ParameterKey,ParameterValue]" \
    --output text 2>/dev/null || true)
EOF
    return 0
  }
# (end image digest helpers)

# Validate + derive before anything else: a typo must fail the deploy fast,
# not after the image build (the tests exercise this through the -h path).
# The return code is checked BEFORE evaluating the output, because eval on
# the (empty) output of a failed run would otherwise succeed.
TUNING_RESOLVED="$(runtime_tuning_resolve)" || exit $?
eval "${TUNING_RESOLVED}"

while getopts "r:p:e:v:schl" opt; do
    case "${opt}" in
        r) REGION="${OPTARG}" ;;
        p) PROJECT_NAME="${OPTARG}" ;;
        e) ENVIRONMENT="${OPTARG}" ;;
        v) VERSION="${OPTARG}"; VERSION_SET=1 ;;
        s) SKIP_IMAGE=1 ;;
        c) BUILD_ON_CODEBUILD=1 ;;
        l) BUILD_ON_CODEBUILD=0 ;;
        h) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option"; exit 2 ;;
    esac
done

# Stack-only updates must not accidentally roll the AgentCore Runtime back to
# the template default image tag, nor move it to a rebuilt image: reuse the
# deployed tag and digest unless callers explicitly choose a version through
# VERSION or -v (TASK-30). Every other path sets IMAGE_DIGEST after the build
# or after resolving the explicit tag.
IMAGE_DIGEST=""
if [ "${SKIP_IMAGE}" -eq 1 ] && [ "${VERSION_SET}" -eq 0 ]; then
    reuse_deployed_image
fi

# The CodeBuild project used by a deploy lives in the bootstrap stack and is
# always present (bootstrap.yaml), so building on it implies nothing about the
# runtime's own capabilities: ENABLE_SESSION_IMAGE_REBUILD stays a pure opt-in
# for letting a SESSION rebuild the image, and a default deploy grants the
# runtime execution role no extra permission.

REPO_ROOT="$(dirname "${SCRIPT_DIR}")"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# Telegram notifications need BOTH values to activate (the runtime's opt-in
# gate requires both env vars non-empty). Half a configuration is a mistake,
# not a preference: say so instead of deploying a stack whose notifications
# are silently off. The most common cause of an unexpectedly empty value is a
# setenv-style helper that assigns without `export` — deploy.sh is a child
# process and only inherits exported variables.
if { [ -n "${TELEGRAM_BOT_TOKEN}" ] && [ -z "${TELEGRAM_CHAT_ID}" ]; } \
    || { [ -z "${TELEGRAM_BOT_TOKEN}" ] && [ -n "${TELEGRAM_CHAT_ID}" ]; }; then
    echo "deploy: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set together" >&2
    echo "  token: $([ -n "${TELEGRAM_BOT_TOKEN}" ] && echo set || echo empty), chat id: $([ -n "${TELEGRAM_CHAT_ID}" ] && echo set || echo empty)" >&2
    echo "  (using a setenv helper? it must 'export' the variables — a child process" >&2
    echo "   does not inherit plain shell assignments)" >&2
    exit 2
fi

# The interaction router (add-telegram-interaction) is meaningless without the
# notification channel it extends: refuse the half-configuration explicitly
# (the CFN condition would silently disable it — a mistake, not a preference).
if [ "${ENABLE_TELEGRAM_INTERACTION}" = "true" ] && [ -z "${TELEGRAM_BOT_TOKEN}" ]; then
    echo "deploy: ENABLE_TELEGRAM_INTERACTION=true requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID" >&2
    exit 2
fi

# Webhook secret (design D6): generated per deploy unless the caller pins it.
# Regeneration is harmless — setWebhook below re-registers the endpoint with
# the fresh secret in the same run that updates the Lambda's copy.
if [ "${ENABLE_TELEGRAM_INTERACTION}" = "true" ] && [ -z "${TELEGRAM_WEBHOOK_SECRET}" ]; then
    if command -v openssl >/dev/null 2>&1; then
        TELEGRAM_WEBHOOK_SECRET="$(openssl rand -hex 32)"
    else
        TELEGRAM_WEBHOOK_SECRET="$(od -vN 32 -An -tx1 /dev/urandom | tr -d ' \n')"
    fi
fi

ECR_REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
ECR_REPO="${PROJECT_NAME}-${ENVIRONMENT}"
IMAGE_URI="${ECR_REGISTRY}/${ECR_REPO}:${VERSION}"

log() { echo "==> $*"; }

CORPORATE_CA_BUILD_ARG=()
if compgen -G "${REPO_ROOT}/image/certs/*.pem" >/dev/null; then
    CORPORATE_CA_BUILD_ARG=(--build-arg RUNTIME_BASE=corporate-ca)
    log "using local corporate CA certificates"
fi

log "deploy: project=${PROJECT_NAME} env=${ENVIRONMENT} region=${REGION} version=${VERSION}"

# add-user-provider-keys (design D5, BREAKING): the provider API keys are a
# per-USER property now — read by `sch` from ~/.sch/env and forwarded in the
# invoke payload. This script neither reads nor passes them: a leftover
# ANTHROPIC_API_KEY / OPENCODE_API_KEY / OPENROUTER_API_KEY / KILO_API_KEY in
# the operator's environment is silently ignored (deliberately silent: it is a
# normal thing to have exported for other tools, and naming it here would only
# invite putting it back). The runtime carries no provider secret at all.

# --- 1. Bootstrap stack ---------------------------------------------------------
# ECR repository + the ARM CodeBuild project that builds the image + the bucket
# that carries the build source. Everything the build needs, before the runtime
# exists — see bootstrap.yaml for why the build project cannot live in the
# runtime stack.
BOOTSTRAP_STACK="${PROJECT_NAME}-${ENVIRONMENT}-bootstrap"
log "deploying bootstrap stack (${BOOTSTRAP_STACK})"
aws cloudformation deploy \
    --template-file "${SCRIPT_DIR}/bootstrap.yaml" \
    --stack-name "${BOOTSTRAP_STACK}" \
    --parameter-overrides "ProjectName=${PROJECT_NAME}" "Environment=${ENVIRONMENT}" \
    --capabilities CAPABILITY_NAMED_IAM \
    --region "${REGION}" \
    --no-fail-on-empty-changeset

# --- 2. Build & push image (linux/arm64, required by AgentCore) ------------------
# Two engines, same result: the CodeBuild project (default) or the local
# Docker daemon (-l). CodeBuild is the default because AgentCore Runtime only
# accepts linux/arm64: CodeBuild builds natively, while a local build needs an
# arm64 host and, on x86_64, QEMU binfmt handlers with every RUN of this image
# (dnf, npm, uv, aws-cli) running emulated.
build_on_codebuild() {
    local project="${PROJECT_NAME}-${ENVIRONMENT}-image-build"
    local bucket="${PROJECT_NAME}-${ENVIRONMENT}-build-sources-${ACCOUNT_ID}"
    local source_key="builds/deploy/source.zip"

    command -v zip >/dev/null 2>&1 || { echo "deploy: 'zip' is required to build on CodeBuild (or use -l)" >&2; exit 1; }

    # Step 1 creates this project, so a missing one means the bootstrap stack is
    # not the one this script expects (e.g. a pre-rename deployment).
    if [ "$(aws codebuild batch-get-projects --names "${project}" --region "${REGION}" \
            --query 'length(projects)' --output text 2>/dev/null || echo 0)" != "1" ]; then
        cat >&2 <<EOF
deploy: CodeBuild project ${project} does not exist, although stack
  ${BOOTSTRAP_STACK} was just deployed. Check that stack in the console:
  its ImageBuildProjectName output must name that project.
  To build without CodeBuild in the meantime: ${0} -l
EOF
        exit 1
    fi

    # The CONTENTS of image/ go at the zip root: the build context is exactly
    # image/ and the buildspec can rely on ./Dockerfile. certs/*.pem are never
    # uploaded — a corporate CA base is a local-build-only affordance.
    local tmp_dir zip_path
    tmp_dir="$(mktemp -d -t sch-deploy-build.XXXXXX)"
    zip_path="${tmp_dir}/source.zip"
    # EXIT, not RETURN: the error paths below exit the script outright.
    # shellcheck disable=SC2064
    trap "rm -rf '${tmp_dir}'" EXIT
    log "packaging ${REPO_ROOT}/image/ (source root = image/ contents)"
    ( cd "${REPO_ROOT}/image" && zip -qr "${zip_path}" . \
        -x '*.pyc' -x '__pycache__/*' -x '*/__pycache__/*' -x 'certs/*.pem' )

    log "uploading to s3://${bucket}/${source_key}"
    aws s3 cp "${zip_path}" "s3://${bucket}/${source_key}" --region "${REGION}" --only-show-errors

    # The buildspec lives in the project (bootstrap.yaml), not here: it is
    # operator-owned either way, and one definition means nothing to keep in
    # sync. Only the destination tag is per-build input.
    log "starting CodeBuild project ${project} (tag ${VERSION})"
    local build_id
    build_id="$(aws codebuild start-build \
        --project-name "${project}" \
        --region "${REGION}" \
        --source-location-override "${bucket}/${source_key}" \
        --environment-variables-override \
            "name=SCH_DEPLOY_TAG,value=${VERSION},type=PLAINTEXT" \
        --query 'build.id' --output text)"
    [ -n "${build_id}" ] && [ "${build_id}" != "None" ] || { echo "deploy: could not start the build" >&2; exit 1; }
    log "build id: ${build_id}"

    local log_group="/aws/codebuild/${project}"
    local elapsed=0 last_phase="" status="" phase=""
    while :; do
        read -r status phase <<EOF
$(aws codebuild batch-get-builds --ids "${build_id}" --region "${REGION}" \
    --query 'builds[0].[buildStatus,currentPhase]' --output text)
EOF
        if [ "${phase:-}" != "${last_phase}" ]; then
            log "    [$(date -u +%H:%M:%S)] ${phase:-?}"
            last_phase="${phase:-}"
        fi
        [ "${status}" = "IN_PROGRESS" ] || break
        if [ "${elapsed}" -ge "${BUILD_TIMEOUT}" ]; then
            echo "deploy: build still IN_PROGRESS after ${BUILD_TIMEOUT}s; follow it with:" >&2
            echo "  aws logs tail ${log_group} --follow --region ${REGION}" >&2
            exit 1
        fi
        sleep 10
        elapsed=$((elapsed + 10))
    done

    if [ "${status}" != "SUCCEEDED" ]; then
        echo "deploy: build ${status}. Logs:" >&2
        echo "  aws logs tail ${log_group} --since 1h --region ${REGION}" >&2
        exit 1
    fi

    # The stack about to be deployed pins this exact image: fail here rather
    # than let AgentCore pull a missing one.
    resolve_image_digest
    log "build SUCCEEDED: ${IMAGE_URI} (${IMAGE_DIGEST})"
}

if [ "${SKIP_IMAGE}" -eq 0 ]; then
    if [ "${BUILD_ON_CODEBUILD}" -eq 1 ]; then
        if [ "${#CORPORATE_CA_BUILD_ARG[@]}" -gt 0 ]; then
            log "note: CodeBuild ignores image/certs/*.pem (never uploaded); building from the standard base"
        fi
        build_on_codebuild
    else
        log "building image ${IMAGE_URI} (linux/arm64)"
        if docker buildx version >/dev/null 2>&1; then
        docker buildx build --platform linux/arm64 --load \
            ${CORPORATE_CA_BUILD_ARG[@]+"${CORPORATE_CA_BUILD_ARG[@]}"} \
                --build-arg "SCH_IMAGE_VERSION=${VERSION}" \
                -t "${IMAGE_URI}" "${REPO_ROOT}/image/"
        else
            docker build --platform linux/arm64 \
                ${CORPORATE_CA_BUILD_ARG[@]+"${CORPORATE_CA_BUILD_ARG[@]}"} \
                --build-arg "SCH_IMAGE_VERSION=${VERSION}" \
                -t "${IMAGE_URI}" "${REPO_ROOT}/image/"
        fi
        log "pushing to ECR"
        aws ecr get-login-password --region "${REGION}" \
            | docker login --username AWS --password-stdin "${ECR_REGISTRY}"
        docker push "${IMAGE_URI}"
        resolve_image_digest
        log "pushed: ${IMAGE_URI} (${IMAGE_DIGEST})"
    fi
else
    log "skipping image build/push (-s)"
    if [ "${VERSION_SET}" -eq 1 ]; then
        # An explicit tag without a build: the image must already exist, and
        # the runtime gets its current digest.
        resolve_image_digest
        log "pinning existing image ${IMAGE_URI} (${IMAGE_DIGEST})"
    fi
fi

# --- 3. Runtime stack -------------------------------------------------------------
# A first-time deployment has no checkpoint bucket until the base stack exists.
# First try the existing bucket so later registry deployments do not temporarily
# remove the control plane; only create the base stack after that upload fails.
CHECKPOINT_BUCKET="${PROJECT_NAME}-${ENVIRONMENT}-checkpoints-${ACCOUNT_ID}"

# CloudFormation refuses inline template bodies larger than 51,200 bytes, and
# agent_runtime.yaml crossed that line with the task watchdog resources
# (add-task-liveness-safety) — it was already at 50,793 bytes before. Deploy
# the template via S3 instead (limit: 1 MB): reuse the checkpoint bucket when
# it exists (it already hosts the Lambda packages); on a first-time deployment
# that bucket cannot pre-exist (the stack itself owns it — creating it out of
# band would make the stack's bucket resource fail), so fall back to a tiny
# dedicated bootstrap bucket created out of band and reused forever after.
TEMPLATE_BUCKET=""
ensure_template_bucket() {
    if [ -n "${TEMPLATE_BUCKET}" ]; then
        return
    fi
    if aws s3api head-bucket --bucket "${CHECKPOINT_BUCKET}" --region "${REGION}" >/dev/null 2>&1; then
        TEMPLATE_BUCKET="${CHECKPOINT_BUCKET}"
        return
    fi
    TEMPLATE_BUCKET="${PROJECT_NAME}-${ENVIRONMENT}-cfn-bootstrap-${ACCOUNT_ID}"
    if ! aws s3api head-bucket --bucket "${TEMPLATE_BUCKET}" --region "${REGION}" >/dev/null 2>&1; then
        log "creating bootstrap bucket ${TEMPLATE_BUCKET} (agent_runtime.yaml exceeds the 51,200-byte inline limit)"
        aws s3 mb "s3://${TEMPLATE_BUCKET}" --region "${REGION}"
    fi
}

deploy_base_stack() {
    log "creating the base stack before uploading Lambda packages"
    ensure_template_bucket
    aws cloudformation deploy \
        --s3-bucket "${TEMPLATE_BUCKET}" \
        --s3-prefix cfn-templates \
        --template-file "${SCRIPT_DIR}/agent_runtime.yaml" \
        --stack-name "${PROJECT_NAME}-${ENVIRONMENT}-runtime" \
        --parameter-overrides \
            "ProjectName=${PROJECT_NAME}" \
            "Environment=${ENVIRONMENT}" \
            "ApplicationVersion=${VERSION}" \
            "ImageDigest=${IMAGE_DIGEST}" \
            "EnableWorkspaceRegistry=false" \
            "EnableTelegramInteraction=false" \
            "EnableTaskWatchdog=false" \
            "EnableSessionImageRebuild=${ENABLE_SESSION_IMAGE_REBUILD}" \
            "TelegramBotToken=${TELEGRAM_BOT_TOKEN}" \
            "TelegramChatId=${TELEGRAM_CHAT_ID}" \
        --capabilities CAPABILITY_NAMED_IAM \
        --region "${REGION}" \
        --no-fail-on-empty-changeset
}

if [ "${ENABLE_WORKSPACE_REGISTRY}" = "true" ]; then
    log "uploading workspace registry Lambda package"
    REGISTRY_PACKAGE="$(mktemp -d)/workspace-registry-handler.zip"
    (cd "${SCRIPT_DIR}" && zip -q "${REGISTRY_PACKAGE}" workspace_registry_handler.py)
    REGISTRY_KEY="registry/workspace-registry-handler.zip"
    if ! aws s3 cp "${REGISTRY_PACKAGE}" "s3://${CHECKPOINT_BUCKET}/${REGISTRY_KEY}" --region "${REGION}"; then
        deploy_base_stack
        aws s3 cp "${REGISTRY_PACKAGE}" "s3://${CHECKPOINT_BUCKET}/${REGISTRY_KEY}" --region "${REGION}"
    fi
    REGISTRY_VERSION="$(aws s3api head-object --bucket "${CHECKPOINT_BUCKET}" --key "${REGISTRY_KEY}" --region "${REGION}" --query VersionId --output text)"
fi

if [ "${ENABLE_TELEGRAM_INTERACTION}" = "true" ]; then
    log "uploading Telegram webhook Lambda package"
    TELEGRAM_PACKAGE="$(mktemp -d)/telegram-webhook-handler.zip"
    (cd "${SCRIPT_DIR}" && zip -q "${TELEGRAM_PACKAGE}" telegram_webhook_handler.py)
    TELEGRAM_KEY="telegram/telegram-webhook-handler.zip"
    if ! aws s3 cp "${TELEGRAM_PACKAGE}" "s3://${CHECKPOINT_BUCKET}/${TELEGRAM_KEY}" --region "${REGION}"; then
        deploy_base_stack
        aws s3 cp "${TELEGRAM_PACKAGE}" "s3://${CHECKPOINT_BUCKET}/${TELEGRAM_KEY}" --region "${REGION}"
    fi
    TELEGRAM_PACKAGE_VERSION="$(aws s3api head-object --bucket "${CHECKPOINT_BUCKET}" --key "${TELEGRAM_KEY}" --region "${REGION}" --query VersionId --output text)"
fi

if [ "${ENABLE_TASK_WATCHDOG}" = "true" ]; then
    # On by default, so the dependency is worth a clear message instead of an
    # obscure failure in a subshell (the -c path checks the same way).
    command -v zip >/dev/null 2>&1 || {
        echo "deploy: 'zip' is required to package the task watchdog Lambda" >&2
        echo "  install it, or deploy without the watchdog: ENABLE_TASK_WATCHDOG=false" >&2
        exit 1
    }
    log "uploading task watchdog Lambda package"
    WATCHDOG_PACKAGE="$(mktemp -d)/task-watchdog-handler.zip"
    (cd "${SCRIPT_DIR}" && zip -q "${WATCHDOG_PACKAGE}" task_watchdog_handler.py)
    WATCHDOG_KEY="watchdog/task-watchdog-handler.zip"
    if ! aws s3 cp "${WATCHDOG_PACKAGE}" "s3://${CHECKPOINT_BUCKET}/${WATCHDOG_KEY}" --region "${REGION}"; then
        deploy_base_stack
        aws s3 cp "${WATCHDOG_PACKAGE}" "s3://${CHECKPOINT_BUCKET}/${WATCHDOG_KEY}" --region "${REGION}"
    fi
    WATCHDOG_PACKAGE_VERSION="$(aws s3api head-object --bucket "${CHECKPOINT_BUCKET}" --key "${WATCHDOG_KEY}" --region "${REGION}" --query VersionId --output text)"
fi

log "deploying AgentCore Runtime stack (${PROJECT_NAME}-${ENVIRONMENT}-runtime)"
ensure_template_bucket
aws cloudformation deploy \
    --s3-bucket "${TEMPLATE_BUCKET}" \
    --s3-prefix cfn-templates \
    --template-file "${SCRIPT_DIR}/agent_runtime.yaml" \
    --stack-name "${PROJECT_NAME}-${ENVIRONMENT}-runtime" \
    --parameter-overrides \
        "ProjectName=${PROJECT_NAME}" \
        "Environment=${ENVIRONMENT}" \
        "ApplicationVersion=${VERSION}" \
        "ImageDigest=${IMAGE_DIGEST}" \
        "WorkspaceRegistryPackageKey=${REGISTRY_KEY:-registry/workspace-registry-handler.zip}" \
        "WorkspaceRegistryPackageVersion=${REGISTRY_VERSION:-}" \
        "EnableWorkspaceRegistry=${ENABLE_WORKSPACE_REGISTRY}" \
        "EnableSessionImageRebuild=${ENABLE_SESSION_IMAGE_REBUILD}" \
        "TelegramBotToken=${TELEGRAM_BOT_TOKEN}" \
        "TelegramChatId=${TELEGRAM_CHAT_ID}" \
        "EnableTelegramInteraction=${ENABLE_TELEGRAM_INTERACTION}" \
        "TelegramWebhookSecret=${TELEGRAM_WEBHOOK_SECRET}" \
        "TelegramWebhookPackageKey=${TELEGRAM_KEY:-telegram/telegram-webhook-handler.zip}" \
        "TelegramWebhookPackageVersion=${TELEGRAM_PACKAGE_VERSION:-}" \
        "EnableTaskWatchdog=${ENABLE_TASK_WATCHDOG}" \
        "TaskWatchdogPackageKey=${WATCHDOG_KEY:-watchdog/task-watchdog-handler.zip}" \
        "TaskWatchdogPackageVersion=${WATCHDOG_PACKAGE_VERSION:-}" \
        "TaskWatchdogStaleSeconds=${TASK_WATCHDOG_STALE_SECONDS}" \
        "TaskWatchdogNotifyAfterSeconds=${TASK_WATCHDOG_NOTIFY_AFTER_SECONDS}" \
        "PiDefaultModel=${SCH_PI_DEFAULT_MODEL}" \
        "RuntimeCapabilities=${TUNING_CAPS}" \
        "RuntimeBedrockAccess=${RUNTIME_BEDROCK_ACCESS}" \
        "RuntimeBedrockModelAllowlist=${TUNING_ALLOWLIST}" \
        "RuntimeAwsApiRead=${RUNTIME_AWS_API_READ}" \
        "RuntimeDataBucketArn=${RUNTIME_DATA_BUCKET_ARN}" \
        "RuntimeExtraPolicyJson=${RUNTIME_EXTRA_POLICY_JSON}" \
        "RuntimeCapTranscribeEnabled=${TUNING_CAP_TRANSCRIBE}" \
        "RuntimeCapTextractEnabled=${TUNING_CAP_TEXTRACT}" \
        "RuntimeCapRekognitionEnabled=${TUNING_CAP_REKOGNITION}" \
        "RuntimeCapPollyEnabled=${TUNING_CAP_POLLY}" \
        "RuntimeCapComprehendEnabled=${TUNING_CAP_COMPREHEND}" \
        "RuntimeBedrockAllowlistHasOpenAIFamily=${TUNING_HAS_OPENAI}" \
    --capabilities CAPABILITY_NAMED_IAM \
    --region "${REGION}" \
    --no-fail-on-empty-changeset

# --- 4. Telegram webhook registration (add-telegram-interaction, design D1) ------
# setWebhook is idempotent: re-registering the same URL with a fresh secret is
# exactly what a redeploy needs (the Lambda got the same fresh secret above).
# With the feature disabled but a bot configured, deleteWebhook makes the
# rollback complete: Telegram stops pushing updates at a dead endpoint and the
# notification-only deployment (change 1) keeps working unchanged.
telegram_api() { # <method> <json payload>
    curl -fsS -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/$1" \
        -H 'Content-Type: application/json' -d "$2"
}
if [ -n "${TELEGRAM_BOT_TOKEN}" ]; then
    if [ "${ENABLE_TELEGRAM_INTERACTION}" = "true" ]; then
        WEBHOOK_URL="$(aws cloudformation describe-stacks \
            --stack-name "${PROJECT_NAME}-${ENVIRONMENT}-runtime" \
            --region "${REGION}" \
            --query "Stacks[0].Outputs[?OutputKey=='TelegramWebhookUrl'].OutputValue" --output text)"
        if [ -n "${WEBHOOK_URL}" ] && [ "${WEBHOOK_URL}" != "None" ]; then
            log "registering Telegram webhook (setWebhook)"
            telegram_api setWebhook "$(printf '{"url":"%s","secret_token":"%s","allowed_updates":["message","callback_query"]}' \
                "${WEBHOOK_URL}" "${TELEGRAM_WEBHOOK_SECRET}")" >/dev/null \
                && log "webhook registered: ${WEBHOOK_URL}" \
                || echo "deploy: WARNING — setWebhook failed; register manually (see docs/telegram-setup.md)" >&2
        else
            echo "deploy: WARNING — TelegramWebhookUrl output missing; webhook not registered" >&2
        fi
    else
        log "Telegram interaction disabled: removing any registered webhook (deleteWebhook)"
        telegram_api deleteWebhook '{}' >/dev/null 2>&1 || true
    fi
fi

RUNTIME_ARN=$(aws cloudformation describe-stacks \
    --stack-name "${PROJECT_NAME}-${ENVIRONMENT}-runtime" \
    --region "${REGION}" \
    --query "Stacks[0].Outputs[?OutputKey=='RuntimeArn'].OutputValue" --output text)

# What the runtime REALLY runs, from AgentCore itself rather than from the
# stack's view of it: the version proves whether this deploy reached the
# runtime (TASK-30), the URI names the digest it was created from.
RUNTIME_VERSION="" RUNTIME_IMAGE=""
read -r RUNTIME_VERSION RUNTIME_IMAGE <<EOF
$(aws bedrock-agentcore-control get-agent-runtime \
    --agent-runtime-id "${RUNTIME_ARN##*/}" --region "${REGION}" \
    --query '[agentRuntimeVersion, agentRuntimeArtifact.containerConfiguration.containerUri]' \
    --output text 2>/dev/null || true)
EOF

log "done."
echo
echo "Runtime ARN: ${RUNTIME_ARN}"
if [ -n "${RUNTIME_VERSION}" ]; then
    echo "Runtime version: ${RUNTIME_VERSION}"
    echo "Runtime image:   ${RUNTIME_IMAGE} (tag ${VERSION})"
fi
echo "Smoke test:"
echo "  aws bedrock-agentcore invoke-agent-runtime \\"
echo "    --agent-runtime-arn '${RUNTIME_ARN}' \\"
echo "    --runtime-session-id \"smoke-test-\$(uuidgen | tr 'A-Z' 'a-z')\" \\"
echo "    --payload '{\"action\": \"info\"}' --region ${REGION} /dev/stdout"
