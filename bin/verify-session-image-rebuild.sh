#!/bin/bash
# verify-session-image-rebuild.sh — live checks for the CodeBuild image rebuild.
#
# The workspace must already contain this repository's image/ build context.
# Run this only against a disposable workspace: the failure check appends an
# invalid instruction to its remote Dockerfile and removes it afterwards.
set -euo pipefail

WS="${1:-}"
[ -n "${WS}" ] || { echo "usage: $0 <workspace>" >&2; exit 2; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCH="${SCRIPT_DIR}/sch"
REGION="${SCH_REGION:-eu-west-1}"
PROJECT="${SCH_PROJECT:-sch}"
ENVIRONMENT="${SCH_ENV:-dev}"
STACK="${PROJECT}-${ENVIRONMENT}-runtime"
REPOSITORY="${PROJECT}-${ENVIRONMENT}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
ARN="$(aws cloudformation describe-stacks --stack-name "${STACK}" --region "${REGION}" --query "Stacks[0].Outputs[?OutputKey=='RuntimeArn'].OutputValue" --output text)"
SID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("runtimeSessionId", ""))' "${XDG_CONFIG_HOME:-$HOME/.config}/sch/workspaces/${WS}")"

PASS=0
FAIL=0
ok() { echo "PASS: $*"; PASS=$((PASS + 1)); }
bad() { echo "FAIL: $*" >&2; FAIL=$((FAIL + 1)); }

remote() {
    agentcore exec --runtime "${ARN}" --session-id "${SID}" --region "${REGION}" \
        --timeout 1800 --json -- sh -c "'$1'"
}

echo "workspace=${WS} session=${SID}"
echo

echo "== 1. environment and successful rebuild =="
env_result="$(remote 'test -f /mnt/workspace/repo/image/Dockerfile && test -n "${SCH_IMAGE_REBUILD_PROJECT:-}" && aws --version && command -v sch-build-image')"
if printf '%s' "${env_result}" | python3 -c 'import json,sys; raise SystemExit(0 if json.load(sys.stdin).get("success") else 1)'; then
    ok "runtime injects the rebuild project and exposes aws/sch-build-image"
else
    bad "runtime environment is missing the rebuild command or project variable"
fi

build_result="$(remote 'cd /mnt/workspace/repo && sch-build-image --timeout 1800')"
build_stdout="$(printf '%s' "${build_result}" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("stdout", ""), end="")')"
image_uri="$(printf '%s\n' "${build_stdout}" | awk '/sessionbuild-/{print; exit}')"
if printf '%s' "${build_result}" | python3 -c 'import json,sys; raise SystemExit(0 if json.load(sys.stdin).get("success") else 1)' && [ -n "${image_uri}" ]; then
    ok "successful build published ${image_uri}"
else
    bad "successful rebuild did not return a sessionbuild image URI"
fi

echo
echo "== 2. uncommitted source modification is built =="
sentinel="verify-$(uuidgen | tr '[:upper:]' '[:lower:]')"
sentinel_command="cd /mnt/workspace/repo && cp image/Dockerfile image/Dockerfile.verify-backup && echo LABEL sch.verify-sentinel=${sentinel} >> image/Dockerfile; sch-build-image --timeout 1800; rc=\$?; mv image/Dockerfile.verify-backup image/Dockerfile; exit \$rc"
sentinel_result="$(remote "${sentinel_command}")"
sentinel_uri="$(printf '%s' "${sentinel_result}" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("stdout", ""), end="")' | awk '/sessionbuild-/{print; exit}')"
sentinel_tag="${sentinel_uri##*:}"
if printf '%s' "${sentinel_result}" | python3 -c 'import json,sys; raise SystemExit(0 if json.load(sys.stdin).get("success") else 1)' && [ -n "${sentinel_tag}" ]; then
    manifest="$(aws ecr batch-get-image --repository-name "${REPOSITORY}" --image-ids "imageTag=${sentinel_tag}" --accepted-media-types application/vnd.docker.distribution.manifest.v2+json --region "${REGION}" --query 'images[0].imageManifest' --output text)"
    config_digest="$(printf '%s' "${manifest}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["config"]["digest"])')"
    config_url="$(aws ecr get-download-url-for-layer --repository-name "${REPOSITORY}" --layer-digest "${config_digest}" --region "${REGION}" --query downloadUrl --output text)"
    if curl --fail --silent --show-error "${config_url}" | python3 -c 'import json,sys; raise SystemExit(0 if json.load(sys.stdin).get("config", {}).get("Labels", {}).get("sch.verify-sentinel") == sys.argv[1] else 1)' "${sentinel}"; then
        ok "uncommitted Dockerfile label is present in ${sentinel_uri}"
    else
        bad "uncommitted Dockerfile label was not present in the built image"
    fi
else
    bad "sentinel rebuild did not return a sessionbuild image URI"
fi

echo
echo "== 3. direct ECR and unrelated CodeBuild access denied =="
ecr_result="$(remote "aws ecr initiate-layer-upload --repository-name ${REPOSITORY} 2>&1 || true")"
if printf '%s' "${ecr_result}" | grep -q 'AccessDenied'; then
    ok "direct ECR upload is denied to the runtime role"
else
    bad "direct ECR upload was not denied"
fi

codebuild_result="$(remote "aws codebuild start-build --project-name definitely-not-the-rebuild-project 2>&1 || true; aws codebuild create-project --name sch-image-rebuild-denied --source type=NO_SOURCE --artifacts type=NO_ARTIFACTS --environment type=LINUX_CONTAINER,image=aws/codebuild/standard:7.0,computeType=BUILD_GENERAL1_SMALL --service-role arn:aws:iam::${ACCOUNT_ID}:role/${PROJECT}-${ENVIRONMENT}-image-rebuild-role 2>&1 || true")"
if printf '%s' "${codebuild_result}" | grep -q 'codebuild:StartBuild' && printf '%s' "${codebuild_result}" | grep -q 'codebuild:CreateProject'; then
    ok "unrelated CodeBuild start and project creation are denied to the runtime role"
else
    bad "unrelated CodeBuild operations were not denied"
fi

s3_result="$(remote 'aws s3api put-object --bucket "${SCH_CHECKPOINT_BUCKET}" --key outside-builds-and-checkpoints.txt --body /mnt/workspace/repo/image/Dockerfile 2>&1 || true')"
if printf '%s' "${s3_result}" | grep -q 'AccessDenied'; then
    ok "S3 writes outside builds/ and checkpoints/ are denied"
else
    bad "out-of-scope S3 write was not denied"
fi

echo
echo "== 4. expected failing build =="
failure_result="$(remote 'cd /mnt/workspace/repo && cp image/Dockerfile image/Dockerfile.verify-backup && printf "\nRUN false # verify-session-image-rebuild\n" >> image/Dockerfile; sch-build-image --timeout 1800; rc=$?; mv image/Dockerfile.verify-backup image/Dockerfile; exit $rc' || true)"
if printf '%s' "${failure_result}" | python3 -c 'import json,sys; d=json.load(sys.stdin); raise SystemExit(0 if d.get("exitCode") == 2 and "BUILD FAILED" in d.get("stderr", "") else 1)'; then
    ok "failed build returns exit code 2 and identifies the failure"
else
    bad "failed rebuild did not report the expected error"
fi

echo
echo "== result: ${PASS} passed, ${FAIL} failed =="
[ "${FAIL}" -eq 0 ]
