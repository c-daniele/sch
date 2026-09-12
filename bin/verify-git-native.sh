#!/bin/bash
# verify-git-native.sh — end-to-end verification of the git-native workflow
# (add-git-native-workflow; spec: git-native-workflow, runtime-image).
#
# Requires a deployed runtime image that includes the `git-seed` /
# `git-snapshot` actions (task 1.5) and a laptop-side git identity.
#
# Flow:
#   1. create a throwaway local git repo (the "operator repo") with history
#   2. PARALLEL scenario (task 6.3): seed TWO fresh workspaces from the same
#      HEAD with two different branches, submit one task to each, `sch fetch`
#      both, assert both local branches exist with the session commits and
#      merge cleanly without overwriting each other
#   3. --continue on a seeded workspace does not re-seed (no bundle transfer)
#   4. RECOVERY scenario (task 6.4): start a task that leaves the remote
#      worktree dirty, stop the workspace (checkpoint), reset-session
#      (workspace restored from checkpoint on next contact), `sch fetch`
#      recovers the commits plus a `wip: session snapshot` service commit
#   5. CREDENTIALS scenario (task 6.5): assert no git credentials exist in
#      the remote workspace environment during the whole flow
#   6. IDEMPOTENCE: double `sch fetch` with no new work exits cleanly with
#      no new commits
#
# Usage:
#   ./verify-git-native.sh <workspace-prefix>
set -uo pipefail

PREFIX="${1:-}"
[ -n "${PREFIX}" ] || { echo "usage: $0 <workspace-prefix>"; exit 2; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCH="${SCRIPT_DIR}/sch"
SCH_REGION="${SCH_REGION:-eu-west-1}"

WS_A="${PREFIX}-gna"
WS_B="${PREFIX}-gnb"
WS_R="${PREFIX}-gnr"

PASS=0
FAIL=0
ok()   { echo "PASS: $*"; PASS=$((PASS+1)); }
bad()  { echo "FAIL: $*"; FAIL=$((FAIL+1)); }

runtime_arn() {
    aws cloudformation describe-stacks \
        --stack-name "${SCH_PROJECT:-sch}-${SCH_ENV:-dev}-runtime" \
        --region "${SCH_REGION}" \
        --query "Stacks[0].Outputs[?OutputKey=='RuntimeArn'].OutputValue" \
        --output text
}
ARN="$(runtime_arn)"

remote() { # <workspace> <command string, single-quote free> -> stdout
    local ws="$1" cmd="$2" sid
    sid=$(python3 -c "import json;print(json.load(open('${HOME}/.config/sch/workspaces/${ws}'))['runtimeSessionId'])")
    agentcore exec \
        --runtime "${ARN}" \
        --session-id "${sid}" \
        --region "${SCH_REGION}" \
        --timeout 300 \
        --json \
        -- sh -c "'${cmd}'" 2>/dev/null \
      | python3 -c 'import json,sys; print(json.load(sys.stdin).get("stdout",""), end="")'
}

wait_terminal() { # <workspace> <max_wait>
    local ws="$1" max_wait="${2:-240}" i st
    for i in $(seq 1 "${max_wait}"); do
        st=$("${SCH}" status "${ws}" --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("state",""))')
        case "${st}" in
            succeeded|failed|timed-out|interrupted) echo "${st}"; return 0 ;;
        esac
        sleep 2
    done
    echo "timeout"
    return 1
}

# --- 1. throwaway operator repo -------------------------------------------------
REPO="$(mktemp -d "${TMPDIR:-/tmp}/sch-gitnative-XXXXXX")"
trap 'rm -rf "${REPO}"' EXIT
git -C "${REPO}" init -q
git -C "${REPO}" -c user.name=verify -c user.email=verify@local commit -q --allow-empty -m "base commit"
echo "shared file" > "${REPO}/shared.txt"
git -C "${REPO}" add -A
git -C "${REPO}" -c user.name=verify -c user.email=verify@local commit -qm "add shared.txt"
BASE_SHA="$(git -C "${REPO}" rev-parse HEAD)"
echo "== operator repo at ${REPO} (HEAD ${BASE_SHA:0:12})"
cd "${REPO}"

# --- 2. parallel scenario (task 6.3) --------------------------------------------
echo "== [2] parallel: two workspaces seeded from the same HEAD"
TASK_A='create a file named from-a.txt containing exactly the text "work A", then git add and git commit it with message "task A"'
TASK_B='create a file named from-b.txt containing exactly the text "work B", then git add and git commit it with message "task B"'

"${SCH}" task "${WS_A}" --branch "verify/a" "${TASK_A}" || bad "task submit A"
"${SCH}" task "${WS_B}" --branch "verify/b" "${TASK_B}" || bad "task submit B"

ST_A="$(wait_terminal "${WS_A}" 300)" && ok "task A terminal (${ST_A})" || bad "task A did not finish"
ST_B="$(wait_terminal "${WS_B}" 300)" && ok "task B terminal (${ST_B})" || bad "task B did not finish"

"${SCH}" fetch "${WS_A}" && ok "fetch A" || bad "fetch A failed"
"${SCH}" fetch "${WS_B}" && ok "fetch B" || bad "fetch B failed"

git rev-parse --verify -q "verify/a" >/dev/null && ok "local branch verify/a exists" || bad "verify/a missing"
git rev-parse --verify -q "verify/b" >/dev/null && ok "local branch verify/b exists" || bad "verify/b missing"

git show "verify/a:from-a.txt" 2>/dev/null | grep -q "work A" && ok "branch a carries task A work" || bad "task A work missing on verify/a"
git show "verify/b:from-b.txt" 2>/dev/null | grep -q "work B" && ok "branch b carries task B work" || bad "task B work missing on verify/b"
git show "verify/a:from-b.txt" >/dev/null 2>&1 && bad "branch a contaminated by task B" || ok "no cross-contamination on branch a"

# merge both without overwrites
git -c user.name=verify -c user.email=verify@local merge -q --no-edit "verify/a" && ok "merge verify/a" || bad "merge verify/a failed"
git -c user.name=verify -c user.email=verify@local merge -q --no-edit "verify/b" && ok "merge verify/b" || bad "merge verify/b failed"
[ -f from-a.txt ] && [ -f from-b.txt ] && [ -f shared.txt ] \
    && ok "both branches merged, nothing overwritten" || bad "merged worktree incomplete"

# --- 3. --continue does not re-seed ---------------------------------------------
echo "== [3] --continue reuses the seeded branch"
CONT_OUT="$("${SCH}" task "${WS_A}" --continue 'append the line "more work" to from-a.txt, then git add and git commit it with message "task A2"' 2>&1)" \
    && ok "continue task submitted" || bad "continue task failed"
echo "${CONT_OUT}" | grep -q "no re-seed" && ok "no re-seed on --continue" || bad "expected 'no re-seed' notice"
wait_terminal "${WS_A}" 300 >/dev/null && ok "continue task terminal" || bad "continue task did not finish"
"${SCH}" fetch "${WS_A}" && ok "fetch after continue" || bad "fetch after continue failed"
git show "verify/a:from-a.txt" | grep -q "more work" && ok "continued work delivered" || bad "continued work missing"

# --- 4. recovery scenario (task 6.4) --------------------------------------------
echo "== [4] recovery: dirty worktree survives stop + session reset"
"${SCH}" task "${WS_R}" --branch "verify/r" \
    'create a file named committed.txt with the text "committed work", git add and git commit it with message "committed"; then create a file named uncommitted.txt with the text "dirty state" and DO NOT commit it' \
    || bad "recovery task submit"
wait_terminal "${WS_R}" 300 >/dev/null && ok "recovery task terminal" || bad "recovery task did not finish"
"${SCH}" stop "${WS_R}" && ok "workspace stopped (checkpoint forced)" || bad "stop failed"
yes | "${SCH}" reset-session "${WS_R}" && ok "session reset (next contact restores from checkpoint)" || bad "reset-session failed"
"${SCH}" fetch "${WS_R}" && ok "fetch after restore" || bad "fetch after restore failed"
git show "verify/r:committed.txt" 2>/dev/null | grep -q "committed work" && ok "agent commit recovered" || bad "agent commit missing"
git show "verify/r:uncommitted.txt" 2>/dev/null | grep -q "dirty state" && ok "dirty state recovered via service snapshot" || bad "dirty state missing"
git log "verify/r" --format='%an %s' | grep -q "^sch-session .* wip: session snapshot" \
    && ok "service snapshot commit has the conventional author/message" || bad "service snapshot commit missing"

# --- 5. no credentials in the remote environment (task 6.5) ---------------------
echo "== [5] credentials never reach the remote workspace"
CRED_SCAN="$(remote "${WS_A}" 'env | grep -iE "github|git_token|gh_token" ; ls -a ~/.git-credentials ~/.config/gh 2>/dev/null ; git config --global --get credential.helper ; true')"
if [ -z "$(echo "${CRED_SCAN}" | tr -d '[:space:]')" ]; then
    ok "no git credentials/env/helpers in the remote workspace"
else
    bad "possible credential material found remotely: ${CRED_SCAN}"
fi
REMOTE_ORIGIN="$(remote "${WS_A}" 'cd /home/sch/workspace/repo 2>/dev/null || cd /mnt/workspace/repo; git remote -v; true')"
if [ -z "$(echo "${REMOTE_ORIGIN}" | tr -d '[:space:]')" ]; then
    ok "remote clone has no origin remote (bundle-seeded, provider-isolated)"
else
    bad "remote clone has remotes configured: ${REMOTE_ORIGIN}"
fi

# --- 6. idempotent double fetch ---------------------------------------------------
echo "== [6] idempotence: double fetch without new work"
SHA_BEFORE="$(git rev-parse verify/a)"
"${SCH}" fetch "${WS_A}" && ok "repeat fetch exits 0" || bad "repeat fetch errored"
SHA_AFTER="$(git rev-parse verify/a)"
[ "${SHA_BEFORE}" = "${SHA_AFTER}" ] && ok "no new commits on repeat fetch" || bad "repeat fetch moved the ref"

echo
echo "== verify-git-native: ${PASS} passed, ${FAIL} failed"
[ "${FAIL}" -eq 0 ]
