#!/bin/bash
# verify-runtime-iam-tuning.sh — live verification of runtime capability tuning
# (docs/specs/security/runtime-capability-tuning.md, TASK-10).
#
# Drives infra/deploy.sh against the LIVE stack through four phases and asserts
# the effective execution-role permissions with iam:SimulatePrincipalPolicy —
# the real role's policies, not the template:
#   0. preflight + snapshot (role policies, RuntimeVersion, checkpoint count)
#   1. TUNED: RUNTIME_CAPABILITIES=transcribe + claude-only Bedrock allow-list
#   2. KILL SWITCH: RUNTIME_BEDROCK_ACCESS=false (tuning otherwise unchanged)
#   3. RE-ENABLE: phase-1 env again — toggling back restores the tuned posture
#   4. ROLLBACK: inert defaults — role byte-identical to the phase-0 snapshot
#
# Session-safety invariants checked along the way:
#   - every deploy reuses the deployed ApplicationVersion and ImageDigest
#     (deploy.sh -s without -v) and re-passes the deployed feature flags, so
#     NO new AgentCore Runtime version is created at any point and session
#     storage is never reset;
#   - the checkpoint bucket's top-level workspace prefix count never changes.
#
# If any phase fails, the script rolls the stack back to inert defaults before
# exiting non-zero (never leave the live stack tuned because a check failed).
#
# Requirements:
#   - aws cli, python3, zip (deploy.sh packages the watchdog Lambda);
#   - credentials allowed to run iam:SimulatePrincipalPolicy on the execution
#     role (ReadOnlyAccess grants it) and to deploy the runtime stack;
#   - when the deployed stack has Telegram configured, TELEGRAM_BOT_TOKEN and
#     TELEGRAM_CHAT_ID must be exported exactly as for infra/deploy.sh:
#     re-deploying without them would change the runtime environment and bump
#     the runtime version (session reset). The script refuses to start without
#     them.
#
# Usage: bin/verify-runtime-iam-tuning.sh [-r region] [-p project] [-e env]
set -euo pipefail

REGION="eu-west-1"
PROJECT_NAME="sch"
ENVIRONMENT="dev"
while getopts "r:p:e:" opt; do
    case "${opt}" in
        r) REGION="${OPTARG}" ;;
        p) PROJECT_NAME="${OPTARG}" ;;
        e) ENVIRONMENT="${OPTARG}" ;;
        *) echo "usage: $0 [-r region] [-p project] [-e env]" >&2; exit 2 ;;
    esac
done

export AWS_PAGER=""

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STACK="${PROJECT_NAME}-${ENVIRONMENT}-runtime"
ROLE_NAME="${PROJECT_NAME}-${ENVIRONMENT}-BedrockAgentCore-role"
TMP_DIR="$(mktemp -d -t sch-verify-iam-tuning.XXXXXX)"

# Tuning exercised by the verification. The allow-list entry is the stack's
# main inference profile: claude-only, so the Mantle plane must be gone.
ALLOWLIST_MODEL="eu.anthropic.claude-sonnet-4-6"
ENABLED_CAPABILITY="transcribe"
# Disabled-capability probe: must be an action OUTSIDE the ReadOnlyAccess
# overlap, because the attached managed policy (v188 at verification time)
# already grants read-classed catalog actions — polly:SynthesizeSpeech and the
# whole comprehend:Detect* family included. The preflight below re-checks the
# chosen probe against the ATTACHED version, so an AWS policy update that
# widens the overlap fails fast with guidance instead of a false pass.
DISABLED_CAPABILITY_ACTION="textract:StartDocumentTextDetection"

log() { echo "==> $*"; }
die() { echo "verify: $*" >&2; exit 1; }
trim() { # --output text prints the literal "None" for empty query results
    [ "$1" = "None" ] && echo "" || echo "$1"
}

# Best-effort safety net: if a verification phase fails after the stack has
# been moved away from its inert posture, put it back before exiting non-zero.
on_exit() {
    local code=$?
    if [ "${code}" -ne 0 ] && [ "${FAILED_PHASE}" -ge 1 ] && [ "${ROLLED_BACK}" -ne 1 ]; then
        ROLLED_BACK=1
        echo "verify: phase ${FAILED_PHASE} failed; rolling the stack back to inert defaults" >&2
        if ! run_deploy; then
            echo "verify: AUTOMATIC ROLLBACK FAILED — inspect ${TMP_DIR} and roll back manually:" >&2
            echo "  (unset RUNTIME_*) infra/deploy.sh -s -r ${REGION} -p ${PROJECT_NAME} -e ${ENVIRONMENT}" >&2
        fi
    fi
    exit "${code}"
}
trap on_exit EXIT

stack_param() {
    aws cloudformation describe-stacks --stack-name "${STACK}" --region "${REGION}" \
        --query "Stacks[0].Parameters[?ParameterKey=='$1'].ParameterValue" --output text 2>/dev/null || true
}
stack_output() {
    aws cloudformation describe-stacks --stack-name "${STACK}" --region "${REGION}" \
        --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text 2>/dev/null || true
}

# --- simulation helpers (defined before use) --------------------------------------
simulate() { # <action> <resource-arn>
    aws iam simulate-principal-policy \
        --policy-source-arn "${ROLE_ARN}" \
        --action-names "$1" --resource-arns "$2" \
        --region "${REGION}" \
        --query 'EvaluationResults[0].EvalDecision' --output text
}
assert_allowed() {
    local decision
    decision="$(simulate "$1" "$2")"
    echo "    allowed?  $1 on $2 -> ${decision}"
    [ "${decision}" = "allowed" ] || die "EXPECTED allowed: $1 on $2 (got ${decision})"
}
assert_denied() {
    local decision
    decision="$(simulate "$1" "$2")"
    echo "    denied?   $1 on $2 -> ${decision}"
    [ "${decision}" != "allowed" ] || die "EXPECTED denied: $1 on $2 (got ${decision})"
}
role_has_inline_policy() { # <policy-name>; returns 0 when present
    aws iam list-role-policies --role-name "${ROLE_NAME}" --region "${REGION}" \
        --query "contains(PolicyNames, '$1')" --output text | grep -q True
}

# --- deploy helper -----------------------------------------------------------------
# Extra env for a phase comes as arguments: RUNTIME_X=... RUNTIME_Y=...
# deploy.sh output goes to a per-phase log (shown in full on failure).
# Any phase failure triggers the EXIT trap below, which rolls the stack back
# to inert defaults before exiting non-zero.
PHASE_NO=0
FAILED_PHASE=0
ROLLED_BACK=0
run_deploy() {
    PHASE_NO=$((PHASE_NO + 1))
    FAILED_PHASE="${PHASE_NO}"
    local phase_log="${TMP_DIR}/deploy-phase-${PHASE_NO}.log"
    log "deploy phase ${PHASE_NO}: $* (stack-only, reusing deployed ApplicationVersion)"
    if ! env \
        "ENABLE_WORKSPACE_REGISTRY=${DEPLOYED_REGISTRY:-false}" \
        "ENABLE_SESSION_IMAGE_REBUILD=${DEPLOYED_IMAGE_REBUILD:-false}" \
        "ENABLE_TASK_WATCHDOG=${DEPLOYED_WATCHDOG:-true}" \
        "$@" \
        "${REPO_ROOT}/infra/deploy.sh" -s -r "${REGION}" -p "${PROJECT_NAME}" -e "${ENVIRONMENT}" \
        >"${phase_log}" 2>&1; then
        echo "----- deploy output (tail) -----" >&2
        tail -40 "${phase_log}" >&2 || true
        die "deploy of phase ${PHASE_NO} failed; full log: ${phase_log}"
    fi
    # No-reset property (spec R2 / invariant I2): same ApplicationVersion in the
    # stack, same AgentCore Runtime version, same number of workspace prefixes.
    local version now_applied
    version="$(trim "$(stack_output RuntimeVersion)")"
    [ "${version}" = "${RUNTIME_VERSION_0}" ] \
        || die "runtime version changed: ${RUNTIME_VERSION_0} -> ${version} (session storage would be reset)"
    now_applied="$(trim "$(stack_param ApplicationVersion)")"
    [ "${now_applied}" = "${APPLICATION_VERSION_0}" ] \
        || die "ApplicationVersion changed: ${APPLICATION_VERSION_0} -> ${now_applied}"
}

# --- snapshot helpers ---------------------------------------------------------------
# Role snapshot: every inline policy document plus the attached managed policies
# with their pinned version — canonical JSON, diffed byte-for-byte at the end
# (invariant I1).
snapshot_role() { # $1 = output file
    python3 - "${ROLE_NAME}" "${REGION}" "$1" <<'PY'
import json, subprocess, sys

role, region, out_path = sys.argv[1], sys.argv[2], sys.argv[3]

def aws(*args):
    r = subprocess.run(["aws", *args, "--region", region, "--output", "json"],
                       capture_output=True, text=True, check=True)
    return json.loads(r.stdout) if r.stdout.strip() else None

inline = {}
for name in aws("iam", "list-role-policies", "--role-name", role)["PolicyNames"]:
    inline[name] = aws("iam", "get-role-policy",
                       "--role-name", role, "--policy-name", name)["PolicyDocument"]
attached = {}
for p in aws("iam", "list-attached-role-policies", "--role-name", role)["AttachedPolicies"]:
    arn = p["PolicyArn"]
    default = aws("iam", "get-policy", "--policy-arn", arn)["Policy"]["DefaultVersionId"]
    attached[arn] = {"PolicyName": p["PolicyName"], "VersionId": default}
with open(out_path, "w") as fh:
    json.dump({"inline": inline, "attached": attached}, fh,
              sort_keys=True, separators=(",", ":"))
    fh.write("\n")
PY
}

checkpoint_bucket() {
    trim "$(stack_output CheckpointBucketName)"
}
checkpoint_count() {
    aws s3 ls "s3://$(checkpoint_bucket)/checkpoints/" --region "${REGION}" | wc -l | tr -d ' '
}
assert_no_reset() {
    local count
    count="$(checkpoint_count)"
    [ "${count}" = "${CHECKPOINT_COUNT_0}" ] \
        || die "workspace checkpoint prefix count changed: ${CHECKPOINT_COUNT_0} -> ${count}"
    echo "    intact:   runtime version ${RUNTIME_VERSION_0}, ${count} workspace checkpoint prefixes"
}

# --- preflight ----------------------------------------------------------------------
log "preflight: stack ${STACK} (region ${REGION})"
STACK_STATUS="$(aws cloudformation describe-stacks --stack-name "${STACK}" --region "${REGION}" \
    --query "Stacks[0].StackStatus" --output text 2>/dev/null)" \
    || die "stack ${STACK} does not exist in ${REGION}"
case "${STACK_STATUS}" in
    *COMPLETE) ;;
    *_OK) ;;
    *) die "stack status is ${STACK_STATUS}; wait for it to settle first" ;;
esac

command -v python3 >/dev/null 2>&1 || die "python3 is required"
command -v zip >/dev/null 2>&1 || die "zip is required (deploy.sh packages the watchdog Lambda)"

ROLE_ARN="$(trim "$(stack_output ExecutionRoleArn)")"
[ -n "${ROLE_ARN}" ] || die "stack has no ExecutionRoleArn output"
RUNTIME_VERSION_0="$(trim "$(stack_output RuntimeVersion)")"
[ -n "${RUNTIME_VERSION_0}" ] || die "stack has no RuntimeVersion output"
ACCOUNT_ID="$(printf '%s' "${ROLE_ARN}" | cut -d: -f5)"
APPLICATION_VERSION_0="$(trim "$(stack_param ApplicationVersion)")"

# Re-deploy with the SAME feature flags the stack already has. Telegram
# credentials cannot be read back (NoEcho): they must come from the operator
# environment, exactly as for a manual deploy — hence the hard gate below.
DEPLOYED_REGISTRY="$(trim "$(stack_param EnableWorkspaceRegistry)")"
DEPLOYED_IMAGE_REBUILD="$(trim "$(stack_param EnableSessionImageRebuild)")"
DEPLOYED_WATCHDOG="$(trim "$(stack_param EnableTaskWatchdog)")"
DEPLOYED_CHAT_ID="$(trim "$(stack_param TelegramChatId)")"
if [ -n "${DEPLOYED_CHAT_ID}" ]; then
    [ -n "${TELEGRAM_BOT_TOKEN:-}" ] || die "the deployed stack has Telegram configured; export TELEGRAM_BOT_TOKEN (as for infra/deploy.sh) — re-deploying without it would bump the runtime version and reset sessions"
    [ -n "${TELEGRAM_CHAT_ID:-}" ] || die "the deployed stack has Telegram configured; export TELEGRAM_CHAT_ID (as for infra/deploy.sh) — re-deploying without it would bump the runtime version and reset sessions"
fi

# Fail fast on missing simulation rights BEFORE any deploy changes the stack.
# polly:SynthesizeSpeech is granted by ReadOnlyAccess (see the note above) — a
# stable probe action that needs no catalog assumptions.
log "preflight: iam:SimulatePrincipalPolicy against ${ROLE_ARN}"
PRECHECK="$(simulate polly:SynthesizeSpeech "*")"
[ "${PRECHECK}" = "allowed" ] \
    || die "caller is not allowed iam:SimulatePrincipalPolicy on ${ROLE_ARN} (ReadOnlyAccess grants it — check credentials)"

# The disabled-capability probe must NOT be covered by the ATTACHED ReadOnlyAccess
# version (AWS widens that policy over time): check it here, so a covered probe
# fails fast with guidance instead of producing a false "expected denied" pass.
log "preflight: disabled-capability probe '${DISABLED_CAPABILITY_ACTION}' is outside the attached ReadOnlyAccess overlap"
PROBE_CHECK="$(ROLE_NAME="${ROLE_NAME}" REGION="${REGION}" PROBE="${DISABLED_CAPABILITY_ACTION}" python3 <<'PY'
import json, os, re, subprocess

pattern = None
attached = json.loads(subprocess.run(
    ["aws", "iam", "list-attached-role-policies", "--role-name", os.environ["ROLE_NAME"],
     "--region", os.environ["REGION"], "--output", "json"],
    capture_output=True, text=True, check=True).stdout)["AttachedPolicies"]
ro = [p for p in attached if p["PolicyName"] == "ReadOnlyAccess"]
if ro:
    arn = ro[0]["PolicyArn"]
    version = subprocess.run(
        ["aws", "iam", "get-policy", "--policy-arn", arn, "--query",
         "Policy.DefaultVersionId", "--region", os.environ["REGION"], "--output", "text"],
        capture_output=True, text=True, check=True).stdout.strip()
    doc = json.loads(subprocess.run(
        ["aws", "iam", "get-policy-version", "--policy-arn", arn,
         "--version-id", version, "--region", os.environ["REGION"], "--output", "json"],
        capture_output=True, text=True, check=True).stdout)["PolicyVersion"]["Document"]
    sts = doc.get("Statement", [])
    if isinstance(sts, dict):
        sts = [sts]
    probe = os.environ["PROBE"]
    for st in sts:
        if not isinstance(st, dict) or st.get("Effect") != "Allow":
            continue
        for action in st.get("Action", []):
            regex = "^" + ".*".join(re.escape(part) for part in action.split("*")) + "$"
            if re.fullmatch(regex, probe):
                pattern = f"{action} (ReadOnlyAccess {version})"
if pattern:
    raise SystemExit(f"covered by {pattern}")
PY
)" || true
[ "${PROBE_CHECK}" = "" ] || die "${PROBE_CHECK} — pick a DISABLED_CAPABILITY_ACTION outside the ReadOnlyAccess overlap"

# Bedrock resource ARNs used as simulation targets (concrete ARNs: the grants
# cover specific resource patterns, so Resource '*' would implicitly deny even
# the allowed posture).
PROFILE_ARN="arn:aws:bedrock:${REGION}:${ACCOUNT_ID}:inference-profile/${ALLOWLIST_MODEL}"
UNLISTED_MODEL_TARGET="arn:aws:bedrock:${REGION}:${ACCOUNT_ID}:foundation-model/amazon.nova-lite-v1:0"
MANTLE_ARN="arn:aws:bedrock-mantle:${REGION}:${ACCOUNT_ID}:project/default"
CAP_POLICY_NAME="${PROJECT_NAME}-${ENVIRONMENT}-runtime-cap-${ENABLED_CAPABILITY}"
CORE_POLICY_NAME="${PROJECT_NAME}-${ENVIRONMENT}-agentcore-policy"

# --- phase 0: snapshot --------------------------------------------------------------
CHECKPOINT_COUNT_0="$(checkpoint_count)"
log "phase 0: snapshot role + ${CHECKPOINT_COUNT_0} workspace checkpoint prefixes"
snapshot_role "${TMP_DIR}/role-pre.json"

# --- shared assertion: the tuned posture --------------------------------------------
assert_tuned_posture() {
    echo "  effective role policies:"
    role_has_inline_policy "${CAP_POLICY_NAME}" \
        || die "expected inline policy ${CAP_POLICY_NAME} on the role"
    echo "    present:  inline policy ${CAP_POLICY_NAME}"
    echo "  simulations:"
    assert_allowed "transcribe:StartTranscriptionJob" "*"
    assert_allowed "bedrock:InvokeModel" "${PROFILE_ARN}"
    assert_denied "bedrock:InvokeModel" "${UNLISTED_MODEL_TARGET}"
    assert_denied "bedrock-mantle:CreateInference" "${MANTLE_ARN}"
    assert_denied "${DISABLED_CAPABILITY_ACTION}" "*"
    assert_no_reset
}

# --- phase 1: tuned deploy ----------------------------------------------------------
log "phase 1: TUNED (capabilities=${ENABLED_CAPABILITY}, allowlist=${ALLOWLIST_MODEL})"
run_deploy "RUNTIME_CAPABILITIES=${ENABLED_CAPABILITY}" \
    "RUNTIME_BEDROCK_MODEL_ALLOWLIST=${ALLOWLIST_MODEL}"
assert_tuned_posture

# The allow-list entry must be expanded verbatim into BOTH ARN forms in the
# effective policy (spec R6), not merely simulated into existence.
ROLE="${ROLE_NAME}" REGION="${REGION}" CORE_POLICY="${CORE_POLICY_NAME}" \
ALLOWLIST_MODEL="${ALLOWLIST_MODEL}" python3 <<'PY' || die "effective allow-list resources are wrong (expected the entry in both ARN forms)"
import json, os, subprocess

doc = json.loads(subprocess.run(
    ["aws", "iam", "get-role-policy",
     "--role-name", os.environ["ROLE"],
     "--policy-name", os.environ["CORE_POLICY"],
     "--region", os.environ["REGION"], "--output", "json"],
    capture_output=True, text=True, check=True).stdout)["PolicyDocument"]
model = os.environ["ALLOWLIST_MODEL"]
expected = {
    f"arn:aws:bedrock:*:*:inference-profile/{model}",
    f"arn:aws:bedrock:*::foundation-model/{model}",
}
sts = doc["Statement"]
if isinstance(sts, dict):
    sts = [sts]
resources = set()
for st in sts:
    if st.get("Sid") == "BedrockInferenceProfilesAllowlisted":
        r = st.get("Resource", [])
        resources.update(r if isinstance(r, list) else [r])
missing = expected - resources
extra = {r for r in resources if not r.endswith(f"/{model}")}
if missing or extra:
    raise SystemExit(f"mismatch: missing={sorted(missing)} extra={sorted(extra)}")
print("    present:  allow-list entry expanded to both ARN forms")
PY

# --- phase 2: bedrock kill switch ----------------------------------------------------
log "phase 2: KILL SWITCH (RUNTIME_BEDROCK_ACCESS=false, tuning otherwise unchanged)"
run_deploy "RUNTIME_CAPABILITIES=${ENABLED_CAPABILITY}" \
    "RUNTIME_BEDROCK_MODEL_ALLOWLIST=${ALLOWLIST_MODEL}" \
    "RUNTIME_BEDROCK_ACCESS=false"
echo "  simulations:"
# I5: no Bedrock invocation through the role on EITHER plane, whatever the
# allow-list says — even the allow-listed model is unreachable now.
assert_denied "bedrock:InvokeModel" "${PROFILE_ARN}"
assert_denied "bedrock-mantle:CreateInference" "${MANTLE_ARN}"
assert_allowed "transcribe:StartTranscriptionJob" "*"
assert_no_reset

# --- phase 3: re-enable ---------------------------------------------------------------
log "phase 3: RE-ENABLE (phase-1 env; toggling back restores the tuned posture)"
run_deploy "RUNTIME_CAPABILITIES=${ENABLED_CAPABILITY}" \
    "RUNTIME_BEDROCK_MODEL_ALLOWLIST=${ALLOWLIST_MODEL}"
assert_tuned_posture

# --- phase 4: rollback to inert defaults ----------------------------------------------
log "phase 4: ROLLBACK (all RUNTIME_* switches unset)"
run_deploy
if role_has_inline_policy "${CAP_POLICY_NAME}"; then
    die "rollback left ${CAP_POLICY_NAME} on the role"
fi
snapshot_role "${TMP_DIR}/role-post.json"
if ! cmp -s "${TMP_DIR}/role-pre.json" "${TMP_DIR}/role-post.json"; then
    echo "----- role diff (pre vs post) -----" >&2
    diff "${TMP_DIR}/role-pre.json" "${TMP_DIR}/role-post.json" >&2 || true
    die "role after rollback is NOT byte-identical to the pre-feature snapshot"
fi
echo "  simulations:"
assert_allowed "bedrock:InvokeModel" "${PROFILE_ARN}"
assert_allowed "bedrock-mantle:CreateInference" "${MANTLE_ARN}"
assert_denied "transcribe:StartTranscriptionJob" "*"
assert_no_reset

echo
echo "verified: runtime capability tuning behaves live — tuned posture asserted via"
echo "  iam:SimulatePrincipalPolicy, no runtime version/reset at any phase, and the"
echo "  rolled-back role is byte-identical to the pre-feature snapshot"
