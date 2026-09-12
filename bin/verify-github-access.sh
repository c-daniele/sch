#!/bin/bash
# verify-github-access.sh — verification of the opt-in GitHub access (TASK-26;
# specs: git-native-workflow "Opt-in GitHub access", provider-api-keys "GitHub
# token", runtime-image R46).
#
# Two sections, run independently:
#
#   offline  no AWS, no deployed runtime: runs the unit suites covering the
#            client transport (allowlist, origin sanitize, seed payload), the
#            shim reconciliation (seed recording, set/withdraw, operator
#            preservation, secrecy) and the dispatcher mapping, plus the image
#            pin guard. Always runnable (CI, laptop, microVM).
#
#   live     needs a DEPLOYED runtime whose image already carries this
#            capability (gh binary + shim reconciliation). Uses a disposable
#            workspace (created and deleted by the script) seeded from a
#            throwaway local repo. Asserts the microVM half:
#              - gh is pinned and on PATH in the harness;
#              - with GITHUB_TOKEN configured: remote has the recorded origin,
#                the tmpfs store helper pointer, GH_TOKEN/GITHUB_TOKEN in env,
#                and no token value in .git/config;
#              - without GITHUB_TOKEN: no origin, no helper (default unchanged);
#              - with SCH_VERIFY_GITHUB_REPO set (a real repo URL the operator
#                may push to): the harness pushes its branch itself.
#
# Prerequisites for the live section:
#   GITHUB_TOKEN in ~/.sch/env (chmod 600) for the with-token assertions;
#   SCH_VERIFY_GITHUB_TOKEN may hold the token value for the secrecy grep
#   (when unset, the .git/config secrecy check is skipped — the script never
#   exfiltrates the file, it only greps for the value you hand it);
#   SCH_VERIFY_GITHUB_REPO=https://github.com/<owner>/<throwaway>.git enables
#   the harness-push half (otherwise skipped).
#
# The live section MUTATES the workspace it creates (tasks, push when
# enabled) and deletes it afterwards (best-effort). The pushed branch is
# removed again with the operator's local credentials (best-effort).
#
# Usage:
#   ./verify-github-access.sh                         # offline section only
#   ./verify-github-access.sh <workspace-prefix>      # offline + live
#   ./verify-github-access.sh <workspace-prefix> live # live section only
#   ./verify-github-access.sh <workspace-prefix> live --harness claude
set -uo pipefail

SECTION_ARG="${1:-}"
SECTION="${2:-}"
shift $(( $# > 2 ? 2 : $# )) || true
HARNESS="opencode"
while [ $# -gt 0 ]; do
    case "$1" in
        --harness) HARNESS="${2:-}"; shift ;;
        *) echo "unknown option '$1'" >&2; exit 2 ;;
    esac
    shift
done
if [ -z "${SECTION}" ]; then
    SECTION=$([ -n "${SECTION_ARG}" ] && echo "full" || echo "offline")
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SCH="${SCRIPT_DIR}/sch"
SCH_REGION="${SCH_REGION:-eu-west-1}"

PASS=0
FAIL=0
ok()  { echo "PASS: $*"; PASS=$((PASS+1)); }
bad() { echo "FAIL: $*"; FAIL=$((FAIL+1)); }

# --- offline: unit suites (always runnable) ------------------------------------
if [ "${SECTION}" = "offline" ] || [ "${SECTION}" = "full" ]; then
    echo "== [offline] unit suites"
    ( cd "${REPO_ROOT}/cli" && python3 -m unittest tests.test_userenv tests.test_gitnative ) \
        && ok "cli userenv+gitnative suites" || bad "cli suites failed"
    ( cd "${REPO_ROOT}/image/app" && python3 -m unittest test_github_access test_runtime_dependency_pins test_provider_api_keys test_user_provider_keys test_git_native ) \
        && ok "image github/pins/dispatcher/staging suites" || bad "image suites failed"
    if [ "${SECTION}" = "offline" ]; then
        echo
        echo "== verify-github-access (offline): ${PASS} passed, ${FAIL} failed"
        [ "${FAIL}" -eq 0 ]
        exit $?
    fi
fi

# --- live ----------------------------------------------------------------------
PREFIX="${SECTION_ARG:-}"
[ -n "${PREFIX}" ] || { echo "usage: $0 <workspace-prefix> [live] [--harness N]"; exit 2; }
STAMP="$(date +%Y%m%d-%H%M%S)"
WS="${PREFIX}-gha-${STAMP}"
BRANCH="verify/gh-${STAMP}"
FAKE_ORIGIN="https://github.com/sch-verify/throwaway.git"

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
    local ws="$1" max_wait="${2:-300}" i st
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

cleanup() {
    cd "${REPO_ROOT}" || true
    if [ "${PUSHED:-no}" = "yes" ] && [ -n "${SCH_VERIFY_GITHUB_REPO:-}" ]; then
        git push origin --delete "${BRANCH}" >/dev/null 2>&1 || true
    fi
    git branch -D "${BRANCH}" >/dev/null 2>&1 || true
    git branch -D "${BRANCH}-remote" >/dev/null 2>&1 || true
    yes | "${SCH}" delete "${WS}" >/dev/null 2>&1 || true
    rm -rf "${REPO}"
}
REPO="$(mktemp -d "${TMPDIR:-/tmp}/sch-github-access-XXXXXX")"
trap cleanup EXIT
git -C "${REPO}" init -q
git -C "${REPO}" -c user.name=verify -c user.email=verify@local commit -q --allow-empty -m "base commit"
echo "hello" > "${REPO}/hello.txt"
git -C "${REPO}" add -A
git -C "${REPO}" -c user.name=verify -c user.email=verify@local commit -qm "add hello.txt"
# The recorded originUrl is bound at seed like the branch: the local origin
# must exist BEFORE the first --branch run. Real repo when the push half is
# enabled, a never-contacted fake one when only the plumbing is asserted,
# nothing when the default (tokenless) posture is under test.
TOKEN_CONFIGURED="no"
if "${SCH}" info 2>/dev/null | grep -q "GITHUB_TOKEN"; then
    TOKEN_CONFIGURED="yes"
fi
if [ -n "${SCH_VERIFY_GITHUB_REPO:-}" ]; then
    git -C "${REPO}" remote add origin "${SCH_VERIFY_GITHUB_REPO}"
elif [ "${TOKEN_CONFIGURED}" = "yes" ]; then
    git -C "${REPO}" remote add origin "${FAKE_ORIGIN}"
fi
cd "${REPO}"
echo "== live on ${WS} (harness ${HARNESS}, branch ${BRANCH}, token configured: ${TOKEN_CONFIGURED})"

# --- 1. gh is pinned and on PATH ------------------------------------------------
echo "== [1] gh in the image + first harness commit"
EXPECTED_GH="$(grep -E '^ARG GH_VERSION=' "${REPO_ROOT}/image/Dockerfile" | cut -d= -f2)"
"${SCH}" task "${WS}" --branch "${BRANCH}" --harness "${HARNESS}" \
    'create a file named remote.txt containing exactly the text "from harness", then run: git add -A && git -c user.name=verify -c user.email=verify@local commit -qm "harness commit"' \
    || bad "task submit"
ST="$(wait_terminal "${WS}" 300)" || bad "task did not finish"
[ "${ST}" = "succeeded" ] && ok "task terminal (${ST})" || bad "task state ${ST}"
GH_LINE="$(remote "${WS}" 'gh --version | head -n 1')"
echo "remote gh: ${GH_LINE}"
echo "${GH_LINE}" | grep -q "${EXPECTED_GH}" \
    && ok "gh ${EXPECTED_GH} on PATH in the harness" || bad "gh version mismatch (want ${EXPECTED_GH})"

# --- 2. with-token vs default ----------------------------------------------------
echo "== [2] origin/helper reconciliation"
REMOTE_REPO='cd /mnt/workspace/repo 2>/dev/null || cd /home/sch/workspace/repo'
REMOTE_ORIGIN="$(remote "${WS}" "${REMOTE_REPO}; git remote -v; true")"
REMOTE_HELPER="$(remote "${WS}" "${REMOTE_REPO}; git config --get credential.helper; true")"
REMOTE_ENV="$(remote "${WS}" 'env | grep -cE "^(GH_TOKEN|GITHUB_TOKEN)="; true')"
if [ "${TOKEN_CONFIGURED}" = "yes" ]; then
    [ -n "$(echo "${REMOTE_ORIGIN}" | tr -d '[:space:]')" ] \
        && ok "remote has an origin (opt-in)" || bad "remote has no origin despite staged token"
    if [ -n "${SCH_VERIFY_GITHUB_REPO:-}" ]; then
        echo "${REMOTE_ORIGIN}" | grep -qF "${SCH_VERIFY_GITHUB_REPO}" \
            && ok "remote origin is the recorded URL" || bad "origin is not the recorded URL: ${REMOTE_ORIGIN}"
    fi
    [ "${REMOTE_HELPER}" = "store --file /run/sch/git-credentials" ] \
        && ok "repo-local helper points at the tmpfs store" || bad "helper is '${REMOTE_HELPER}'"
    [ "${REMOTE_ENV}" = "2" ] \
        && ok "GH_TOKEN and GITHUB_TOKEN both exported" || bad "token env count is ${REMOTE_ENV}"
    if [ -n "${SCH_VERIFY_GITHUB_TOKEN:-}" ]; then
        LEAK="$(remote "${WS}" "${REMOTE_REPO}; grep -c '${SCH_VERIFY_GITHUB_TOKEN}' .git/config state 2>/dev/null; git config --list | grep -c '${SCH_VERIFY_GITHUB_TOKEN}'; true")"
        [ "$(echo "${LEAK}" | tr -d '[:space:]')" = "00" ] \
            && ok "no token value in .git/config or git config" || bad "token value found in persisted git config"
    else
        echo "SKIP: token secrecy grep (set SCH_VERIFY_GITHUB_TOKEN to enable)"
    fi
else
    [ -z "$(echo "${REMOTE_ORIGIN}" | tr -d '[:space:]')" ] \
        && ok "no origin without a token (default unchanged)" || bad "unexpected origin: ${REMOTE_ORIGIN}"
    [ -z "$(echo "${REMOTE_HELPER}" | tr -d '[:space:]')" ] \
        && ok "no helper without a token (default unchanged)" || bad "unexpected helper: ${REMOTE_HELPER}"
fi

# --- 3. harness push (only with a real repo + token) ------------------------------
PUSHED="no"
if [ -n "${SCH_VERIFY_GITHUB_REPO:-}" ] && [ "${TOKEN_CONFIGURED}" = "yes" ]; then
    echo "== [3] harness pushes its branch itself"
    "${SCH}" task "${WS}" --continue \
        'create a file named pushed.txt containing exactly the text "pushed by harness", then run: git add -A && git -c user.name=verify -c user.email=verify@local commit -qm "harness push" && git push origin '"${BRANCH}" \
        || bad "push task submit"
    ST="$(wait_terminal "${WS}" 300)" || bad "push task did not finish"
    [ "${ST}" = "succeeded" ] && ok "push task terminal (${ST})" || bad "push task state ${ST}"
    git fetch origin "${BRANCH}:${BRANCH}-remote" 2>/dev/null \
        && ok "pushed branch visible locally" || bad "pushed branch not found (local credentials?)"
    git show "${BRANCH}-remote:pushed.txt" 2>/dev/null | grep -q "pushed by harness" \
        && ok "harness commit arrived via its own push" || bad "pushed content missing"
    PUSHED="yes"
else
    echo "SKIP: harness push (set SCH_VERIFY_GITHUB_REPO + GITHUB_TOKEN to enable)"
fi

# --- 4. fetch still delivers -------------------------------------------------------
echo "== [4] fetch delivery unchanged"
"${SCH}" fetch "${WS}" && ok "fetch exits 0" || bad "fetch failed"
git show "${BRANCH}:remote.txt" 2>/dev/null | grep -q "from harness" \
    && ok "harness commit delivered via fetch" || bad "harness work missing on ${BRANCH}"
if [ "${PUSHED}" = "yes" ]; then
    git show "${BRANCH}:pushed.txt" 2>/dev/null | grep -q "pushed by harness" \
        && ok "pushed commit delivered via fetch" || bad "pushed content missing on ${BRANCH}"
fi

echo
echo "== verify-github-access (live): ${PASS} passed, ${FAIL} failed"
[ "${FAIL}" -eq 0 ]
