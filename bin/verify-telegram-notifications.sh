#!/bin/bash
# verify-telegram-notifications.sh — live verification of the Telegram push
# notification channel (spec: telegram-notifications, add-telegram-notifications).
#
# Programmatic assertions (no Telegram read API exists for a bot's own
# messages, so the chat content itself is checked by eye — task 6.5):
#   1. runtime deployed with the Telegram env config (via the stack parameters)
#   2. a real headless task in workspace A reaches a terminal state and the
#      notifier persists checkpoints/<A>/telegram-topic.json (proof that the
#      submit/terminal notifications were routed to a per-workspace topic)
#   3. a second workspace B does the same and its topic mapping differs from
#      A's (spec: 'Sessioni concorrenti non si mescolano')
#   4. the mapping object never contains the bot token
#
# On the phone you should see, in two distinct topics (or with two distinct
# [workspace] prefixes when the chat has no Topics): the submit notification
# and the terminal notification of each task. The final guided check exercises
# attached, detached, and reconnected delivery for the selected harness.
#
# Usage:
#   ./verify-telegram-notifications.sh <workspace-a> <workspace-b> [--harness <opencode|claude|pi>]
set -uo pipefail

WS_A=""
WS_B=""
HARNESS_FLAG=""
while [ $# -gt 0 ]; do
    case "$1" in
        --harness)
            [ $# -ge 2 ] || { echo "usage: $0 <workspace-a> <workspace-b> [--harness <opencode|claude|pi>]"; exit 2; }
            HARNESS_FLAG="$2"; shift 2 ;;
        --help|-h)
            sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)
            if [ -z "${WS_A}" ]; then WS_A="$1"
            elif [ -z "${WS_B}" ]; then WS_B="$1"
            else echo "usage: $0 <workspace-a> <workspace-b> [--harness <opencode|claude|pi>]" >&2; exit 2; fi
            shift ;;
    esac
done
[ -n "${WS_A}" ] && [ -n "${WS_B}" ] || { echo "usage: $0 <workspace-a> <workspace-b> [--harness <opencode|claude|pi>]"; exit 2; }
HARNESS="${HARNESS_FLAG:-claude}"
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

echo "== 0. preflight: Telegram parameters present on the runtime stack =="
CHAT_ID_PARAM=$(aws cloudformation describe-stacks \
    --stack-name "${SCH_PROJECT}-${SCH_ENV}-runtime" --region "${SCH_REGION}" \
    --query "Stacks[0].Parameters[?ParameterKey=='TelegramChatId'].ParameterValue" \
    --output text 2>/dev/null)
if [ -n "${CHAT_ID_PARAM}" ] && [ "${CHAT_ID_PARAM}" != "None" ]; then
    ok "TelegramChatId parameter set on the stack (${CHAT_ID_PARAM})"
else
    bad "TelegramChatId not set — deploy with TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID first"
    echo "aborting: the feature is off on this deployment"; exit 1
fi
echo

topic_object() { # <workspace> -> JSON of telegram-topic.json or empty
    aws s3 cp "s3://${BUCKET}/checkpoints/$1/telegram-topic.json" - \
        --region "${SCH_REGION}" 2>/dev/null
}

run_task_and_check_topic() { # <workspace> -> sets THREAD_ID
    local ws="$1"
    THREAD_ID=""
    echo "-- workspace ${ws}: submit + wait terminal"
    # A runtime version bump (any env/image change) means the first invocation
    # hits a cold microVM: `sch` reports "runtime warmup invocation failed"
    # rather than blocking. Retry a few times before calling it a failure.
    local task_id="" attempt
    for attempt in 1 2 3; do
        task_id=$("${SCH}" task "${ws}" --harness "${HARNESS}" \
            "Reply with the single word DONE and do nothing else." 2>/dev/null)
        [ -n "${task_id}" ] && break
        echo "   submit attempt ${attempt} failed (cold start?); retrying in 20s"
        sleep 20
    done
    if [ -z "${task_id}" ]; then
        bad "${ws}: task submit did not return a task id after 3 attempts"
        return 1
    fi
    ok "${ws}: task submitted (${task_id:0:12})"
    local i st=""
    for i in $(seq 1 240); do
        st=$("${SCH}" status "${ws}" --json 2>/dev/null \
            | python3 -c 'import json,sys; print(json.load(sys.stdin).get("state",""))' 2>/dev/null)
        case "${st}" in succeeded|failed|timed-out) break ;; esac
        sleep 2
    done
    case "${st}" in
        succeeded) ok "${ws}: task terminal (${st})" ;;
        failed|timed-out) ok "${ws}: task terminal (${st}) — terminal notification still due" ;;
        *) bad "${ws}: task never reached a terminal state"; return 1 ;;
    esac
    # The terminal notification is enqueued right after the terminal status
    # upload; give the notifier thread a moment to send + persist the mapping.
    local mapping=""
    for i in $(seq 1 30); do
        mapping=$(topic_object "${ws}")
        [ -n "${mapping}" ] && break
        sleep 2
    done
    if [ -z "${mapping}" ]; then
        bad "${ws}: telegram-topic.json never appeared in the checkpoint prefix"
        return 1
    fi
    ok "${ws}: topic mapping persisted (${mapping})"
    if echo "${mapping}" | grep -qi "token"; then
        bad "${ws}: mapping object mentions a token"
    else
        ok "${ws}: mapping object carries no token material"
    fi
    # Global, NOT stdout: capturing this function's output in a $( ) would run
    # it in a subshell, losing every ok()/bad() increment (a false green).
    THREAD_ID=$(echo "${mapping}" \
        | python3 -c 'import json,sys; print(json.load(sys.stdin).get("thread_id"))')
}

echo "== 1. workspace A: task lifecycle notified in its own topic =="
run_task_and_check_topic "${WS_A}"
THREAD_A="${THREAD_ID}"
echo
echo "== 2. workspace B: parallel workspace gets its own topic =="
run_task_and_check_topic "${WS_B}"
THREAD_B="${THREAD_ID}"
echo

echo "== 3. topics are distinct (no message mixing) =="
if [ -n "${THREAD_A}" ] && [ -n "${THREAD_B}" ]; then
    if [ "${THREAD_A}" = "None" ] && [ "${THREAD_B}" = "None" ]; then
        ok "chat without Topics: both fell back to [workspace] prefixes (check the chat by eye)"
    elif [ "${THREAD_A}" != "${THREAD_B}" ]; then
        ok "distinct topics: ${WS_A}=${THREAD_A} ${WS_B}=${THREAD_B}"
    else
        bad "both workspaces mapped to the same thread_id ${THREAD_A}"
    fi
else
    bad "missing thread ids (A='${THREAD_A}' B='${THREAD_B}')"
fi
echo

echo "== 4. guided ${HARNESS} attached/detached/reconnected check =="
LIVE_TAG="${HARNESS}-presence-$(date +%s)"
cat <<EOF
This check needs a real TTY and phone observation; bot-authored Telegram
messages cannot be read back through the Bot API.

The script will open ${HARNESS} in ${WS_A}. In that session:
  1. Ask: Reply exactly ATTACHED-${LIVE_TAG} and do nothing else.
  2. Wait for the reply while still attached. It must NOT appear on Telegram.
  3. Ask: Use the shell tool to run 'sleep 30'. After it completes, reply
     exactly DETACHED-${LIVE_TAG} and do nothing else.
  4. If ${HARNESS} shows a native approval for sleep, approve it locally.
     Once sleep is running, detach immediately with Ctrl+]. Do not quit the
     harness. Note the shell ID printed by AgentCore.
EOF
if "${SCH}" run "${WS_A}" --harness "${HARNESS}"; then
    ok "${HARNESS}: initial CommandShell detached without ending the harness"
else
    bad "${HARNESS}: sch run did not complete the guided detach"
fi

confirm_check \
    "While attached, was ATTACHED-${LIVE_TAG} absent from Telegram?" \
    "${HARNESS}: attached milestone was suppressed" \
    "${HARNESS}: attached milestone appeared on Telegram"
confirm_check \
    "After detach, did DETACHED-${LIVE_TAG} arrive on Telegram?" \
    "${HARNESS}: post-detach milestone was delivered" \
    "${HARNESS}: post-detach milestone was not observed"
confirm_check \
    "After that delivery, was ATTACHED-${LIVE_TAG} still absent?" \
    "${HARNESS}: suppressed attached milestone was not replayed" \
    "${HARNESS}: attached milestone replayed after detach"

printf "Enter the shell ID printed for the detached session: "
IFS= read -r SHELL_ID
if [ -z "${SHELL_ID}" ]; then
    bad "${HARNESS}: shell ID is required to verify reconnect"
else
    cat <<EOF

Reconnect to the same shell. Confirm the existing ${HARNESS} process is
still open, then ask: Reply exactly RECONNECTED-${LIVE_TAG} and do nothing
else. Wait for the local reply, verify it does not appear on Telegram, then
detach with Ctrl+] or quit normally to return here.
EOF
    if "${SCH}" shell "${WS_A}" --shell-id "${SHELL_ID}"; then
        ok "${HARNESS}: reconnected to shell ${SHELL_ID}"
    else
        bad "${HARNESS}: reconnect to shell ${SHELL_ID} failed"
    fi
    confirm_check \
        "While reconnected, was RECONNECTED-${LIVE_TAG} absent from Telegram?" \
        "${HARNESS}: reconnect restored milestone suppression" \
        "${HARNESS}: milestone appeared while reconnected"
fi
echo

echo "== manual lifecycle check (task 6.5) =="
echo "On the phone you should now see, separated per workspace:"
echo "  - ▶️ submit and ✅/❌ terminal messages for both tasks"
echo "  - milestone messages (💬 turn end) if the harness hooks are seeded"
echo
echo "RESULT: PASS=${PASS} FAIL=${FAIL}"
[ "${FAIL}" -eq 0 ]
