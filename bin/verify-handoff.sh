#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -gt 0 ]; then
  WS="$1"
  DELETE_WS=0
else
  WS="sch-handoff-verify-$(date +%s)-$$"
  DELETE_WS=1
fi
command -v opencode >/dev/null || { echo "FAIL: opencode is required" >&2; exit 1; }
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCH="$ROOT/bin/sch"
TMP="$(mktemp -d)"
TOKEN="SCH_HANDOFF_$(date +%s)_$$"
show_failure() {
  rc=$?
  echo "FAIL: verify-handoff exited with status $rc" >&2
  for log in "$TMP"/*.err; do
    [ -f "$log" ] || continue
    echo "--- $(basename "$log") ---" >&2
    cat "$log" >&2
  done
  exit "$rc"
}
cleanup() {
  rm -rf "$TMP"
  "$SCH" stop "$WS" >/dev/null 2>&1 || true
  if [ "$DELETE_WS" -eq 1 ]; then
    "$SCH" delete "$WS" --yes >/dev/null 2>&1 || true
  fi
}
trap show_failure ERR
trap cleanup EXIT

cd "$TMP"
git init -q
git config user.name sch-verify
git config user.email sch-verify@local
printf 'handoff verification\n' > README.md
git add README.md
git commit -qm init

echo "==> creating local OpenCode session with continuity token $TOKEN"
opencode run --standalone "Remember the exact token $TOKEN for a later message. Reply with exactly READY." >/dev/null
SESSION_ID="$(opencode session list --format json | python3 -c '
import json, os, sys
cwd = os.path.realpath(os.getcwd())
sessions = [s for s in json.load(sys.stdin) if os.path.realpath(str(s.get("directory", ""))) == cwd]
if not sessions:
    raise SystemExit("no OpenCode session created for temporary project")
print(max(sessions, key=lambda s: int(s.get("updated", 0)))["id"])
')"

echo "==> handing off $SESSION_ID to $WS"
first="$($SCH handoff "$WS" --session "$SESSION_ID" 2>"$TMP/first.err")"
test "$first" = "$SESSION_ID"

echo "==> continuing imported session and checking preserved context"
task_id="$($SCH task "$WS" --continue "What exact token did I ask you to remember? Reply with only the token.")"
test -n "$task_id"
state=""
for _ in $(seq 1 240); do
  # `sch status` exits 3 when the persisted `running` state is stale
  # (add-task-liveness-safety task 1.2); under `set -e` that must not abort
  # the poll — the loop below decides on the state it just read.
  status="$($SCH status "$WS" --json)" || test $? -eq 3
  status_task_id="$(printf '%s' "$status" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("task_id", ""))')"
  state="$(printf '%s' "$status" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("state", ""))')"
  if [ "$status_task_id" = "$task_id" ]; then
    case "$state" in
      succeeded|failed|timed-out|interrupted) break ;;
    esac
  fi
  sleep 1
done
output="$(printf '%s' "$status" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("output", ""))')"
if [ "$state" != "succeeded" ]; then
  error="$(printf '%s' "$status" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("error", ""))')"
  echo "FAIL: continued task ended in state '$state': $error" >&2
  exit 1
fi
case "$output" in
  *"$TOKEN"*) ;;
  *) echo "FAIL: continued task did not recall $TOKEN" >&2; exit 1 ;;
esac

echo "==> re-pushing the same session and checking last-write-wins warning"
second="$($SCH handoff "$WS" --session "$SESSION_ID" 2>"$TMP/second.err")"
test "$second" = "$SESSION_ID"
grep -q "overwritten" "$TMP/second.err"
trap - ERR
echo "PASS: handoff import, preserved context, continue, and last-write-wins re-push"
