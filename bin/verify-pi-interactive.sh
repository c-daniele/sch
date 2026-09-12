#!/bin/bash
# verify-pi-interactive.sh — guided live verification for add-pi-harness 10.3.
#
# This check needs a real PTY and operator input, so it cannot be reduced to an
# unattended probe. It opens Pi with an optional explicit model, then asks the
# operator to reattach to that exact CommandShell using its shell ID.
#
# Usage:
#   ./verify-pi-interactive.sh <workspace> [--model <bedrock-model-id>]
set -uo pipefail

WS=""
MODEL=""
while [ $# -gt 0 ]; do
    case "$1" in
        --model)
            [ $# -ge 2 ] || { echo "usage: $0 <workspace> [--model <id>]" >&2; exit 2; }
            MODEL="$2"
            shift 2
            ;;
        --help|-h)
            sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            if [ -z "${WS}" ]; then
                WS="$1"
            else
                echo "usage: $0 <workspace> [--model <id>]" >&2
                exit 2
            fi
            shift
            ;;
    esac
done
[ -n "${WS}" ] || { echo "usage: $0 <workspace> [--model <id>]" >&2; exit 2; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCH="${SCRIPT_DIR}/sch"

cat <<EOF
Phase 1: Pi TUI${MODEL:+ with explicit Bedrock model ${MODEL}}.
  1. Confirm the banner says harness=pi and the TUI opens successfully.
  2. Ask: Remember the keyword PI-INTERACTIVE-${WS} and reply with it.
  3. Confirm the answer, then detach with Ctrl+]. Do not quit Pi.
EOF
RUN_ARGS=(run "${WS}" --harness pi)
if [ -n "${MODEL}" ]; then
    RUN_ARGS+=(--model "${MODEL}")
fi
"${SCH}" "${RUN_ARGS[@]}" || exit 1

printf "Enter the shell ID printed by AgentCore for the detached session: "
IFS= read -r SHELL_ID
[ -n "${SHELL_ID}" ] || { echo "shell ID is required for a real reattach" >&2; exit 2; }
cat <<EOF
Phase 2: reattach to the same shell/TUI.
  1. Confirm the existing Pi process is still open.
  2. Ask: What keyword did I ask you to remember? Reply with only the keyword.
  3. Confirm it replies PI-INTERACTIVE-${WS}, then quit Pi normally.
EOF
"${SCH}" shell "${WS}" --shell-id "${SHELL_ID}" || exit 1

echo "PASS: Pi TUI/model/detach/reattach command flow completed."
echo "Confirm the two response checks above before marking OpenSpec task 10.3 complete."
