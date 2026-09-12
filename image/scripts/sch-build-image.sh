#!/usr/bin/env bash
# sch-build-image — rebuild the runtime image from inside a session.
#
# A microVM cannot build container images (non-root uid 1000, no Docker daemon,
# no privileges to run one). This command therefore does not build anything: it
# packages the CURRENT contents of the workspace's `image/` directory, uploads
# them to S3 and asks the dedicated CodeBuild project to build and push. See
# openspec/changes/add-codebuild-image-rebuild/design.md (D7).
#
# Nothing here decides the image tag: the buildspec lives in the CloudFormation
# template and derives a `sessionbuild-*` tag server-side (design D3/D4).
#
# The produced image is NOT deployed. Promoting it requires updating the stack
# parameter ApplicationVersion, which resets the session storage of EVERY
# workspace — a deliberate human action, never done by this command.
#
# Usage:
#   sch-build-image [--follow] [--timeout <seconds>]
#
#   --follow           stream the build log live (aws logs tail --follow)
#   --timeout <sec>    give up waiting after N seconds (default 2700)
#
# Exit codes: 0 build succeeded · 1 usage/precondition error · 2 build failed
#             or stopped · 3 timed out waiting for the build

set -euo pipefail

PROG="$(basename "$0")"
FOLLOW=0
WAIT_TIMEOUT=2700
POLL_INTERVAL=10
TAIL_PID=""

die() { printf '%s: %s\n' "${PROG}" "$*" >&2; exit "${2:-1}"; }
log() { printf '%s\n' "$*" >&2; }

usage() {
    cat <<'USAGE'
sch-build-image [--follow] [--timeout <seconds>]

Rebuilds this project's runtime image from the workspace's image/ directory by
delegating to the dedicated CodeBuild project (no build engine runs in the
microVM). The tag is assigned by the build project (sessionbuild-*), never by
the caller, and the produced image is NOT deployed.

  --follow           stream the build log live
  --timeout <sec>    give up waiting after N seconds (default 2700)
  -h, --help         show this help

Exit codes: 0 succeeded · 1 usage/precondition error · 2 build failed/stopped
            · 3 timed out waiting
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --follow) FOLLOW=1; shift ;;
        --timeout) [ $# -ge 2 ] || die "--timeout requires a value"; WAIT_TIMEOUT="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1 (try --help)" ;;
    esac
done

case "${WAIT_TIMEOUT}" in
    ''|*[!0-9]*) die "--timeout must be an integer number of seconds" ;;
esac

# --- Preconditions ----------------------------------------------------------
# The capability is opt-in at the CloudFormation level (design D6): when it is
# disabled the runtime injects no project name, and we must say so plainly
# instead of emitting a confusing AWS error. No AWS call happens before this.
PROJECT="${SCH_IMAGE_REBUILD_PROJECT:-}"
[ -n "${PROJECT}" ] || die "image rebuild capability is not enabled on this runtime
  (SCH_IMAGE_REBUILD_PROJECT is not set — redeploy the stack with
   EnableSessionImageRebuild=true to enable it)"

BUCKET="${SCH_CHECKPOINT_BUCKET:-}"
[ -n "${BUCKET}" ] || die "SCH_CHECKPOINT_BUCKET is not set: cannot upload the build source"

command -v aws >/dev/null 2>&1 || die "the AWS CLI is not available in this image"
command -v zip >/dev/null 2>&1 || die "'zip' is not available in this image"

WORKSPACE_ROOT="${SCH_WORKSPACE_ROOT:-/mnt/workspace}"
REPO_DIR="${WORKSPACE_ROOT}/repo"
IMAGE_DIR="${REPO_DIR}/image"
[ -f "${IMAGE_DIR}/Dockerfile" ] || die "no Dockerfile at ${IMAGE_DIR}/Dockerfile
  (this command only rebuilds this project's runtime image, from the
   'image/' build context of the workspace repo)"

# --- Source key scope -------------------------------------------------------
# One key per workspace so two concurrent sessions cannot overwrite each
# other's source (design D2). The workspace name is already persisted by the
# shim in the mount marker; hostname is the fallback.
scope() {
    local marker="${WORKSPACE_ROOT}/state/.sch-initialized" name=""
    if [ -r "${marker}" ]; then
        name="$(python3.11 -c 'import json,sys
try:
    print((json.load(open(sys.argv[1])).get("workspace") or "").strip())
except Exception:
    print("")' "${marker}" 2>/dev/null || true)"
    fi
    # No `hostname` binary is guaranteed on AL2023 minimal: use the shell's
    # HOSTNAME, then /etc/hostname, then a literal.
    [ -n "${name}" ] || name="${HOSTNAME:-}"
    [ -n "${name}" ] || name="$(cat /etc/hostname 2>/dev/null || true)"
    [ -n "${name}" ] || name="unknown"
    printf '%s' "${name}" | tr -c 'A-Za-z0-9_.-' '-' | cut -c1-64
}

SCOPE="$(scope)"
SOURCE_KEY="builds/${SCOPE}/source.zip"
TMP_DIR="$(mktemp -d -t sch-build-image.XXXXXX)"
ZIP_PATH="${TMP_DIR}/source.zip"

cleanup() {
    rm -rf "${TMP_DIR}" 2>/dev/null || true
    [ -n "${TAIL_PID}" ] && kill "${TAIL_PID}" 2>/dev/null || true
}
trap cleanup EXIT

# --- 1. Package -------------------------------------------------------------
# The CONTENTS of image/ go at the zip root, so the build context is exactly
# image/ and the buildspec can rely on ./Dockerfile. The archive is written to
# local disk (/tmp), never onto the session-storage mount.
log "==> packaging ${IMAGE_DIR} (source root = image/ contents)"
( cd "${IMAGE_DIR}" && zip -qr "${ZIP_PATH}" . -x '*.pyc' -x '__pycache__/*' -x '*/__pycache__/*' -x 'certs/*.pem' )
ZIP_SIZE="$(wc -c < "${ZIP_PATH}" | tr -d ' ')"
log "    ${ZIP_SIZE} bytes"

# --- 2. Upload --------------------------------------------------------------
log "==> uploading to s3://${BUCKET}/${SOURCE_KEY}"
aws s3 cp "${ZIP_PATH}" "s3://${BUCKET}/${SOURCE_KEY}" --only-show-errors

# --- 3. Start the build -----------------------------------------------------
log "==> starting CodeBuild project ${PROJECT}"
BUILD_ID="$(aws codebuild start-build \
    --project-name "${PROJECT}" \
    --source-location-override "${BUCKET}/${SOURCE_KEY}" \
    --query 'build.id' --output text)"
[ -n "${BUILD_ID}" ] && [ "${BUILD_ID}" != "None" ] || die "could not start the build" 2
log "    build id: ${BUILD_ID}"

# The log group is /aws/codebuild/<project name> by construction of the stack.
LOG_GROUP="/aws/codebuild/${PROJECT}"
if [ "${FOLLOW}" = "1" ]; then
    aws logs tail "${LOG_GROUP}" --follow --since 1m >&2 &
    TAIL_PID=$!
fi

# --- 4. Wait ----------------------------------------------------------------
# Phase polling instead of log streaming by default: it needs no log
# permissions beyond what the role already has and no long-lived connection.
ELAPSED=0
LAST_PHASE=""
STATUS=""
while :; do
    read -r STATUS PHASE <<EOF
$(aws codebuild batch-get-builds --ids "${BUILD_ID}" \
    --query 'builds[0].[buildStatus,currentPhase]' --output text)
EOF
    if [ "${PHASE:-}" != "${LAST_PHASE}" ]; then
        log "    [$(date -u +%H:%M:%S)] ${PHASE:-?}"
        LAST_PHASE="${PHASE:-}"
    fi
    [ "${STATUS}" = "IN_PROGRESS" ] || break
    if [ "${ELAPSED}" -ge "${WAIT_TIMEOUT}" ]; then
        die "still IN_PROGRESS after ${WAIT_TIMEOUT}s — the build keeps running;
  inspect it with: aws logs tail ${LOG_GROUP} --follow" 3
    fi
    sleep "${POLL_INTERVAL}"
    ELAPSED=$((ELAPSED + POLL_INTERVAL))
done

[ -n "${TAIL_PID}" ] && { kill "${TAIL_PID}" 2>/dev/null || true; TAIL_PID=""; }

if [ "${STATUS}" != "SUCCEEDED" ]; then
    FAILED_PHASE="$(aws codebuild batch-get-builds --ids "${BUILD_ID}" \
        --query "builds[0].phases[?phaseStatus!='SUCCEEDED'].phaseType | [0]" \
        --output text 2>/dev/null || true)"
    log ""
    log "BUILD ${STATUS} (failing phase: ${FAILED_PHASE:-unknown})"
    log "logs: aws logs tail ${LOG_GROUP} --since 1h"
    exit 2
fi

# --- 5. Report --------------------------------------------------------------
IMAGE_URI="$(aws codebuild batch-get-builds --ids "${BUILD_ID}" \
    --query "builds[0].exportedEnvironmentVariables[?name=='SCH_IMAGE_URI'].value | [0]" \
    --output text 2>/dev/null || true)"
[ "${IMAGE_URI}" = "None" ] && IMAGE_URI=""

log ""
log "BUILD SUCCEEDED"
if [ -n "${IMAGE_URI}" ]; then
    printf '%s\n' "${IMAGE_URI}"
else
    log "warning: the build did not export SCH_IMAGE_URI; find the tag in the log:"
    log "  aws logs tail ${LOG_GROUP} --since 1h"
fi
log ""
log "NOTE: this image is NOT deployed. Nothing about the running runtime changed."
log "      To promote it, redeploy the stack with ApplicationVersion set to the"
log "      tag above — which RESETS THE SESSION STORAGE OF EVERY WORKSPACE."
