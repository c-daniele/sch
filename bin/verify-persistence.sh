#!/bin/bash
# verify-persistence.sh — end-to-end verification of workspace persistence
# across stop/resume cycles (spec: workspace-persistence).
#
# Flow:
#   setup   : create known state in the workspace microVM
#             - sentinel file (uncommitted) in /mnt/workspace/repo
#             - an OpenCode session with a message (via `opencode run`)
#             - marker in the OpenCode user config
#   stop    : stop the runtime session (sch stop)
#   verify  : resume the session (new microVM) and assert:
#             - sentinel file present with identical content (worktree integrity)
#             - git sees the uncommitted change
#             - OpenCode session is listed again (state ritrovato)
#             - user config marker preserved
#
# Usage:
#   ./verify-persistence.sh <workspace>            # full cycle: setup -> stop -> verify
#   ./verify-persistence.sh <workspace> setup      # only create state (for idle-timeout test)
#   ./verify-persistence.sh <workspace> verify     # only verify (after idle timeout / manual stop)
#
# For the idle-timeout variant (task 5.3): run `setup`, wait > idle timeout
# (default 900s) WITHOUT touching the session, then run `verify`.
set -uo pipefail

WS="${1:-}"
PHASE="${2:-full}"
[ -n "${WS}" ] || { echo "usage: $0 <workspace> [setup|verify|full]"; exit 2; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCH="${SCRIPT_DIR}/sch"
SCH_REGION="${SCH_REGION:-eu-west-1}"
REQUESTED_STORAGE="${SCH_VERIFY_STORAGE:-s3}"
STATE_FILE="${TMPDIR:-/tmp}/sch-verify-${WS}.state"

PASS=0
FAIL=0
ok()   { echo "PASS: $*"; PASS=$((PASS+1)); }
bad()  { echo "FAIL: $*"; FAIL=$((FAIL+1)); }

# --- remote one-shot helper -----------------------------------------------------
# Note the quoting: `agentcore exec` joins argv with spaces and evaluates the
# result in a remote shell, so the command is wrapped as sh -c "'...'" and must
# not contain single quotes.
runtime_arn() {
    aws cloudformation describe-stacks \
        --stack-name "${SCH_PROJECT:-sch}-${SCH_ENV:-dev}-runtime" \
        --region "${SCH_REGION}" \
        --query "Stacks[0].Outputs[?OutputKey=='RuntimeArn'].OutputValue" \
        --output text
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

wait_ready() { # [storage-hint: fresh|resumed]
    # The session-storage restore is asynchronous w.r.t. microVM boot: poll
    # the shim until the bootstrap reports phase=ready before touching the
    # workspace (the invoke itself triggers provisioning/resume). The hint
    # tells the shim how long to wait for restored content.
    local hint="${1:-}"
    local payload="{\"action\": \"info\", \"workspace\": \"${WS}\", \"storage_backend\": \"${STORAGE}\", \"session_epoch\": ${SESSION_EPOCH}}"
    [ -n "${hint}" ] && payload="{\"action\": \"info\", \"storage\": \"${hint}\", \"workspace\": \"${WS}\", \"storage_backend\": \"${STORAGE}\", \"session_epoch\": ${SESSION_EPOCH}}"
    echo "-- waiting for microVM boot + storage restore (hint: ${hint:-none}, max 360s)"
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
            echo "   microVM ready ($(python3 -c "import json; b=json.load(open('${out}')).get('boot',{}); print('mount=%s restore=%s' % (b.get('mount'), b.get('restore_result')))" 2>/dev/null))"
            rm -f "${out}"
            return 0
        fi
        sleep 5
    done
    rm -f "${out}"
    echo "   WARNING: shim not ready after 360s, proceeding anyway"
    return 0
}

ARN=$(runtime_arn) || { echo "cannot resolve runtime ARN"; exit 1; }
# Same workspace -> session mapping used by sch (create it if new).
WS_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/sch/workspaces/${WS}"
if [ ! -f "${WS_FILE}" ]; then
    mkdir -p "$(dirname "${WS_FILE}")"
    python3 -c 'import json,sys,uuid; print(json.dumps({"runtimeSessionId":f"sch-{sys.argv[1]}-{uuid.uuid4()}","harness":"opencode","storage":sys.argv[2],"sessionEpoch":1}))' "${WS}" "${REQUESTED_STORAGE}" > "${WS_FILE}"
fi
SID=$(python3 -c 'import json,sys
try:
 d=json.load(open(sys.argv[1])); print(d.get("runtimeSessionId") or d.get("sessionId") or "")
except Exception: print(open(sys.argv[1]).read().strip())' "${WS_FILE}")
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
STATE_DIR="${WORKSPACE_ROOT}/state"
echo "workspace=${WS} session=${SID} storage=${STORAGE} root=${WORKSPACE_ROOT}"
echo

do_setup() {
    local magic="sch-magic-$(uuidgen | tr '[:upper:]' '[:lower:]' | cut -c1-8)"
    echo "== setup: creating known state (magic=${magic}) =="
    wait_ready

    echo "-- sentinel file + uncommitted git change"
    remote_stdout "cd ${REPO_DIR} && echo ${magic} > sentinel.txt && git add sentinel.txt && echo uncommitted-${magic} >> sentinel.txt && git status --porcelain && cat sentinel.txt"

    echo "-- config marker (fetch, modify locally, push via base64)"
    local cfg new_b64
    cfg=$(remote_stdout "cat ${STATE_DIR}/config/opencode/opencode.json 2>/dev/null")
    [ -n "${cfg}" ] || cfg='{}'
    # 'username' is a schema-valid opencode.json key (arbitrary keys are
    # rejected by OpenCode's strict config validation).
    new_b64=$(echo "${cfg}" | python3 -c "import json,sys; d=json.load(sys.stdin); d['username']='${magic}'; print(json.dumps(d,indent=2))" | base64 | tr -d '\n')
    remote_stdout "echo ${new_b64} | base64 -d > ${STATE_DIR}/config/opencode/opencode.json && grep -o ${magic} ${STATE_DIR}/config/opencode/opencode.json"

    echo "-- OpenCode session with a message (invokes Bedrock via execution role)"
    # `< /dev/null` is required (task 6.6 regression, poc-aws-mcp-and-bedrock-
    # provider): with both aws-docs and aws-mcp MCP servers enabled, a
    # foreground `agentcore exec ... opencode run` with no stdin redirection
    # hangs indefinitely (client-side timeout, remote process orphaned but
    # never exits) — a stdin/pty fd-inheritance interaction across opencode's
    # two local MCP subprocesses, reproducible and isolated empirically:
    # backgrounding the same command, or enabling only one MCP server, both
    # complete in seconds. Forcing stdin to /dev/null avoids the hang. Does
    # NOT affect the interactive TUI path (`sch shell` uses a real `--it` pty).
    remote_stdout "cd ${REPO_DIR} && opencode run --title sch-verify-${magic} \"Reply with exactly: ${magic}\" < /dev/null 2>&1 | tail -3"

    echo "-- recording session list"
    remote_stdout "cd ${REPO_DIR} && opencode session list 2>/dev/null | head -20"

    echo "${magic}" > "${STATE_FILE}"
    echo
    echo "setup done; magic saved to ${STATE_FILE}"
}

do_stop() {
    echo
    echo "== stop: stopping the runtime session =="
    "${SCH}" stop "${WS}"
    echo "waiting 15s before resume..."
    sleep 15
}

do_verify() {
    [ -f "${STATE_FILE}" ] || { echo "no state file ${STATE_FILE} — run setup first"; exit 1; }
    local magic
    magic=$(cat "${STATE_FILE}")
    echo
    echo "== verify: resuming session and asserting persisted state (magic=${magic}) =="
    wait_ready resumed

    local sentinel
    sentinel=$(remote_stdout "cat ${REPO_DIR}/sentinel.txt 2>/dev/null")
    if echo "${sentinel}" | grep -q "^${magic}$" && echo "${sentinel}" | grep -q "uncommitted-${magic}"; then
        ok "worktree: sentinel file present with identical content (incl. uncommitted change)"
    else
        bad "worktree: sentinel missing or content changed: '${sentinel}'"
    fi

    local gitstat
    gitstat=$(remote_stdout "cd ${REPO_DIR} && git status --porcelain 2>/dev/null")
    if echo "${gitstat}" | grep -q "sentinel.txt"; then
        ok "git: uncommitted change still visible (${gitstat})"
    else
        bad "git: uncommitted change lost (status: '${gitstat}')"
    fi

    local sessions
    sessions=$(remote_stdout "cd ${REPO_DIR} && opencode session list 2>/dev/null | head -20")
    local statehit
    statehit=$(remote_stdout "grep -rla ${magic} ${STATE_DIR}/data 2>/dev/null | head -5")
    if echo "${sessions}" | grep -q "${magic}" || [ -n "${statehit}" ]; then
        ok "opencode: session state with magic found after resume (list or state files)"
    else
        bad "opencode: no trace of session ${magic} after resume. Sessions: ${sessions}"
    fi

    local marker
    marker=$(remote_stdout "grep -o ${magic} ${STATE_DIR}/config/opencode/opencode.json 2>/dev/null")
    if [ "${marker}" = "${magic}" ]; then
        ok "config: user marker preserved"
    else
        bad "config: marker missing from opencode.json"
    fi

    echo
    echo "== result: ${PASS} passed, ${FAIL} failed =="
    [ "${FAIL}" -eq 0 ] || exit 1
}

case "${PHASE}" in
    setup)  do_setup ;;
    verify) do_verify ;;
    full)   do_setup; do_stop; do_verify ;;
    *) echo "unknown phase '${PHASE}' (setup|verify|full)"; exit 2 ;;
esac
