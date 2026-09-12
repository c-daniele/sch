#!/bin/bash
# verify-headless-tasks.sh — end-to-end verification of headless detached tasks
# (spec: headless-task-execution, workspace-checkpointing, runtime-image).
#
# Multi-harness (sch-multi-harness, task 8.1): parameterized with
# `--harness <opencode|claude|pi>`. The OpenCode path is byte-identical to the
# pre-multi-harness flow when `--harness opencode` (or no flag, now defaulting to
# opencode for backward-compat with existing callers) is passed.
#
# Flow:
#   1. submit a simple task, assert immediate return with task_id
#   2. while that task is still running, submit a second and assert `status: busy`
#   3. poll `sch status` (offline S3 read) until the first task is terminal
#   4. stop the workspace, then status still returns the terminal outcome
#   5. reopen a shell and assert the task's work was checkpointed
#   6. simulate microVM death mid-task, assert orphan reconciliation -> interrupted
#   7. brainstorm with the interactive agent, stop, then `sch task --continue`
#      resumes the session under the unattended agent and checkpoints it
#   7b. stop again, then `sch task --continue` with NO warmup (cold boot):
#      the task still resumes the pre-stop session and records the outcome
#   8. submit a task with `--timeout 5` and a sleep prompt, assert timed-out
#
# Usage:
#   ./verify-headless-tasks.sh <workspace> [--harness <opencode|claude|pi>]
set -uo pipefail

WS=""
HARNESS_FLAG=""
# Parse args: first positional is workspace; --harness <x> optional.
while [ $# -gt 0 ]; do
    case "$1" in
        --harness)
            [ $# -ge 2 ] || { echo "usage: $0 <workspace> [--harness <opencode|claude|pi>]"; exit 2; }
            HARNESS_FLAG="$2"
            shift 2
            ;;
        --help|-h)
            sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            if [ -z "${WS}" ]; then
                WS="$1"
            else
                echo "usage: $0 <workspace> [--harness <opencode|claude|pi>]" >&2
                exit 2
            fi
            shift
            ;;
    esac
done
[ -n "${WS}" ] || { echo "usage: $0 <workspace> [--harness <opencode|claude|pi>]"; exit 2; }
# Default to opencode for backward-compat with the pre-multi-harness caller
# (task 8.1: "keep the OpenCode path byte-identical when the flag is `opencode`").
HARNESS="${HARNESS_FLAG:-opencode}"
case "${HARNESS}" in
    opencode|claude|pi) ;;
    *) echo "invalid harness '${HARNESS}' (expected: opencode|claude|pi)" >&2; exit 2 ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCH="${SCRIPT_DIR}/sch"
SCH_REGION="${SCH_REGION:-eu-west-1}"
REQUESTED_STORAGE="${SCH_VERIFY_STORAGE:-s3}"

PASS=0
FAIL=0
ok()   { echo "PASS [harness=${HARNESS}]: $*"; PASS=$((PASS+1)); }
bad()  { echo "FAIL [harness=${HARNESS}]: $*"; FAIL=$((FAIL+1)); }

runtime_arn() {
    aws cloudformation describe-stacks \
        --stack-name "${SCH_PROJECT:-sch}-${SCH_ENV:-dev}-runtime" \
        --region "${SCH_REGION}" \
        --query "Stacks[0].Outputs[?OutputKey=='RuntimeArn'].OutputValue" \
        --output text
}

_invoke_info() {
    aws bedrock-agentcore invoke-agent-runtime \
        --cli-binary-format raw-in-base64-out \
        --agent-runtime-arn "${ARN}" \
        --runtime-session-id "${SID}" \
        --payload "{\"action\": \"info\", \"workspace\": \"${WS}\", \"storage_backend\": \"${STORAGE}\", \"session_epoch\": ${SESSION_EPOCH}}" \
        --region "${SCH_REGION}" \
        "${TMPDIR:-/tmp}/sch-info-$$.json" >/dev/null 2>&1 \
        && cat "${TMPDIR:-/tmp}/sch-info-$$.json"
}

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

status_json() {
    "${SCH}" status "${WS}" --json
}

status_field() { # <jq-style field name>
    status_json | python3 -c "import json,sys; print(json.load(sys.stdin).get('$1',''))"
}

wait_terminal_for_task() { # <task_id> <max_wait>
    local expected_task_id="$1"
    local max_wait="${2:-180}"
    echo "-- polling status for task ${expected_task_id:0:12}... (max ${max_wait}s)"
    local i
    for i in $(seq 1 "${max_wait}"); do
        local st tid
        tid=$(status_field "task_id")
        st=$(status_field "state")
        if [ "${tid}" = "${expected_task_id}" ]; then
            case "${st}" in
                succeeded|failed|timed-out|interrupted)
                    echo "   terminal state: ${st}"
                    return 0
                    ;;
            esac
        fi
        sleep 1
    done
    echo "   timed out waiting for terminal state"
    return 1
}

wait_boot_ready() {
    local max_wait="${1:-240}"
    echo "-- waiting for shim boot ready (max ${max_wait}s)"
    local i
    for i in $(seq 1 "${max_wait}"); do
        local phase
        phase=$(_invoke_info | python3 -c 'import json,sys; print(json.load(sys.stdin).get("boot",{}).get("phase",""))' 2>/dev/null)
        if [ "${phase}" = "ready" ]; then
            echo "   boot ready"
            return 0
        fi
        sleep 1
    done
    echo "   boot not ready after ${max_wait}s"
    return 1
}

ARN=$(runtime_arn) || { echo "cannot resolve runtime ARN"; exit 1; }
WS_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/sch/workspaces/${WS}"
# Ensure the workspace index exists with the chosen harness. If absent, create
# it via `sch shell` (which would exec agentcore) — instead, seed it directly
# with the harness so the rest of the script can submit tasks without opening
# a TUI. Falls back to the bare-sid legacy format if `sch` is too old to know
# about harness (the shim's upgrade_reconcile handles that on first noop).
if [ ! -f "${WS_FILE}" ]; then
    mkdir -p "$(dirname "${WS_FILE}")"
    python3 -c '
import json, sys
ws, harness, storage = sys.argv[1], sys.argv[2], sys.argv[3]
import uuid
print(json.dumps({"runtimeSessionId": f"sch-{ws}-{uuid.uuid4()}", "harness": harness, "storage": storage, "sessionEpoch": 1}))
' "${WS}" "${HARNESS}" "${REQUESTED_STORAGE}" > "${WS_FILE}"
fi
SID=$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("runtimeSessionId") or d.get("sessionId") or "")' "${WS_FILE}" 2>/dev/null || cat "${WS_FILE}")
STORAGE=$(python3 -c 'import json,sys
try:
 d=json.load(open(sys.argv[1])); print(d.get("storage") or "session")
except Exception: print("session")' "${WS_FILE}")
SESSION_EPOCH=$(python3 -c 'import json,sys
try:
 d=json.load(open(sys.argv[1])); print(d.get("sessionEpoch", 0))
except Exception: print(0)' "${WS_FILE}")
WORKSPACE_ROOT=$([ "${STORAGE}" = "s3" ] && echo /home/sch/workspace || echo /mnt/workspace)
REPO_DIR="${WORKSPACE_ROOT}/repo"
echo "workspace=${WS} session=${SID} harness=${HARNESS} storage=${STORAGE}"
echo

# Unique markers for this run.
MAGIC="sch-hl-$(uuidgen | tr '[:upper:]' '[:lower:]' | cut -c1-8)"
MARKER_FILE="${REPO_DIR}/task-marker-${MAGIC}"

echo "== 0. warm: provision microVM so submit is non-blocking =="
aws bedrock-agentcore invoke-agent-runtime \
    --cli-binary-format raw-in-base64-out \
    --agent-runtime-arn "${ARN}" \
    --runtime-session-id "${SID}" \
    --payload "{\"action\": \"noop\", \"storage\": \"resumed\", \"storage_backend\": \"${STORAGE}\", \"session_epoch\": ${SESSION_EPOCH}, \"workspace\": \"${WS}\", \"harness\": \"${HARNESS}\"}" \
    --region "${SCH_REGION}" \
    /dev/null >/dev/null 2>&1 || true
wait_boot_ready 240
echo

echo "== 1. submit: immediate return with task_id =="
start=$(date +%s)
TASK1=$("${SCH}" task "${WS}" --harness "${HARNESS}" "Create a file at ${MARKER_FILE} containing exactly the line DONE-${MAGIC} and nothing else.")
elapsed=$(( $(date +%s) - start ))
if [ -n "${TASK1}" ] && [ "${elapsed}" -le 5 ]; then
    ok "submit returned task_id=${TASK1} in ${elapsed}s"
else
    bad "submit failed or too slow (task_id='${TASK1}', elapsed=${elapsed}s)"
fi

echo
echo "== 2. concurrency rejection: second submit while first is running =="
BUSY_ACK=$("${SCH}" task "${WS}" --harness "${HARNESS}" "Run the shell command 'echo should-not-run' and report the output." 2>&1 || true)
if echo "${BUSY_ACK}" | grep -q "busy"; then
    ok "second submit rejected with busy"
else
    bad "second submit not rejected: ${BUSY_ACK}"
fi

echo
echo "== 3. detached completion: poll status (offline S3 read) until terminal =="
if wait_terminal_for_task "${TASK1}" 240; then
    TASK1_STATE=$(status_field "state")
    TASK1_EXIT=$(status_field "exit_code")
    TASK1_HARNESS=$(status_field "harness")
    TASK1_CHECKPOINT=$(status_field "checkpoint_status")
    if [ "${TASK1_STATE}" = "succeeded" ] && [ "${TASK1_EXIT}" = "0" ]; then
        ok "task1 terminal state=${TASK1_STATE} exit_code=${TASK1_EXIT}"
    else
        bad "task1 unexpected terminal state=${TASK1_STATE} exit_code=${TASK1_EXIT}"
    fi
    if [ "${TASK1_CHECKPOINT}" = "confirmed" ]; then
        ok "task1 final checkpoint confirmed"
    else
        bad "task1 checkpoint status=${TASK1_CHECKPOINT}, expected confirmed"
    fi
    # Task 8.3 assertion: harness field present in sch status output.
    if [ "${TASK1_HARNESS}" = "${HARNESS}" ]; then
        ok "task1 status harness field matches: ${TASK1_HARNESS}"
    else
        bad "task1 status harness field mismatch: got '${TASK1_HARNESS}', expected '${HARNESS}'"
    fi
else
    bad "task1 did not reach terminal state"
fi

echo
echo "== 4. offline status: stop workspace, then status still returns outcome =="
"${SCH}" stop "${WS}" >/dev/null 2>&1 || true
sleep 15
OFFLINE_STATE=$(status_field "state")
if [ "${OFFLINE_STATE}" = "succeeded" ]; then
    ok "offline status after stop: state=${OFFLINE_STATE}"
else
    bad "offline status after stop: state=${OFFLINE_STATE}"
fi

echo
echo "== 5. checkpoint captured task work: warm session and check marker =="
aws bedrock-agentcore invoke-agent-runtime \
    --cli-binary-format raw-in-base64-out \
    --agent-runtime-arn "${ARN}" \
    --runtime-session-id "${SID}" \
    --payload "{\"action\": \"noop\", \"storage\": \"resumed\", \"storage_backend\": \"${STORAGE}\", \"session_epoch\": ${SESSION_EPOCH}, \"workspace\": \"${WS}\", \"harness\": \"${HARNESS}\"}" \
    --region "${SCH_REGION}" \
    /dev/null >/dev/null 2>&1 || true
wait_boot_ready 240
MARKER=$(remote_stdout "cat ${MARKER_FILE} 2>/dev/null")
if echo "${MARKER}" | grep -q "DONE-${MAGIC}"; then
    ok "checkpoint captured task marker: ${MARKER}"
else
    bad "task marker missing or wrong: '${MARKER}'"
fi

echo
echo "== 6. orphan reconciliation: stop runtime session without sch stop =="
ORPHAN_TASK=$("${SCH}" task "${WS}" --harness "${HARNESS}" "Run the shell command 'sleep 90' and then report that it finished.")
if [ -n "${ORPHAN_TASK}" ]; then
    ok "orphan task submitted: ${ORPHAN_TASK}"
else
    bad "orphan task submit failed"
fi
sleep 10
aws bedrock-agentcore stop-runtime-session \
    --agent-runtime-arn "${ARN}" \
    --runtime-session-id "${SID}" \
    --region "${SCH_REGION}" >/dev/null 2>&1 || true
sleep 15
# Warm a new microVM; shim boot should reconcile the orphan running status.
aws bedrock-agentcore invoke-agent-runtime \
    --cli-binary-format raw-in-base64-out \
    --agent-runtime-arn "${ARN}" \
    --runtime-session-id "${SID}" \
    --payload "{\"action\": \"noop\", \"storage\": \"resumed\", \"storage_backend\": \"${STORAGE}\", \"session_epoch\": ${SESSION_EPOCH}, \"workspace\": \"${WS}\", \"harness\": \"${HARNESS}\"}" \
    --region "${SCH_REGION}" \
    /dev/null >/dev/null 2>&1 || true
wait_boot_ready 240
ORPHAN_STATE=$(status_field "state")
if [ "${ORPHAN_STATE}" = "interrupted" ]; then
    ok "orphan reconciled to interrupted"
else
    bad "orphan state=${ORPHAN_STATE}, expected interrupted"
fi
# A fresh task should now be accepted freely.
FRESH_TASK=$("${SCH}" task "${WS}" --harness "${HARNESS}" "Run the shell command 'echo fresh-after-interrupt' and report the output.")
if [ -n "${FRESH_TASK}" ]; then
    ok "fresh task accepted after orphan reconciliation: ${FRESH_TASK}"
else
    bad "fresh task not accepted after orphan reconciliation"
fi
wait_terminal_for_task "${FRESH_TASK}" 120 >/dev/null 2>&1 || true

echo
echo "== 7. handoff: TUI brainstorm, stop, then task --continue (harness=${HARNESS}) =="
BRAINSTORM_MAGIC="brainstorm-${MAGIC}"
# Per-harness brainstorm seed: run the harness non-interactively with stdin
# redirected from /dev/null (same workaround as the headless task path, D7)
# to leave a session on disk that `sch task --continue` will resume.
# - opencode: `opencode run --title <t> "<prompt>"` writes to opencode.db
# - claude:   `claude -p "<prompt>"` writes a JSONL under ~/.claude/projects/
# - pi:       `pi -p --append-system-prompt <remote-interactive> "<prompt>"`
#             writes a version-3 JSONL under ~/.pi/agent/sessions/<cwd>/
# The wrapper dispatcher applies the ENV bridge; SCH_HARNESS is exported so
# the dispatcher selects the right branch regardless of $0.
if [ "${HARNESS}" = "claude" ]; then
    AGENT_STATE=$(remote_stdout "python3 -c \"import json; print(json.load(open(\\\"/home/sch/.claude/settings.json\\\")).get(\\\"agent\\\",\\\"\\\"))\" && test -f /home/sch/.claude/agents/remote-interactive.md && test -f /home/sch/.claude/agents/remote-auto.md && printf agents-present")
    if echo "${AGENT_STATE}" | grep -q remote-interactive && echo "${AGENT_STATE}" | grep -q agents-present; then
        ok "Claude interactive default and both agent templates are present"
    else
        bad "Claude agent seed/default missing: ${AGENT_STATE}"
    fi
    remote_stdout "cd ${REPO_DIR} && SCH_HARNESS=claude claude -p --agent remote-interactive \"Remember this keyword: ${BRAINSTORM_MAGIC}\" < /dev/null 2>&1 | tail -3"
elif [ "${HARNESS}" = "pi" ]; then
    PI_AGENT_STATE=$(remote_stdout "test -f /home/sch/.pi/agent/settings.json && test -f /home/sch/.pi/agent/roles/remote-interactive.md && test -f /home/sch/.pi/agent/roles/remote-auto.md && printf pi-seed-present")
    if echo "${PI_AGENT_STATE}" | grep -q pi-seed-present; then
        ok "Pi settings and both role prompts are present"
    else
        bad "Pi seed or role prompts missing: ${PI_AGENT_STATE}"
    fi
    remote_stdout "cd ${REPO_DIR} && SCH_HARNESS=pi pi -p --append-system-prompt /home/sch/.pi/agent/roles/remote-interactive.md \"Remember this keyword: ${BRAINSTORM_MAGIC}\" < /dev/null 2>&1 | tail -3"
    PI_SESSION_STATE=$(remote_stdout "python3 -c \"import json,pathlib; root=pathlib.Path(\\\"/home/sch/.pi/agent/sessions\\\"); matches=[p for p in root.rglob(\\\"*.jsonl\\\") if \\\"${BRAINSTORM_MAGIC}\\\" in p.read_text(encoding=\\\"utf-8\\\",errors=\\\"replace\\\")]; p=max(matches,key=lambda x:x.stat().st_mtime); h=json.loads(p.open(encoding=\\\"utf-8\\\").readline()); assert h.get(\\\"type\\\")==\\\"session\\\" and h.get(\\\"version\\\")==3 and h.get(\\\"cwd\\\")==\\\"${REPO_DIR}\\\" and h.get(\\\"id\\\"); print(h[\\\"id\\\"]+\\\" \\\"+str(p))\" 2>/dev/null")
    if [ -n "${PI_SESSION_STATE}" ]; then
        ok "Pi remote-interactive seed created a resumable session: ${PI_SESSION_STATE}"
    else
        bad "Pi remote-interactive seed did not create a valid resumable session"
    fi
else
    remote_stdout "cd ${REPO_DIR} && SCH_HARNESS=opencode opencode run --title ${BRAINSTORM_MAGIC} \"Remember this keyword: ${BRAINSTORM_MAGIC}\" < /dev/null 2>&1 | tail -3"
fi
"${SCH}" stop "${WS}" >/dev/null 2>&1 || true
sleep 15
aws bedrock-agentcore invoke-agent-runtime \
    --cli-binary-format raw-in-base64-out \
    --agent-runtime-arn "${ARN}" \
    --runtime-session-id "${SID}" \
    --payload "{\"action\": \"noop\", \"storage\": \"resumed\", \"storage_backend\": \"${STORAGE}\", \"session_epoch\": ${SESSION_EPOCH}, \"workspace\": \"${WS}\", \"harness\": \"${HARNESS}\"}" \
    --region "${SCH_REGION}" \
    /dev/null >/dev/null 2>&1 || true
wait_boot_ready 240
HANDOFF_TASK=$("${SCH}" task "${WS}" --harness "${HARNESS}" --continue "What keyword did I ask you to remember in the brainstorm session? Reply with only the keyword.")
if [ -n "${HANDOFF_TASK}" ]; then
    ok "handoff task submitted: ${HANDOFF_TASK}"
else
    bad "handoff task submit failed"
fi
wait_terminal_for_task "${HANDOFF_TASK}" 240 >/dev/null 2>&1
HANDOFF_STATE=$(status_field "state")
HANDOFF_OUTPUT=$(status_field "output")
if [ "${HANDOFF_STATE}" = "succeeded" ]; then
    ok "handoff task terminal state=succeeded"
else
    bad "handoff task state=${HANDOFF_STATE}, expected succeeded"
fi
if echo "${HANDOFF_OUTPUT}" | grep -q "${BRAINSTORM_MAGIC}"; then
    ok "handoff preserved brainstorm context"
else
    bad "handoff output does not contain remembered keyword: ${HANDOFF_OUTPUT}"
fi
HANDOFF_CHECKPOINT=$(status_field "checkpoint_status")
if [ "${HANDOFF_CHECKPOINT}" = "confirmed" ]; then
    ok "handoff terminal checkpoint confirmed"
else
    bad "handoff checkpoint status=${HANDOFF_CHECKPOINT}, expected confirmed"
fi

echo
echo "== 7b. cold-boot continue: stop, then task --continue with NO warmup (harness=${HARNESS}) =="
# The race fixed by TASK-27 (spec headless-task-execution R2): right after
# `sch stop`, `sch task --continue` lands on a cold microVM whose session-store
# restore is still in flight. Step 7 warms the VM and waits for boot-ready
# before submitting, so it never exercises that window. This step submits
# straight after the stop and asserts the task still resumes the pre-stop
# session (the shim resolves after the workspace-ready wait) and that the
# outcome is recorded (continue_resolved, harness_session_id) for `sch status`.
# The pre-stop session id is the one step 7 resumed (status of the last task).
PRE_STOP_SESSION=$(status_field "harness_session_id")
if [ -n "${PRE_STOP_SESSION}" ] && [ "${PRE_STOP_SESSION}" != "None" ]; then
    ok "pre-stop session id captured from status: ${PRE_STOP_SESSION}"
else
    bad "no harness_session_id in status after step 7 (got '${PRE_STOP_SESSION}')"
fi
"${SCH}" stop "${WS}" >/dev/null 2>&1 || true
sleep 15
COLD_STDERR="${TMPDIR:-/tmp}/sch-cold-stderr-$$.txt"
COLD_TASK=$("${SCH}" task "${WS}" --harness "${HARNESS}" --continue "What keyword did I ask you to remember in the brainstorm session? Reply with only the keyword." 2>"${COLD_STDERR}")
if [ -n "${COLD_TASK}" ]; then
    ok "cold-boot continue task submitted without warmup: ${COLD_TASK}"
else
    bad "cold-boot continue task submit failed"
fi
if grep -q "did not echo requested --continue" "${COLD_STDERR}"; then
    bad "runtime did not echo --continue (image predates the feature?)"
else
    ok "submit ack echoed --continue (no CLI warning)"
fi
rm -f "${COLD_STDERR}"
wait_terminal_for_task "${COLD_TASK}" 300 >/dev/null 2>&1
COLD_STATE=$(status_field "state")
COLD_RESOLVED=$(status_field "continue_resolved")
COLD_SESSION=$(status_field "harness_session_id")
COLD_OUTPUT=$(status_field "output")
if [ "${COLD_STATE}" = "succeeded" ]; then
    ok "cold-boot continue task terminal state=succeeded"
else
    bad "cold-boot continue task state=${COLD_STATE}, expected succeeded"
fi
if [ "${COLD_RESOLVED}" = "True" ]; then
    ok "continue_resolved=true recorded in status"
else
    bad "continue_resolved='${COLD_RESOLVED}', expected True"
fi
if [ -n "${COLD_SESSION}" ] && [ "${COLD_SESSION}" = "${PRE_STOP_SESSION}" ]; then
    ok "cold-boot continue resumed the pre-stop session: ${COLD_SESSION}"
else
    bad "session mismatch: pre-stop='${PRE_STOP_SESSION}' resumed='${COLD_SESSION}'"
fi
if echo "${COLD_OUTPUT}" | grep -q "${BRAINSTORM_MAGIC}"; then
    ok "cold-boot continue preserved brainstorm context"
else
    bad "cold-boot continue output does not contain remembered keyword: ${COLD_OUTPUT}"
fi

echo
echo "== 8. application timeout: --timeout 5 with a sleep prompt =="
TIMEOUT_TASK=$("${SCH}" task "${WS}" --harness "${HARNESS}" --timeout 5 "Run the shell command 'sleep 30' and then report that it finished.")
if [ -n "${TIMEOUT_TASK}" ]; then
    ok "timeout task submitted: ${TIMEOUT_TASK}"
else
    bad "timeout task submit failed"
fi
wait_terminal_for_task "${TIMEOUT_TASK}" 60 >/dev/null 2>&1
TIMEOUT_STATE=$(status_field "state")
TIMEOUT_EXIT=$(status_field "exit_code")
if [ "${TIMEOUT_STATE}" = "timed-out" ]; then
    ok "timeout task state=timed-out exit_code=${TIMEOUT_EXIT}"
else
    bad "timeout task state=${TIMEOUT_STATE}, expected timed-out"
fi
# Verify /ping returned Healthy: invoke info and inspect boot (no HealthyBusy).
if _invoke_info >/dev/null 2>&1; then
    ok "info invocation succeeded after timeout (HealthyBusy should be cleared)"
else
    bad "info invocation failed after timeout"
fi

echo
echo "== result: ${PASS} passed, ${FAIL} failed =="
[ "${FAIL}" -eq 0 ] || exit 1
