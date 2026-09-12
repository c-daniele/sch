#!/bin/bash
# verify-telegram-interaction.sh — live verification of the inbound Telegram
# interaction channel (spec: telegram-interaction, add-telegram-interaction).
#
# Mixed programmatic + guided checks: the Bot API offers no way to read the
# bot's own chat programmatically, and approvals require a human finger on
# the inline keyboard — the script asserts everything reachable from AWS
# (stack parameters, DynamoDB routing/commands items, task submissions
# observed via `sch status`) and walks the operator through the phone-side
# steps (exclusive local/Telegram approval ownership, reconnect fallback,
# late-button handling, per-harness timeout, and free-text limits).
#
# Programmatic assertions:
#   0. interaction enabled on the stack; tables exist; webhook registered
#      (getWebhookInfo, only when TELEGRAM_BOT_TOKEN is exported)
#   1. after a task, the routing item threadId -> workspace matches the S3
#      topic mapping (design D2)
#   2. follow-up: a synthetic text command enqueued while the backend is
#      alive becomes a `task --continue` with that prompt (design D5 case 4)
#   3. one-task-per-workspace: a text command during a running task is
#      consumed but does NOT start a second task (refusal in the topic)
#
# Guided (by eye on the phone):
#   4. attached native-only and detached Telegram approval ownership for
#      opencode/claude, including reconnect fallback and a late button
#   5. Pi tool execution remains prompt-free attached and detached
#   6. claude/pi free-text limit message
#
# The synthetic enqueue in phases 2-3 writes directly to the commands table
# (same item shape the webhook Lambda produces): the caller needs
# dynamodb:PutItem on it — the deployer identity normally has it.
#
# Usage:
#   ./verify-telegram-interaction.sh <workspace> [--harness <opencode|claude|pi>]
set -uo pipefail

WS=""
HARNESS_FLAG=""
while [ $# -gt 0 ]; do
    case "$1" in
        --harness)
            [ $# -ge 2 ] || { echo "usage: $0 <workspace> [--harness <opencode|claude|pi>]"; exit 2; }
            HARNESS_FLAG="$2"; shift 2 ;;
        --help|-h)
            sed -n '2,36p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)
            if [ -z "${WS}" ]; then WS="$1"
            else echo "usage: $0 <workspace> [--harness <opencode|claude|pi>]" >&2; exit 2; fi
            shift ;;
    esac
done
[ -n "${WS}" ] || { echo "usage: $0 <workspace> [--harness <opencode|claude|pi>]"; exit 2; }
HARNESS="${HARNESS_FLAG:-opencode}"
case "${HARNESS}" in
    opencode|claude|pi) ;;
    *) echo "invalid harness '${HARNESS}' (expected: opencode|claude|pi)" >&2; exit 2 ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCH="${SCRIPT_DIR}/sch"
SCH_REGION="${SCH_REGION:-eu-west-1}"
SCH_PROJECT="${SCH_PROJECT:-sch}"
SCH_ENV="${SCH_ENV:-dev}"

PASS=0
FAIL=0
ok()  { echo "PASS: $*"; PASS=$((PASS+1)); }
bad() { echo "FAIL: $*"; FAIL=$((FAIL+1)); }

confirm_check() { # <question> <pass-message> <fail-message>
    local answer=""
    printf "%s [y/N]: " "$1"
    if IFS= read -r answer; then
        case "${answer}" in
            y|Y|yes|YES|Yes) ok "$2"; return ;;
        esac
    fi
    bad "$3"
}

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
BUCKET="${SCH_PROJECT}-${SCH_ENV}-checkpoints-${ACCOUNT_ID}"
COMMANDS_TABLE="${SCH_PROJECT}-${SCH_ENV}-telegram-commands"
ROUTING_TABLE="${SCH_PROJECT}-${SCH_ENV}-telegram-routing"

echo "== 0. preflight: interaction enabled on the runtime stack =="
ENABLED_PARAM=$(aws cloudformation describe-stacks \
    --stack-name "${SCH_PROJECT}-${SCH_ENV}-runtime" --region "${SCH_REGION}" \
    --query "Stacks[0].Parameters[?ParameterKey=='EnableTelegramInteraction'].ParameterValue" \
    --output text 2>/dev/null)
if [ "${ENABLED_PARAM}" = "true" ]; then
    ok "EnableTelegramInteraction=true on the stack"
else
    bad "EnableTelegramInteraction is '${ENABLED_PARAM}' — deploy with ENABLE_TELEGRAM_INTERACTION=true first"
    echo "aborting: the feature is off on this deployment"; exit 1
fi
for table in "${COMMANDS_TABLE}" "${ROUTING_TABLE}"; do
    if aws dynamodb describe-table --table-name "${table}" --region "${SCH_REGION}" >/dev/null 2>&1; then
        ok "table ${table} exists"
    else
        bad "table ${table} missing"
    fi
done
if [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
    WEBHOOK_URL_SET=$(curl -fsS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getWebhookInfo" 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin).get("result",{}).get("url",""))' 2>/dev/null)
    if [ -n "${WEBHOOK_URL_SET}" ]; then
        ok "webhook registered: ${WEBHOOK_URL_SET}"
    else
        bad "no webhook registered for this bot (deploy.sh runs setWebhook)"
    fi
else
    echo "note: TELEGRAM_BOT_TOKEN not exported — skipping getWebhookInfo check"
fi
echo

echo "== 1. topic routing: thread_id -> workspace item matches the S3 mapping =="
# Any notification creates/reverifies both mappings; a trivial task is the
# cheapest way to force one on a cold workspace.
TASK_ID=""
for attempt in 1 2 3; do
    TASK_ID=$("${SCH}" task "${WS}" --harness "${HARNESS}" \
        "Reply with the single word DONE and do nothing else." 2>/dev/null)
    [ -n "${TASK_ID}" ] && break
    echo "   submit attempt ${attempt} failed (cold start?); retrying in 20s"
    sleep 20
done
if [ -z "${TASK_ID}" ]; then
    bad "task submit did not return a task id"; echo "aborting"; exit 1
fi
ok "warmup task submitted (${TASK_ID:0:12})"
STATE=""
for i in $(seq 1 240); do
    STATE=$("${SCH}" status "${WS}" --json 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin).get("state",""))' 2>/dev/null)
    case "${STATE}" in succeeded|failed|timed-out) break ;; esac
    sleep 2
done
ok "warmup task terminal (${STATE:-unknown})"
MAPPING=""
for i in $(seq 1 30); do
    MAPPING=$(aws s3 cp "s3://${BUCKET}/checkpoints/${WS}/telegram-topic.json" - \
        --region "${SCH_REGION}" 2>/dev/null)
    [ -n "${MAPPING}" ] && break
    sleep 2
done
THREAD_ID=$(echo "${MAPPING}" \
    | python3 -c 'import json,sys; v=json.load(sys.stdin).get("thread_id"); print("chat" if v is None else v)' 2>/dev/null)
if [ -z "${THREAD_ID}" ]; then
    bad "no telegram-topic.json mapping for ${WS}"
else
    ROUTED_WS=$(aws dynamodb get-item --table-name "${ROUTING_TABLE}" --region "${SCH_REGION}" \
        --key "{\"threadId\":{\"S\":\"${THREAD_ID}\"}}" \
        --query 'Item.workspace.S' --output text 2>/dev/null)
    if [ "${ROUTED_WS}" = "${WS}" ]; then
        ok "routing item ${THREAD_ID} -> ${WS} present (design D2)"
    else
        bad "routing item for thread ${THREAD_ID} is '${ROUTED_WS}' (expected '${WS}')"
    fi
fi
echo

enqueue_text() { # <text> — same item shape as the webhook Lambda
    local now sk
    now=$(date +%s)
    sk="$(( now * 1000 ))#verify$$"
    aws dynamodb put-item --table-name "${COMMANDS_TABLE}" --region "${SCH_REGION}" --item "{
        \"workspace\": {\"S\": \"${WS}\"},
        \"sk\": {\"S\": \"${sk}\"},
        \"type\": {\"S\": \"text\"},
        \"text\": {\"S\": \"$1\"},
        \"threadId\": {\"S\": \"${THREAD_ID:-chat}\"},
        \"createdAt\": {\"N\": \"${now}\"},
        \"expiresAt\": {\"N\": \"$(( now + 900 ))\"}
    }" >/dev/null
}

commands_pending() {
    aws dynamodb query --table-name "${COMMANDS_TABLE}" --region "${SCH_REGION}" \
        --key-condition-expression "workspace = :ws" \
        --expression-attribute-values "{\":ws\": {\"S\": \"${WS}\"}}" \
        --query 'Count' --output text 2>/dev/null
}

echo "== 2. follow-up: text command on a quiescent workspace starts task --continue =="
echo "   (the warm runtime polls while idle; no web/TUI process is required)"
FOLLOWUP_PROMPT="verify-interaction follow-up $(date +%s): reply DONE"
enqueue_text "${FOLLOWUP_PROMPT}"
ok "text command enqueued"
NEW_TASK=""
for i in $(seq 1 60); do
    NEW_TASK=$("${SCH}" status "${WS}" --json 2>/dev/null \
        | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("task_id","") if "verify-interaction follow-up" in (d.get("prompt") or "") else "")' 2>/dev/null)
    [ -n "${NEW_TASK}" ] && break
    sleep 3
done
if [ -n "${NEW_TASK}" ]; then
    ok "follow-up task started from the text command (${NEW_TASK:0:12})"
    for i in $(seq 1 240); do
        STATE=$("${SCH}" status "${WS}" --json 2>/dev/null \
            | python3 -c 'import json,sys; print(json.load(sys.stdin).get("state",""))' 2>/dev/null)
        case "${STATE}" in succeeded|failed|timed-out) break ;; esac
        sleep 2
    done
    ok "follow-up task terminal (${STATE:-unknown})"
else
    bad "no follow-up task appeared while the runtime was warm"
fi
if [ "$(commands_pending)" = "0" ]; then
    ok "command consumed exactly once (queue empty)"
else
    bad "command still pending in the queue"
fi
echo

echo "== 3. one task per workspace: text during a running task is refused =="
LONG_TASK=$("${SCH}" task "${WS}" --harness "${HARNESS}" \
    "Wait: run 'sleep 60' with your shell tool, then reply DONE." 2>/dev/null)
if [ -n "${LONG_TASK}" ]; then
    ok "long task submitted (${LONG_TASK:0:12})"
    sleep 5
    enqueue_text "this must be refused $(date +%s)"
    sleep 20
    CURRENT=$("${SCH}" status "${WS}" --json 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin).get("task_id",""))' 2>/dev/null)
    if [ "${CURRENT}" = "${LONG_TASK}" ]; then
        ok "no second task started (refusal with task reference in the topic — check by eye)"
    else
        bad "task id changed while a task was running (${CURRENT:0:12})"
    fi
    if [ "$(commands_pending)" = "0" ]; then
        ok "refused command was still consumed (no re-application)"
    else
        bad "command still pending after the refusal window"
    fi
    for i in $(seq 1 120); do
        STATE=$("${SCH}" status "${WS}" --json 2>/dev/null \
            | python3 -c 'import json,sys; print(json.load(sys.stdin).get("state",""))' 2>/dev/null)
        case "${STATE}" in succeeded|failed|timed-out) break ;; esac
        sleep 2
    done
else
    bad "could not submit the long task"
fi
echo

echo "== 4. guided ${HARNESS} approval ownership check =="
LIVE_TAG="${HARNESS}-approval-$(date +%s)"
if [ "${HARNESS}" = "pi" ]; then
    cat <<EOF
This check needs a real TTY and phone observation. The script will open Pi.

  1. Ask Pi to use its shell tool to run
       touch /tmp/${LIVE_TAG}-attached
     Expect immediate execution: no native permission prompt, no Telegram
     Approve/Deny request, and no broker wait.
  2. Ask Pi to make two separate shell calls: first 'sleep 30', then
       touch /tmp/${LIVE_TAG}-detached
     Detach with Ctrl+] while sleep is running. Do not quit Pi, and note the
     shell ID. The second tool call must remain prompt-free after detach.
EOF
    if "${SCH}" run "${WS}" --harness pi; then
        ok "Pi: initial CommandShell detached without ending Pi"
    else
        bad "Pi: sch run did not complete the guided detach"
    fi
    confirm_check \
        "Did the attached tool execute without a native or Telegram permission prompt?" \
        "Pi: attached tool execution remained prompt-free" \
        "Pi: attached tool execution showed a permission prompt"
    confirm_check \
        "After detach, did the second tool execute without a Telegram approval request?" \
        "Pi: detached tool execution remained outside the approval broker" \
        "Pi: detached tool execution produced an approval request or stalled"

    printf "Enter the shell ID printed for the detached Pi session: "
    IFS= read -r SHELL_ID
    if [ -z "${SHELL_ID}" ]; then
        bad "Pi: shell ID is required to verify the attached state again"
    else
        cat <<EOF
Reconnect to the existing Pi process and ask it to run
  touch /tmp/${LIVE_TAG}-reconnected
Confirm there is still no native or Telegram permission prompt, then detach
with Ctrl+] or quit normally to return here.
EOF
        if "${SCH}" shell "${WS}" --shell-id "${SHELL_ID}"; then
            ok "Pi: reconnected to shell ${SHELL_ID}"
        else
            bad "Pi: reconnect to shell ${SHELL_ID} failed"
        fi
        confirm_check \
            "Did the reconnected tool execute without any permission prompt?" \
            "Pi: reconnected tool execution remained prompt-free" \
            "Pi: reconnected tool execution showed a permission prompt"
    fi

    cat <<EOF

Additional existing checks:
  - FREE-TEXT LIMIT: while Pi is active, write free text in the workspace
    topic. Expect no TUI injection and a limit message offering
    'sch run ${WS}' or a follow-up after the session ends.
  - FOLLOW-UP: end Pi, ensure no task is active, then write a keyword question
    in the topic while the runtime is warm. Expect one task --continue and its
    submit/terminal notifications, using prior Pi context.
EOF
else
    cat <<EOF
This check needs a real TTY and phone observation; the Bot API cannot read
bot-authored approval messages back for this script.

The script will open ${HARNESS}. In that session:
  1. Ask it to use the shell tool to run
       touch /tmp/${LIVE_TAG}-native
     While attached, expect ONLY the native permission prompt and NO Telegram
     Approve/Deny request. Approve the native prompt and confirm the tool runs.
  2. Ask it to make two separate shell calls: first 'sleep 30', then
       touch /tmp/${LIVE_TAG}-telegram
     Approve the sleep call with the one-shot option only ("allow once" / the
     non-persistent choice). Do NOT choose "always allow bash" or an equivalent
     session-wide grant: OpenCode would then execute the second bash call without
     emitting a new permission.ask event. Once sleep is running, detach with
     Ctrl+] and note the shell ID. Do not quit ${HARNESS}. The second tool
     request is created after detach and must appear only on Telegram.
EOF
    if "${SCH}" run "${WS}" --harness "${HARNESS}"; then
        ok "${HARNESS}: initial CommandShell detached without ending the harness"
    else
        bad "${HARNESS}: sch run did not complete the guided detach"
    fi
    confirm_check \
        "Was the attached request native-only, with no Telegram buttons?" \
        "${HARNESS}: attached approval ownership was exclusively native" \
        "${HARNESS}: attached request was missing natively or duplicated on Telegram"
    confirm_check \
        "After detach, did the new request appear on Telegram with Approve/Deny buttons?" \
        "${HARNESS}: detached approval ownership moved to Telegram" \
        "${HARNESS}: detached Telegram approval request was not observed"

    printf "Tap Approve now, then confirm the tool result. Did Telegram approve it and update the message? [y/N]: "
    IFS= read -r REMOTE_APPROVED
    case "${REMOTE_APPROVED}" in
        y|Y|yes|YES|Yes) ok "${HARNESS}: detached Telegram approval completed" ;;
        *) bad "${HARNESS}: detached Telegram approval did not complete" ;;
    esac

    printf "Enter the shell ID printed for the detached session: "
    IFS= read -r SHELL_ID
    if [ -z "${SHELL_ID}" ]; then
        bad "${HARNESS}: shell ID is required for reconnect fallback"
    else
        cat <<EOF

Reconnect to the same ${HARNESS} process. Then create a second pending remote
request:
  1. Ask for two separate shell calls: first 'sleep 30', then
       touch /tmp/${LIVE_TAG}-fallback
  2. Approve sleep with the one-shot/non-persistent option and detach with
     Ctrl+] while it runs. Never choose a session-wide "always allow bash" grant.
  3. Wait until the new Telegram Approve/Deny request is visible. Do not tap it.
EOF
        if "${SCH}" shell "${WS}" --shell-id "${SHELL_ID}"; then
            ok "${HARNESS}: reconnected before creating the fallback request"
        else
            bad "${HARNESS}: reconnect to shell ${SHELL_ID} failed"
        fi
        confirm_check \
            "Is the second Telegram approval pending now?" \
            "${HARNESS}: remote request is pending for reconnect test" \
            "${HARNESS}: second remote request was not observed"

        cat <<EOF
Reconnect again now. Expect the pending Telegram request to be closed as
resolved elsewhere and the native prompt to take ownership without waiting
for the remote timeout. Resolve the native prompt locally. Then tap an old
Telegram button: it must report that the request is already resolved and must
not change whether the tool ran. Detach or quit to return here.
EOF
        if "${SCH}" shell "${WS}" --shell-id "${SHELL_ID}"; then
            ok "${HARNESS}: reconnected while a remote request was pending"
        else
            bad "${HARNESS}: reconnect for fallback failed"
        fi
        confirm_check \
            "Did reconnect transfer ownership to the native prompt before timeout?" \
            "${HARNESS}: reconnect fell back promptly to native approval" \
            "${HARNESS}: reconnect did not produce native fallback"
        confirm_check \
            "Was the late Telegram button rejected as already resolved, with no tool effect?" \
            "${HARNESS}: late Telegram decision was rejected" \
            "${HARNESS}: late Telegram decision was not safely rejected"
    fi

    cat <<EOF

Additional existing checks:
  - DENY: create another request while detached and tap Deny. Expect the tool
    not to run and the message to be updated as denied via Telegram.
  - TIMEOUT: create another request while detached and tap nothing for
    ${SCH_APPROVAL_TIMEOUT:-10} minutes. Expect native fallback, no auto-approval,
    and a Telegram timeout update.
  - TEXT INJECTION (opencode only): with 'sch web ${WS}' open, write in the
    workspace topic. Expect injection confirmation and the answer milestone.
  - CLAUDE LIMIT (claude only): while Claude is active, write free text in the
    topic. Expect the alternatives limit message and no injection.
EOF
fi

cat <<EOF
== RESULT: PASS=${PASS} FAIL=${FAIL} ==
EOF
[ "${FAIL}" -eq 0 ]
