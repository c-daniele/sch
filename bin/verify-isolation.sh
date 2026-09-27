#!/bin/bash
# verify-isolation.sh — live two-principal check of per-principal workspace
# isolation (spec: docs/specs/security/per-principal-isolation.md; TASK-20).
#
# Run it as the operator against a stack deployed with ISOLATED_PRINCIPALS,
# with AWS CLI profiles for two listed principals (A, B), one unlisted
# principal (C) and, optionally, a listed IAM Identity Center user (D).
#
# What it checks (every line is PASS or FAIL; exit 1 on any FAIL):
#   0. preflight: the stack reports IsolationStatus=true, the profiles are
#      distinct identities, A and B each have their own plane
#   1. unlisted caller: the registry answers HTTP 403 naming the entry to add
#   2. own workflow (A and B, same logical workspace name): `sch task` runs to
#      success on the caller's plane, `sch status`, `sch list`,
#      `sch list --remote-check` and the `sch dashboard` data path work, and
#      the owner reads its own task status through its access role
#   3. join denied even with a known session ID: B (and C, and D) invoke and
#      stop A's session on A's runtime with A's runtime ARN and session ID;
#      B invokes the shared runtime; A's same call is the positive control
#   4. cross-owner checkpoint reads denied from the CLI: B reads A's task
#      status with its own credentials, through its own access role, by
#      assuming A's access role, and lists A's owner tree; C reads it too
#   5. cross-owner reads denied from inside the agent: in A's microVM (the
#      credentials every tool of A's agent runs with) read and list B's owner
#      tree, scan the registry table, read B's plane parameter and B's runtime
#      configuration, assume B's access role; A's own task status is the
#      positive control
#
# Denials count only when the error is an authorization error
# (AccessDenied, not authorized, explicit deny, Forbidden); any other error
# is a FAIL, and every denied read has a positive control proving the object
# exists. The script changes nothing apart from its own test workspaces,
# which it deletes at the end (--keep leaves them). Twelve-digit numbers
# (account IDs) are masked in all output.
#
# Requirements: aws CLI v2, python3, the `agentcore` CLI (step 5), and for
# each listed profile the caller permissions of docs/getting-started.md
# (execute-api:Invoke on the registry, AgentCore data plane on its own plane,
# sts:AssumeRole on its own access role, cloudformation:DescribeStacks).
#
# Usage:
#   bin/verify-isolation.sh --profile-a <A> --profile-b <B> --profile-c <C>
#                           [--profile-sso <D>] [--registry-url <url>]
#                           [--harness <opencode|claude|pi>] [--keep]
#   (SCH_REGION, SCH_PROJECT, SCH_ENV select the deployment as for sch.)
set -uo pipefail

usage() {
    sed -n '2,45p' "$0" | sed 's/^# \{0,1\}//'
}

PROFILE_A=""
PROFILE_B=""
PROFILE_C=""
PROFILE_D=""
REGISTRY_URL="${SCH_WORKSPACE_REGISTRY_URL:-}"
HARNESS="opencode"
KEEP=0
while [ $# -gt 0 ]; do
    case "$1" in
        --profile-a|--profile-b|--profile-c|--profile-sso|--registry-url|--harness)
            [ $# -ge 2 ] || { echo "missing value for $1" >&2; exit 2; }
            case "$1" in
                --profile-a) PROFILE_A="$2" ;;
                --profile-b) PROFILE_B="$2" ;;
                --profile-c) PROFILE_C="$2" ;;
                --profile-sso) PROFILE_D="$2" ;;
                --registry-url) REGISTRY_URL="$2" ;;
                --harness) HARNESS="$2" ;;
            esac
            shift 2
            ;;
        --keep) KEEP=1; shift ;;
        --help|-h) usage; exit 0 ;;
        *) echo "unknown argument '$1' (see --help)" >&2; exit 2 ;;
    esac
done
[ -n "${PROFILE_A}" ] && [ -n "${PROFILE_B}" ] && [ -n "${PROFILE_C}" ] \
    || { echo "usage: $0 --profile-a <A> --profile-b <B> --profile-c <C> [--profile-sso <D>] (see --help)" >&2; exit 2; }
case "${HARNESS}" in
    opencode|claude|pi) ;;
    *) echo "invalid harness '${HARNESS}' (expected: opencode|claude|pi)" >&2; exit 2 ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# SCH_VERIFY_SCH / SCH_VERIFY_SUPPORT are test hooks (cli/tests/test_verify_scripts.py).
SCH="${SCH_VERIFY_SCH:-${SCRIPT_DIR}/sch}"
SUPPORT="${SCH_VERIFY_SUPPORT:-${SCRIPT_DIR}/../cli/sch/verify_support.py}"
REGION="${SCH_REGION:-eu-west-1}"
PROJECT="${SCH_PROJECT:-sch}"
ENVIRONMENT="${SCH_ENV:-dev}"
STACK="${PROJECT}-${ENVIRONMENT}-runtime"
WS="iso-verify-$(date +%s)"
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/sch-verify-isolation.XXXXXX")"

PASS=0
FAIL=0
ok()  { echo "PASS: $*"; PASS=$((PASS + 1)); }
bad() { echo "FAIL: $*"; FAIL=$((FAIL + 1)); }

# --- per-principal execution ----------------------------------------------------
# Each principal gets its own profile and its own sch config directory. The
# static credential variables are removed, because they would override
# AWS_PROFILE in the AWS CLI and SDKs.
as_principal() { # <label> <command...>
    local label="$1" profile
    shift
    case "${label}" in
        a) profile="${PROFILE_A}" ;;
        b) profile="${PROFILE_B}" ;;
        c) profile="${PROFILE_C}" ;;
        d) profile="${PROFILE_D}" ;;
    esac
    env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN \
        -u AWS_SECURITY_TOKEN -u AWS_DEFAULT_PROFILE \
        AWS_PROFILE="${profile}" \
        XDG_CONFIG_HOME="${TMP_DIR}/config-${label}" \
        SCH_WORKSPACE_REGISTRY_URL="${REGISTRY_URL}" \
        SCH_REGION="${REGION}" SCH_PROJECT="${PROJECT}" SCH_ENV="${ENVIRONMENT}" \
        "$@"
}

json_field() { # <json> <dotted.field>
    printf '%s' "$1" | python3 -c '
import json, sys
try:
    value = json.load(sys.stdin)
except ValueError:
    value = None
for part in sys.argv[1].split("."):
    value = value.get(part) if isinstance(value, dict) else None
print("" if value is None else ("true" if value is True else "false" if value is False else value))
' "$2"
}

is_denial() { # <text>
    printf '%s' "$1" | grep -Eqi 'AccessDenied|not authorized|explicit deny|Forbidden'
}

# expect_denied <label> <principal|-> <command...>: PASS only on an
# authorization error. "-" runs the command as is (helpers such as
# invoke_info choose the principal themselves).
run_as_label() { # <principal|-> <command...>
    local who="$1"
    shift
    if [ "${who}" = "-" ]; then "$@"; else as_principal "${who}" "$@"; fi
}

expect_denied() {
    local what="$1" who="$2" out rc=0
    shift 2
    out="$(run_as_label "${who}" "$@" 2>&1)" || rc=$?
    if [ "${rc}" -eq 0 ]; then
        bad "${what}: NOT denied (the call succeeded)"
    elif is_denial "${out}"; then
        ok "${what}: denied"
    else
        bad "${what}: failed without an authorization error: $(printf '%s' "${out}" | tail -n 1)"
    fi
}

expect_allowed() { # <label> <principal|-> <command...>
    local what="$1" who="$2" out rc=0
    shift 2
    out="$(run_as_label "${who}" "$@" 2>&1)" || rc=$?
    if [ "${rc}" -eq 0 ]; then
        ok "${what}"
    else
        bad "${what}: $(printf '%s' "${out}" | tail -n 1)"
    fi
}

stack_output() { # <principal> <output-key>
    local value
    value="$(as_principal "$1" aws cloudformation describe-stacks --stack-name "${STACK}" \
        --region "${REGION}" --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" \
        --output text 2>/dev/null)" || value=""
    [ "${value}" = "None" ] && value=""
    printf '%s\n' "${value}"
}

wait_terminal() { # <principal> -> final state on stdout
    local who="$1" raw="" state=""
    for _ in $(seq 1 90); do
        raw="$(as_principal "${who}" "${SCH}" status "${WS}" --json 2>/dev/null)" || true
        state="$(json_field "${raw}" state)"
        case "${state}" in
            succeeded|failed|timed-out|interrupted) break ;;
        esac
        sleep 5
    done
    printf '%s\n' "${state}"
}

invoke_info() { # <principal> <runtime-arn> <session-id> <payload-json>
    as_principal "$1" aws bedrock-agentcore invoke-agent-runtime \
        --cli-binary-format raw-in-base64-out \
        --agent-runtime-arn "$2" --runtime-session-id "$3" \
        --payload "$4" --region "${REGION}" "${TMP_DIR}/invoke-$1.json"
}

# Runs a shell command in A's microVM with A's identity; prints the command's
# combined output followed by a final "rc=<exit code>" line. The command must
# not contain single quotes (agentcore exec quoting, see verify-persistence.sh).
remote_a() {
    as_principal a agentcore exec --runtime "${ARN_A}" --session-id "${SID_A}" \
        --region "${REGION}" --timeout 300 --json -- sh -c "'$1 2>&1; echo rc=\$?'" 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin).get("stdout",""), end="")' 2>/dev/null
}

remote_expect_denied() { # <label> <command>
    local out
    out="$(remote_a "$2")"
    if printf '%s' "${out}" | grep -q '^rc=0$'; then
        bad "agent of A, $1: NOT denied (the call succeeded)"
    elif is_denial "${out}"; then
        ok "agent of A, $1: denied"
    else
        bad "agent of A, $1: failed without an authorization error: $(printf '%s' "${out}" | grep -v '^rc=' | tail -n 1)"
    fi
}

cleanup() {
    if [ "${KEEP}" -eq 1 ]; then
        echo "note: --keep: leaving workspace ${WS} of A and B"
    else
        as_principal a "${SCH}" delete "${WS}" --yes >/dev/null 2>&1 || echo "note: could not delete ${WS} of A"
        as_principal b "${SCH}" delete "${WS}" --yes >/dev/null 2>&1 || echo "note: could not delete ${WS} of B"
    fi
    rm -rf "${TMP_DIR}"
}

main() {
    local out rc key_a key_b status_a

    echo "############################################"
    echo "# per-principal isolation: live check"
    echo "# stack ${STACK} (${REGION}), workspace ${WS}, harness ${HARNESS}"
    echo "############################################"

    # --- 0. preflight -------------------------------------------------------
    echo "== 0. preflight =="
    command -v aws >/dev/null 2>&1 || { bad "aws CLI not found"; return 1; }
    command -v python3 >/dev/null 2>&1 || { bad "python3 not found"; return 1; }
    [ "$(stack_output a IsolationStatus)" = "true" ] \
        || { bad "stack ${STACK} does not report IsolationStatus=true (deploy with ISOLATED_PRINCIPALS first)"; return 1; }
    ok "stack reports IsolationStatus=true"
    if [ -z "${REGISTRY_URL}" ]; then
        REGISTRY_URL="$(stack_output a WorkspaceRegistryUrl)"
    fi
    [ -n "${REGISTRY_URL}" ] || { bad "no registry URL (set --registry-url or SCH_WORKSPACE_REGISTRY_URL)"; return 1; }
    BUCKET="$(stack_output a CheckpointBucketName)"
    TABLE="$(stack_output a WorkspaceRegistryTableName)"
    SHARED_ARN="$(stack_output a RuntimeArn)"
    [ -n "${BUCKET}" ] && [ -n "${TABLE}" ] && [ -n "${SHARED_ARN}" ] \
        || { bad "cannot read the checkpoint bucket, registry table or shared runtime from ${STACK}"; return 1; }

    local ids="" label id
    for label in a b c ${PROFILE_D:+d}; do
        id="$(as_principal "${label}" aws sts get-caller-identity --query UserId --output text 2>/dev/null)" \
            || { bad "profile ${label}: sts get-caller-identity failed"; return 1; }
        echo "   principal ${label}: $(as_principal "${label}" aws sts get-caller-identity --query Arn --output text 2>/dev/null)"
        ids="${ids}${id}"$'\n'
    done
    if [ "$(printf '%s' "${ids}" | sort -u | grep -c .)" -eq "$(printf '%s' "${ids}" | grep -c .)" ]; then
        ok "the profiles are distinct identities"
    else
        bad "two profiles resolve to the same identity"
        return 1
    fi

    out="$(as_principal a python3 "${SUPPORT}" probe)" || { bad "A: registry probe failed (is A listed?)"; return 1; }
    PREFIX_A="$(json_field "${out}" plane.ownerPrefix)"
    ACCESS_A="$(json_field "${out}" plane.accessRoleArn)"
    [ "$(json_field "${out}" isolation)" = "true" ] || { bad "A: the registry does not report isolation"; return 1; }
    out="$(as_principal b python3 "${SUPPORT}" probe)" || { bad "B: registry probe failed (is B listed?)"; return 1; }
    PREFIX_B="$(json_field "${out}" plane.ownerPrefix)"
    PLANE_ARN_B="$(json_field "${out}" plane.runtimeArn)"
    ACCESS_B="$(json_field "${out}" plane.accessRoleArn)"
    [ "$(json_field "${out}" isolation)" = "true" ] || { bad "B: the registry does not report isolation"; return 1; }
    if [ -n "${PREFIX_A}" ] && [ -n "${PREFIX_B}" ] && [ "${PREFIX_A}" != "${PREFIX_B}" ]; then
        ok "A and B have distinct planes (${PREFIX_A}, ${PREFIX_B})"
    else
        bad "A and B do not have distinct planes"
        return 1
    fi

    # --- 1. unlisted caller -------------------------------------------------
    echo "== 1. unlisted caller =="
    rc=0
    out="$(as_principal c python3 "${SUPPORT}" probe 2>&1)" || rc=$?
    if [ "${rc}" -eq 4 ] && printf '%s' "${out}" | grep -q 'is not an isolated principal' \
        && printf '%s' "${out}" | grep -Eq 'add (user|sso|role):[^ ]+ to ISOLATED_PRINCIPALS|only IAM users'; then
        ok "C is refused by the registry with the entry to add: $(printf '%s' "${out}" | grep -o 'add [^ ]* to ISOLATED_PRINCIPALS' | head -n 1)"
    else
        bad "C was not refused as an unlisted principal (exit ${rc}): $(printf '%s' "${out}" | tail -n 1)"
    fi

    # --- 2. own workflow ----------------------------------------------------
    echo "== 2. own workflow on each plane (same logical name ${WS}) =="
    for label in a b; do
        if as_principal "${label}" "${SCH}" task "${WS}" --harness "${HARNESS}" \
            "Reply exactly: isolation check ${label}" >/dev/null 2>"${TMP_DIR}/task-${label}.err"; then
            ok "${label}: sch task submitted"
        else
            bad "${label}: sch task failed: $(tail -n 1 "${TMP_DIR}/task-${label}.err")"
        fi
    done
    for label in a b; do
        status_a="$(wait_terminal "${label}")"
        [ "${status_a}" = "succeeded" ] && ok "${label}: task succeeded (sch status)" \
            || bad "${label}: task ended in state '${status_a:-unknown}'"
        if as_principal "${label}" "${SCH}" list 2>/dev/null | awk -v ws="${WS}" '$1 == ws { found = 1 } END { exit !found }'; then
            ok "${label}: sch list shows the workspace"
        else
            bad "${label}: sch list does not show the workspace"
        fi
        expect_allowed "${label}: sch list --remote-check" "${label}" "${SCH}" list --remote-check
        out="$(as_principal "${label}" python3 "${SUPPORT}" dashboard 2>&1)"
        if printf '%s' "${out}" | python3 -c '
import json, sys
rows = [r for r in json.load(sys.stdin) if r.get("name") == sys.argv[1]]
raise SystemExit(0 if rows and rows[0].get("taskState") == "succeeded" else 1)' "${WS}" 2>/dev/null; then
            ok "${label}: the dashboard data path reads the task state"
        else
            bad "${label}: the dashboard data path did not read the task state"
        fi
    done

    out="$(as_principal a python3 "${SUPPORT}" workspace "${WS}")" || { bad "A: cannot resolve ${WS}"; return 1; }
    ARN_A="$(json_field "${out}" runtimeArn)"
    SID_A="$(json_field "${out}" sessionId)"
    IDENT_A="$(json_field "${out}" runtimeWorkspace)"
    EPOCH_A="$(json_field "${out}" sessionEpoch)"
    STORAGE_A="$(json_field "${out}" storage)"
    key_a="$(json_field "${out}" checkpointPrefix)task-status.json"
    out="$(as_principal b python3 "${SUPPORT}" workspace "${WS}")" || { bad "B: cannot resolve ${WS}"; return 1; }
    SID_B="$(json_field "${out}" sessionId)"
    IDENT_B="$(json_field "${out}" runtimeWorkspace)"
    key_b="$(json_field "${out}" checkpointPrefix)task-status.json"
    if [ "${SID_A}" != "${SID_B}" ] && [ "${IDENT_A}" != "${IDENT_B}" ] && [ "${ARN_A}" != "${PLANE_ARN_B}" ]; then
        ok "the same logical name maps to distinct sessions, identities and runtimes"
    else
        bad "A and B share a session, identity or runtime for ${WS}"
    fi
    expect_allowed "A reads its own task status through its access role" a \
        python3 "${SUPPORT}" owner-exec -- aws s3api head-object --bucket "${BUCKET}" --key "${key_a}" --region "${REGION}"
    expect_allowed "B reads its own task status through its access role" b \
        python3 "${SUPPORT}" owner-exec -- aws s3api head-object --bucket "${BUCKET}" --key "${key_b}" --region "${REGION}"

    # --- 3. join denied -----------------------------------------------------
    echo "== 3. joining A's session with its runtime ARN and session ID =="
    local payload
    payload="{\"action\": \"info\", \"workspace\": \"${IDENT_A}\", \"owner_prefix\": \"${PREFIX_A}\", \"storage_backend\": \"${STORAGE_A}\", \"session_epoch\": ${EPOCH_A:-0}}"
    expect_allowed "A invokes its own session (positive control)" - invoke_info a "${ARN_A}" "${SID_A}" "${payload}"
    expect_denied "B invokes A's session" - invoke_info b "${ARN_A}" "${SID_A}" "${payload}"
    expect_denied "B stops A's session" b aws bedrock-agentcore stop-runtime-session \
        --agent-runtime-arn "${ARN_A}" --runtime-session-id "${SID_A}" --region "${REGION}"
    expect_denied "C invokes A's session" - invoke_info c "${ARN_A}" "${SID_A}" "${payload}"
    expect_denied "B invokes the shared runtime" - invoke_info b "${SHARED_ARN}" "${SID_A}" "${payload}"
    if command -v agentcore >/dev/null 2>&1; then
        out="$(as_principal b agentcore exec --runtime "${ARN_A}" --session-id "${SID_A}" \
            --region "${REGION}" --timeout 60 --json -- true 2>&1)" || true
        if is_denial "${out}"; then
            ok "B opens a command on A's session (agentcore exec): denied"
        else
            bad "B opens a command on A's session (agentcore exec): no authorization error: $(printf '%s' "${out}" | tail -n 1)"
        fi
    fi
    if [ -n "${PROFILE_D}" ]; then
        expect_denied "D (Identity Center) invokes A's session" - invoke_info d "${ARN_A}" "${SID_A}" "${payload}"
        expect_denied "D (Identity Center) stops A's session" d aws bedrock-agentcore stop-runtime-session \
            --agent-runtime-arn "${ARN_A}" --runtime-session-id "${SID_A}" --region "${REGION}"
    fi

    # --- 4. cross-owner reads from the CLI ----------------------------------
    echo "== 4. reading A's checkpoint objects as another principal =="
    expect_denied "B reads A's task status with its own credentials" b \
        aws s3api head-object --bucket "${BUCKET}" --key "${key_a}" --region "${REGION}"
    expect_denied "B reads A's task status through B's access role" b \
        python3 "${SUPPORT}" owner-exec -- aws s3api head-object --bucket "${BUCKET}" --key "${key_a}" --region "${REGION}"
    expect_denied "B lists A's owner tree through B's access role" b \
        python3 "${SUPPORT}" owner-exec -- aws s3api list-objects-v2 --bucket "${BUCKET}" --prefix "checkpoints/${PREFIX_A}/" --region "${REGION}"
    expect_denied "B assumes A's access role" b \
        aws sts assume-role --role-arn "${ACCESS_A}" --role-session-name sch-verify-isolation --region "${REGION}"
    expect_denied "C reads A's task status" c \
        aws s3api head-object --bucket "${BUCKET}" --key "${key_a}" --region "${REGION}"
    if [ -n "${PROFILE_D}" ]; then
        expect_denied "D (Identity Center) reads A's task status" d \
            aws s3api head-object --bucket "${BUCKET}" --key "${key_a}" --region "${REGION}"
        expect_denied "D (Identity Center) assumes A's access role" d \
            aws sts assume-role --role-arn "${ACCESS_A}" --role-session-name sch-verify-isolation --region "${REGION}"
    fi

    # --- 5. cross-owner reads from inside A's agent -------------------------
    echo "== 5. reading other owners' data from inside A's microVM =="
    if ! command -v agentcore >/dev/null 2>&1; then
        bad "agentcore CLI not found: the in-agent checks cannot run"
    else
        out="$(remote_a "aws s3api head-object --bucket ${BUCKET} --key ${key_a} --region ${REGION} >/dev/null")"
        if printf '%s' "${out}" | grep -q '^rc=0$'; then
            ok "agent of A reads its own task status (positive control)"
        else
            bad "agent of A cannot read its own task status: $(printf '%s' "${out}" | grep -v '^rc=' | tail -n 1)"
        fi
        out="$(as_principal b python3 "${SUPPORT}" owner-exec -- aws s3api head-object --bucket "${BUCKET}" --key "${key_b}" --region "${REGION}" 2>&1)" \
            && ok "B's task status exists (positive control for the next check)" \
            || bad "B's task status is not readable by B: $(printf '%s' "${out}" | tail -n 1)"
        remote_expect_denied "read B's task status" \
            "aws s3api head-object --bucket ${BUCKET} --key ${key_b} --region ${REGION}"
        remote_expect_denied "list B's owner tree" \
            "aws s3api list-objects-v2 --bucket ${BUCKET} --prefix checkpoints/${PREFIX_B}/ --region ${REGION}"
        remote_expect_denied "list the whole checkpoints/ tree" \
            "aws s3api list-objects-v2 --bucket ${BUCKET} --prefix checkpoints/ --region ${REGION}"
        remote_expect_denied "scan the registry table" \
            "aws dynamodb scan --table-name ${TABLE} --max-items 1 --region ${REGION}"
        remote_expect_denied "read B's plane parameter" \
            "aws ssm get-parameter --name /${PROJECT}/${ENVIRONMENT}/planes/${PREFIX_B#o.} --region ${REGION}"
        remote_expect_denied "read B's runtime configuration" \
            "aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id ${PLANE_ARN_B##*/} --region ${REGION}"
        remote_expect_denied "assume B's access role" \
            "aws sts assume-role --role-arn ${ACCESS_B} --role-session-name sch-verify-isolation --region ${REGION}"
    fi

    echo
    echo "############################################"
    echo "# isolation result: ${PASS} passed, ${FAIL} failed"
    echo "############################################"
    [ "${FAIL}" -eq 0 ]
}

run() {
    trap cleanup EXIT
    main
}

# Mask account IDs (any 12-digit run) in everything the check prints.
run 2>&1 | sed -E 's/[0-9]{12}/<account-id>/g'
exit "${PIPESTATUS[0]}"
