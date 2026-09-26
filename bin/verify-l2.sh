#!/bin/bash
# verify-l2.sh — end-to-end verification of the L2 durability checkpoint
# cycle (spec: workspace-checkpointing, "End-to-end verification of the
# checkpoint, loss, restore cycle"; design D8). Twin script to
# verify-persistence.sh: that script exercises the L1 stop/resume cycle on
# the SAME runtimeSessionId; this one simulates TOTAL L1 loss via sessionId
# rotation (`sch reset-session`) — the same observable effect as storage
# expiry (14 days) or a runtime version update (`deploy.sh -v`): a brand-new,
# empty session storage for the SAME workspace name, without waiting 14 days
# or doing a real image bump.
#
# Flow:
#   setup      : known state (sentinel + uncommitted change, harness
#                session with history, config marker) — same recipe as
#                verify-persistence.sh
#   checkpoint : `sch stop <ws>` (forces a synchronous, full L2 checkpoint
#                before StopRuntimeSession — spec: "Checkpoint finale
#                sincrono allo stop")
#   rotate     : `sch reset-session <ws>` (simulates total L1 loss: new
#                runtimeSessionId, empty session storage, SAME workspace
#                name — the exact case L2 restore must cover)
#   verify     : open a shell on the rotated session and assert worktree,
#                harness session history, and user config were all
#                restored from S3
#   scenarios  : task 6.3 — checkpoint upload after a worktree change, no
#                upload on an idle workspace, manifest updated (forced) on
#                `sch stop`. Independent of rotate/verify, run on the
#                current session as-is.
#
# Every check reports an explicit PASS/FAIL; the script exits non-zero (and
# the final summary shows which check failed) if any assertion fails (task
# 6.2).
#
# Usage:
#   ./verify-l2.sh <workspace> [setup|checkpoint|rotate|verify|scenarios|full] [--harness <opencode|pi>]
#   ./verify-l2.sh <workspace>                 # backward-compatible: full cycle with opencode
#   ./verify-l2.sh <workspace> setup --harness pi
#   ./verify-l2.sh <workspace> --harness pi    # full cycle with pi
set -uo pipefail

usage() {
    echo "usage: $0 <workspace> [setup|checkpoint|rotate|verify|scenarios|full] [--harness <opencode|pi>]"
}

WS=""
PHASE="full"
PHASE_SET=false
HARNESS_FLAG=""
while [ $# -gt 0 ]; do
    case "$1" in
        --harness)
            [ $# -ge 2 ] || { usage; exit 2; }
            HARNESS_FLAG="$2"
            shift 2
            ;;
        --help|-h)
            sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            if [ -z "${WS}" ]; then
                WS="$1"
            elif [ "${PHASE_SET}" = false ]; then
                PHASE="$1"
                PHASE_SET=true
            else
                usage >&2
                exit 2
            fi
            shift
            ;;
    esac
done
[ -n "${WS}" ] || { usage; exit 2; }
HARNESS="${HARNESS_FLAG:-opencode}"
case "${HARNESS}" in
    opencode|pi) ;;
    *) echo "invalid harness '${HARNESS}' (expected: opencode|pi)" >&2; exit 2 ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCH="${SCRIPT_DIR}/sch"
SCH_REGION="${SCH_REGION:-eu-west-1}"
REQUESTED_STORAGE="${SCH_VERIFY_STORAGE:-s3}"
STATE_FILE="${TMPDIR:-/tmp}/sch-verify-l2-${WS}.state"

PASS=0
FAIL=0
ok()  { echo "PASS: $*"; PASS=$((PASS+1)); }
bad() { echo "FAIL: $*"; FAIL=$((FAIL+1)); }

# --- resolution helpers ----------------------------------------------------------
runtime_arn() {
    aws cloudformation describe-stacks \
        --stack-name "${SCH_PROJECT:-sch}-${SCH_ENV:-dev}-runtime" \
        --region "${SCH_REGION}" \
        --query "Stacks[0].Outputs[?OutputKey=='RuntimeArn'].OutputValue" \
        --output text
}

checkpoint_bucket() {
    aws cloudformation describe-stacks \
        --stack-name "${SCH_PROJECT:-sch}-${SCH_ENV:-dev}-runtime" \
        --region "${SCH_REGION}" \
        --query "Stacks[0].Outputs[?OutputKey=='CheckpointBucketName'].OutputValue" \
        --output text
}

# --- remote one-shot helper (see verify-persistence.sh for the quoting note) ----
remote() { # <command string, single-quote free> -> stdout
    agentcore exec \
        --runtime "${ARN}" \
        --session-id "${SID}" \
        --region "${SCH_REGION}" \
        --timeout 300 \
        --json \
        -- sh -c "'$1'" 2>/dev/null
}

remote_stdout() {
    remote "$1" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("stdout",""), end="")'
}

wait_ready() { # [storage-hint: fresh|resumed]
    local hint="${1:-}"
    local payload="{\"action\": \"info\", \"workspace\": \"${WS}\", \"harness\": \"${HARNESS}\", \"storage_backend\": \"${STORAGE}\", \"session_epoch\": ${SESSION_EPOCH}}"
    [ -n "${hint}" ] && payload="{\"action\": \"info\", \"storage\": \"${hint}\", \"storage_backend\": \"${STORAGE}\", \"session_epoch\": ${SESSION_EPOCH}, \"workspace\": \"${WS}\", \"harness\": \"${HARNESS}\"}"
    echo "-- waiting for microVM boot + storage/L2 restore (hint: ${hint:-none}, max 360s)"
    local out="${TMPDIR:-/tmp}/sch-info-$$.json"
    for _ in $(seq 1 72); do
        local phase=""
        if aws bedrock-agentcore invoke-agent-runtime \
            --cli-binary-format raw-in-base64-out \
            --agent-runtime-arn "${ARN}" \
            --runtime-session-id "${SID}" \
            --payload "${payload}" \
            --region "${SCH_REGION}" \
            "${out}" >/dev/null 2>&1; then
            phase=$(python3 -c "import json; print(json.load(open('${out}')).get('boot',{}).get('phase',''))" 2>/dev/null)
        fi
        if [ "${phase}" = "ready" ]; then
            echo "   microVM ready ($(python3 -c "
import json
b = json.load(open('${out}')).get('boot', {})
print('mount=%s restore=%s restore_l2=%s' % (b.get('mount'), b.get('restore_result'), b.get('restore_l2')))
" 2>/dev/null))"
            rm -f "${out}"
            return 0
        fi
        sleep 5
    done
    rm -f "${out}"
    echo "   WARNING: shim not ready after 360s, proceeding anyway"
    return 0
}

# Fetches a fresh `info` response and returns the path to the saved JSON
# (caller removes it). Used for both the checkpoint status block and the
# boot.restore_l2 field.
info_snapshot() {
    local out="${TMPDIR:-/tmp}/sch-info-snap-$$.json"
    aws bedrock-agentcore invoke-agent-runtime \
        --cli-binary-format raw-in-base64-out \
        --agent-runtime-arn "${ARN}" \
        --runtime-session-id "${SID}" \
        --payload "{\"action\": \"info\", \"workspace\": \"${WS}\", \"harness\": \"${HARNESS}\", \"storage_backend\": \"${STORAGE}\", \"session_epoch\": ${SESSION_EPOCH}}" \
        --region "${SCH_REGION}" \
        "${out}" >/dev/null 2>&1
    echo "${out}"
}

manifest_last_modified() {
    aws s3api head-object \
        --bucket "$(checkpoint_bucket)" \
        --key "checkpoints/${WS}/manifest.json" \
        --region "${SCH_REGION}" \
        --query 'LastModified' --output text 2>/dev/null || echo ""
}

ARN=$(runtime_arn) || { echo "cannot resolve runtime ARN"; exit 1; }
workspace_sid() {
    python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("runtimeSessionId") or d.get("sessionId") or "")' "$1" 2>/dev/null || cat "$1"
}
workspace_storage() {
    python3 -c 'import json,sys
try:
 d=json.load(open(sys.argv[1])); print(d.get("storage") or "session")
except Exception: print("session")' "$1"
}
workspace_epoch() {
    python3 -c 'import json,sys
try:
 d=json.load(open(sys.argv[1])); print(d.get("sessionEpoch", 0))
except Exception: print(0)' "$1"
}
# Same workspace -> session mapping used by sch (create it if new).
WS_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/sch/workspaces/${WS}"
if [ ! -f "${WS_FILE}" ]; then
    mkdir -p "$(dirname "${WS_FILE}")"
    python3 -c 'import json,sys,uuid; print(json.dumps({"runtimeSessionId":f"sch-{sys.argv[1]}-{uuid.uuid4()}","harness":sys.argv[2],"storage":sys.argv[3],"sessionEpoch":1}))' "${WS}" "${HARNESS}" "${REQUESTED_STORAGE}" > "${WS_FILE}"
fi
SID=$(workspace_sid "${WS_FILE}")
STORAGE=$(workspace_storage "${WS_FILE}")
SESSION_EPOCH=$(workspace_epoch "${WS_FILE}")
WORKSPACE_ROOT=$([ "${STORAGE}" = "s3" ] && echo /home/sch/workspace || echo /mnt/workspace)
REPO_DIR="${WORKSPACE_ROOT}/repo"
STATE_DIR="${WORKSPACE_ROOT}/state"
PI_CONFIG_DIR="/home/sch/.pi/agent"
echo "workspace=${WS} session=${SID} harness=${HARNESS} storage=${STORAGE} root=${WORKSPACE_ROOT}"
echo

do_setup() {
    local magic="sch-l2-$(uuidgen | tr '[:upper:]' '[:lower:]' | cut -c1-8)"
    echo "== setup: creating known state (magic=${magic}) =="
    SID=$(workspace_sid "${WS_FILE}")
    SESSION_EPOCH=$(workspace_epoch "${WS_FILE}")
    wait_ready

    echo "-- sentinel file + uncommitted git change"
    remote_stdout "cd ${REPO_DIR} && echo ${magic} > sentinel.txt && git add sentinel.txt && echo uncommitted-${magic} >> sentinel.txt && git status --porcelain && cat sentinel.txt"

    if [ "${HARNESS}" = "pi" ]; then
        echo "-- Pi state marker"
        remote_stdout "echo ${magic} > ${PI_CONFIG_DIR}/verify-l2-marker && cat ${PI_CONFIG_DIR}/verify-l2-marker"

        echo "-- Pi session with a remembered keyword"
        remote_stdout "cd ${REPO_DIR} && SCH_HARNESS=pi pi -p --append-system-prompt ${PI_CONFIG_DIR}/roles/remote-interactive.md \"Remember this keyword: ${magic}. Reply with exactly: ${magic}\" < /dev/null 2>&1 | tail -3"

        echo "-- recording Pi session files"
        remote_stdout "find ${PI_CONFIG_DIR}/sessions -type f -name \"*.jsonl\" 2>/dev/null | tail -20"
    else
        echo "-- config marker (fetch, modify locally, push via base64)"
        local cfg new_b64
        cfg=$(remote_stdout "cat ${STATE_DIR}/config/opencode/opencode.json 2>/dev/null")
        [ -n "${cfg}" ] || cfg='{}'
        new_b64=$(echo "${cfg}" | python3 -c "import json,sys; d=json.load(sys.stdin); d['username']='${magic}'; print(json.dumps(d,indent=2))" | base64 | tr -d '\n')
        remote_stdout "echo ${new_b64} | base64 -d > ${STATE_DIR}/config/opencode/opencode.json && grep -o ${magic} ${STATE_DIR}/config/opencode/opencode.json"

        echo "-- OpenCode session with a remembered keyword (stdin redirected to /dev/null, see README operational note)"
        remote_stdout "cd ${REPO_DIR} && opencode run --standalone --title sch-verify-l2-${magic} \"Remember this keyword: ${magic}. Reply with exactly: ${magic}\" < /dev/null 2>&1 | tail -3"

        echo "-- recording OpenCode session list"
        remote_stdout "cd ${REPO_DIR} && opencode session list 2>/dev/null | head -20"
    fi

    echo "${magic}" > "${STATE_FILE}"
    echo
    echo "setup done; magic saved to ${STATE_FILE}"
}

do_checkpoint() {
    echo
    echo "== checkpoint: sch stop (forces a synchronous, full L2 checkpoint) =="
    "${SCH}" stop "${WS}"
}

do_rotate() {
    echo
    echo "== rotate: sch reset-session (simulates total L1 loss for '${WS}') =="
    local old_sid
    old_sid=$(workspace_sid "${WS_FILE}")
    echo y | "${SCH}" reset-session "${WS}"
    SID=$(workspace_sid "${WS_FILE}")
    SESSION_EPOCH=$(workspace_epoch "${WS_FILE}")
    if [ "${SID}" != "${old_sid}" ]; then
        ok "reset-session: new sessionId assigned (${SID}, was ${old_sid})"
    else
        bad "reset-session: sessionId unchanged (${SID})"
    fi
    echo "waiting 5s before reopening..."
    sleep 5
}

do_verify() {
    [ -f "${STATE_FILE}" ] || { echo "no state file ${STATE_FILE} — run setup first"; exit 1; }
    local magic
    magic=$(cat "${STATE_FILE}")
    SID=$(workspace_sid "${WS_FILE}")
    SESSION_EPOCH=$(workspace_epoch "${WS_FILE}")
    echo
    echo "== verify: reopening rotated session and asserting L2-restored state (magic=${magic}) =="
    # The runtimeSessionId is brand new to AgentCore -- but sch's local
    # bookkeeping file for the workspace already existed before rotation, so
    # `sch shell` would send hint="resumed" here too; either hint is fine,
    # the shim's own restore decision is driven by ACTUAL mount emptiness
    # (_mount_storage_empty), not by the hint.
    wait_ready resumed

    local sentinel
    sentinel=$(remote_stdout "cat ${REPO_DIR}/sentinel.txt 2>/dev/null")
    if echo "${sentinel}" | grep -q "^${magic}$" && echo "${sentinel}" | grep -q "uncommitted-${magic}"; then
        ok "worktree: sentinel file present with identical content (incl. uncommitted change) after L2 restore"
    else
        bad "worktree: sentinel missing or content changed after L2 restore: '${sentinel}'"
    fi

    local gitstat
    gitstat=$(remote_stdout "cd ${REPO_DIR} && git status --porcelain 2>/dev/null")
    if echo "${gitstat}" | grep -q "sentinel.txt"; then
        ok "git: uncommitted change restored (${gitstat})"
    else
        bad "git: uncommitted change lost after L2 restore (status: '${gitstat}')"
    fi

    local sessions statehit
    if [ "${HARNESS}" = "pi" ]; then
        sessions=$(remote_stdout "find ${PI_CONFIG_DIR}/sessions -type f -name \"*.jsonl\" 2>/dev/null | tail -20")
        statehit=$(remote_stdout "grep -rla ${magic} ${PI_CONFIG_DIR}/sessions 2>/dev/null | head -5")
    else
        sessions=$(remote_stdout "cd ${REPO_DIR} && opencode session list 2>/dev/null | head -20")
        statehit=$(remote_stdout "grep -rla ${magic} ${STATE_DIR}/data 2>/dev/null | head -5")
    fi
    if echo "${sessions}" | grep -q "${magic}" || [ -n "${statehit}" ]; then
        ok "${HARNESS}: session with history restored after L2 restore (list or state files)"
    else
        bad "${HARNESS}: no trace of session ${magic} after L2 restore. Sessions: ${sessions}"
    fi

    local marker
    if [ "${HARNESS}" = "pi" ]; then
        marker=$(remote_stdout "cat ${PI_CONFIG_DIR}/verify-l2-marker 2>/dev/null")
    else
        marker=$(remote_stdout "grep -o ${magic} ${STATE_DIR}/config/opencode/opencode.json 2>/dev/null")
    fi
    if [ "${marker}" = "${magic}" ]; then
        ok "${HARNESS}: user state marker restored"
    else
        bad "${HARNESS}: user state marker missing after L2 restore"
    fi

    local info_file restore_l2
    info_file=$(info_snapshot)
    restore_l2=$(python3 -c "import json; print(json.dumps(json.load(open('${info_file}')).get('boot',{}).get('restore_l2',{})))" 2>/dev/null || echo "{}")
    rm -f "${info_file}"
    echo "-- boot.restore_l2 after reopen: ${restore_l2}"
    if echo "${restore_l2}" | grep -q '"result": "restored"'; then
        ok "shim reports restore_l2.result=restored"
    else
        bad "shim does not report restore_l2.result=restored (${restore_l2})"
    fi

    echo "-- task --continue must recover the pre-reset keyword"
    local continue_task continue_state continue_output continue_status continue_status_file
    continue_task=$("${SCH}" task "${WS}" --harness "${HARNESS}" --continue "What keyword did I ask you to remember? Reply with only the keyword." 2>/dev/null)
    if [ -n "${continue_task}" ]; then
        ok "${HARNESS}: continuation task submitted (${continue_task:0:12})"
    else
        bad "${HARNESS}: continuation task submission failed"
    fi
    continue_state=""
    continue_output=""
    if [ -n "${continue_task}" ]; then
        for _ in $(seq 1 120); do
            continue_status_file="${TMPDIR:-/tmp}/sch-l2-continue-$$.json"
            if "${SCH}" status "${WS}" --json > "${continue_status_file}" 2>/dev/null; then
                continue_status=$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print("%s\t%s" % (d.get("task_id", ""), d.get("state", "")))' "${continue_status_file}" 2>/dev/null)
                if [ "${continue_status%%$'\t'*}" = "${continue_task}" ]; then
                    continue_state="${continue_status#*$'\t'}"
                    case "${continue_state}" in
                        succeeded|failed|timed-out|interrupted)
                            continue_output=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("output", ""))' "${continue_status_file}" 2>/dev/null)
                            rm -f "${continue_status_file}"
                            break
                            ;;
                    esac
                fi
            fi
            rm -f "${continue_status_file}"
            sleep 2
        done
    fi
    if [ "${continue_state}" = "succeeded" ] && echo "${continue_output}" | grep -q "${magic}"; then
        ok "${HARNESS}: task --continue recovered the keyword after reset/restore"
    else
        bad "${HARNESS}: task --continue did not recover the keyword (state=${continue_state:-unknown}, output='${continue_output}')"
    fi

    echo
    echo "== result: ${PASS} passed, ${FAIL} failed =="
    [ "${FAIL}" -eq 0 ] || exit 1
}

do_scenarios() {
    echo
    echo "== scenarios (task 6.3): checkpoint upload / no-upload / manifest-on-stop =="
    SID=$(workspace_sid "${WS_FILE}")
    wait_ready

    local info_file interval
    info_file=$(info_snapshot)
    interval=$(python3 -c "import json; print(json.load(open('${info_file}')).get('checkpoint',{}).get('interval_s',60))" 2>/dev/null || echo 60)
    rm -f "${info_file}"
    [ "${interval}" -gt 0 ] 2>/dev/null || { echo "checkpoint disabled (interval_s=${interval}); scenarios need SCH_CHECKPOINT_INTERVAL > 0"; exit 2; }
    echo "-- checkpoint interval_s=${interval}"

    echo "-- baseline manifest LastModified"
    local before
    before=$(manifest_last_modified)
    echo "   manifest before: ${before:-<absent>}"

    echo "-- idle: waiting one checkpoint interval + margin with no changes"
    sleep "$((interval + 15))"
    local after_idle
    after_idle=$(manifest_last_modified)
    if [ "${after_idle}" = "${before}" ]; then
        ok "no upload on idle workspace (manifest unchanged: ${after_idle:-<absent>})"
    else
        bad "manifest changed on idle workspace (${before:-<absent>} -> ${after_idle})"
    fi

    echo "-- modify worktree, wait one more interval + margin"
    remote_stdout "cd ${REPO_DIR} && date > scenario-touch.txt"
    sleep "$((interval + 15))"
    local after_change
    after_change=$(manifest_last_modified)
    if [ -n "${after_change}" ] && [ "${after_change}" != "${after_idle}" ]; then
        ok "upload after worktree modification (manifest updated: ${after_idle:-<absent>} -> ${after_change})"
    else
        bad "manifest did not update after worktree modification (${after_idle:-<absent>} -> ${after_change:-<absent>})"
    fi

    echo "-- sch stop: manifest must be updated (forced) even without further changes"
    local before_stop
    before_stop=$(manifest_last_modified)
    "${SCH}" stop "${WS}"
    local after_stop
    after_stop=$(manifest_last_modified)
    if [ -n "${after_stop}" ] && [ "${after_stop}" != "${before_stop}" ]; then
        ok "manifest updated on stop (forced checkpoint: ${before_stop:-<absent>} -> ${after_stop})"
    else
        bad "manifest not updated on stop (${before_stop:-<absent>} -> ${after_stop:-<absent>})"
    fi

    echo
    echo "== result: ${PASS} passed, ${FAIL} failed =="
    [ "${FAIL}" -eq 0 ] || exit 1
}

case "${PHASE}" in
    setup)      do_setup ;;
    checkpoint) do_checkpoint ;;
    rotate)     do_rotate ;;
    verify)     do_verify ;;
    scenarios)  do_scenarios ;;
    full)       do_setup; do_checkpoint; do_rotate; do_verify ;;
    *) echo "unknown phase '${PHASE}' (setup|checkpoint|rotate|verify|scenarios|full)"; exit 2 ;;
esac
