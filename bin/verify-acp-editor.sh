#!/bin/bash
# verify-acp-editor.sh — end-to-end verification of the ACP editor
# integration (change sch-acp-editor-integration, task 6.2; capabilities
# acp-editor-integration + acp-file-locality) against the LIVE AgentCore
# runtime (image >= v22), WITHOUT the Zed GUI.
#
# What it proves, for BOTH harnesses (opencode + claude):
#   A. Complete editor-contract session through `sch acp`: initialize
#      (advertising fs+terminal caps like Zed) -> session/new {cwd=<mirror>}
#      -> session/prompt; the agent NEVER issues fs/* or terminal/* to the
#      client (capability neutralization, design D3); every tool_call
#      location/diff path is LOCAL (under the mirror — path translation);
#      the file the agent creates in /mnt/workspace/repo appears in the
#      local mirror (remote->local sync).
#   B. A local save DURING the session (operator-style write into the
#      mirror) is visible to the agent on a following prompt
#      (local->remote sync, spec "Salvataggio locale visibile all'agente").
# And once (the sync engine is harness-independent — same fs channel code
# path for both):
#   C. Reconnection mid-sync: with the physical connection forced to drop
#      every few seconds (SCH_TUNNEL_PROACTIVE_RECONNECT_MS), a multi-MB
#      push converges with no loss/truncation/duplication — verified by
#      re-hydrating a FRESH mirror and comparing sha1 digests (spec
#      "Reconnection mid-sync").
#   D. LWW conflict: a local edit with a NEWER timestamp survives an
#      incoming remote change to the same file, with the explicit CONFLICT
#      warning on stderr (spec "Concurrent conflict on the same file").
#
# The ACP client is tunnel/test/fake-zed.js (design D5): it replicates the
# message contract Zed exercises, with programmable prompts and asserts.
#
# Requirements: AWS credentials with the same surface as `sch`, `aws` CLI,
# Node >= 18, tunnel deps installed (cd tunnel && npm install), python3.
# The two workspaces are created (index-seeded) if missing.
#
# Usage:
#   ./verify-acp-editor.sh [<opencode-ws>] [<claude-ws>] [--keep]
#     defaults: verify-acp-oc / verify-acp-cl
set -uo pipefail

WS_OC=""
WS_CL=""
KEEP=0
while [ $# -gt 0 ]; do
    case "$1" in
        --keep) KEEP=1 ;;
        --help|-h) sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) echo "unknown option '$1'" >&2; exit 2 ;;
        *)
            if [ -z "${WS_OC}" ]; then WS_OC="$1"
            elif [ -z "${WS_CL}" ]; then WS_CL="$1"
            else echo "usage: $0 [<opencode-ws>] [<claude-ws>] [--keep]" >&2; exit 2; fi
            ;;
    esac
    shift
done
WS_OC="${WS_OC:-verify-acp-oc}"
WS_CL="${WS_CL:-verify-acp-cl}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCH="${SCRIPT_DIR}/sch"
TUNNEL_DIR="${SCRIPT_DIR}/../tunnel"
FAKE_ZED="${TUNNEL_DIR}/test/fake-zed.js"
SCH_REGION="${SCH_REGION:-eu-west-1}"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/sch"
WS_DIR="${CONFIG_DIR}/workspaces"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/sch-verify-acp-XXXXXX")"

PASS=0
FAIL=0
WARN=0
ok()   { echo "PASS [acp-editor]: $*"; PASS=$((PASS+1)); }
bad()  { echo "FAIL [acp-editor]: $*"; FAIL=$((FAIL+1)); }
warn() { echo "WARN [acp-editor]: $*"; WARN=$((WARN+1)); }

echo "############################################"
echo "# ACP editor integration verification"
echo "# opencode workspace: ${WS_OC}"
echo "# claude workspace:   ${WS_CL}"
echo "# region:             ${SCH_REGION}"
echo "# scratch:            ${WORK}"
echo "############################################"
echo

# --- prerequisites -----------------------------------------------------------
command -v node >/dev/null 2>&1 || { echo "node not found" >&2; exit 1; }
command -v aws  >/dev/null 2>&1 || { echo "aws CLI not found" >&2; exit 1; }
[ -d "${TUNNEL_DIR}/node_modules" ] || { echo "tunnel deps missing (cd tunnel && npm install)" >&2; exit 1; }
[ -f "${FAKE_ZED}" ] || { echo "missing ${FAKE_ZED}" >&2; exit 1; }

# Seed workspace indexes with the right harness when missing (same pattern
# as verify-remote-ui-tunnel.sh; avoids interactive creation paths).
seed_ws() { # <ws> <harness>
    local ws="$1" harness="$2"
    [ -f "${WS_DIR}/${ws}" ] && return 0
    mkdir -p "${WS_DIR}"
    python3 -c '
import json, sys, uuid
ws, harness = sys.argv[1], sys.argv[2]
print(json.dumps({"runtimeSessionId": f"sch-{ws}-{uuid.uuid4()}", "harness": harness}))
' "${ws}" "${harness}" > "${WS_DIR}/${ws}"
    echo "-- seeded workspace '${ws}' (harness=${harness})"
}
seed_ws "${WS_OC}" opencode
seed_ws "${WS_CL}" claude

# fake-zed runner: writes report JSON to $3, stderr log to $4.
run_fake_zed() { # <ws> <mirror> <report-out> <stderr-out> <extra fake-zed args...>
    local ws="$1" mirror="$2" report="$3" errlog="$4"; shift 4
    node "${FAKE_ZED}" \
        --cmd "'${SCH}' acp '${ws}' --mirror '${mirror}'" \
        --cwd "${mirror}" \
        --timeout 420 \
        "$@" >"${report}" 2>"${errlog}"
}

# JSON accessor: prints eval(<expr>) over the parsed report `d`.
jq_py() { # <file> <expr>
    python3 -c "
import json, os, sys
d = json.load(open(sys.argv[1]))
print(eval(sys.argv[2]))
" "$1" "$2" 2>/dev/null
}

sha1_of() { python3 -c "import hashlib,sys; print(hashlib.sha1(open(sys.argv[1],'rb').read()).hexdigest())" "$1"; }

# ===========================================================================
# A+B per harness: full session, no client fs/terminal, local locations,
# agent file lands in the mirror; local save visible to a second prompt.
# ===========================================================================
run_harness_suite() { # <ws> <harness-label>
    local ws="$1" label="$2"
    local mirror="${WORK}/${label}/mirror/repo"
    mkdir -p "${mirror}"
    local marker="e2e-${label}-$$"
    local agent_file="acp-e2e-${label}.txt"
    local save_file="operator-save-${label}.txt"
    local report="${WORK}/${label}-report.json"
    local errlog="${WORK}/${label}-stderr.log"

    echo "== [${label}] A+B: editor-contract session with file locality =="
    run_fake_zed "${ws}" "${mirror}" "${report}" "${errlog}" \
        --prompt "Create a file named ${agent_file} containing exactly the single line ${marker} in the current directory using your file tools, then stop. Do not ask questions." \
        --between "printf 'saved-by-operator-${marker}\n' > '${mirror}/${save_file}'" \
        --settle 8 \
        --prompt2 "Read the file named ${save_file} in the current directory and reply with its exact content. Do not modify anything." \
        --linger 10 \
        --assert-no-client-fs \
        --assert-locations-under "${mirror}"
    local rc=$?

    if [ "${rc}" -eq 0 ]; then
        ok "[${label}] session completed (initialize -> session/new -> 2 prompt turns) with fake-zed asserts green"
    else
        bad "[${label}] fake-zed session failed (rc=${rc}) — see ${errlog}"
        tail -5 "${errlog}" 2>/dev/null | sed 's/^/    /'
        return
    fi

    local proto stop1 stop2
    proto=$(jq_py "${report}" "d['initialize']['protocolVersion']")
    stop1=$(jq_py "${report}" "d['stopReason']")
    stop2=$(jq_py "${report}" "d['stopReason2']")
    [ "${proto}" = "1" ] && ok "[${label}] initialize came from the real remote agent (protocolVersion=1)" \
                         || bad "[${label}] unexpected protocolVersion '${proto}'"
    { [ "${stop1}" = "end_turn" ] && [ "${stop2}" = "end_turn" ]; } \
        && ok "[${label}] both prompt turns streamed to end_turn" \
        || warn "[${label}] stopReasons: turn1=${stop1} turn2=${stop2}"

    local fs_calls
    fs_calls=$(jq_py "${report}" "len(d['fsCalls'])")
    [ "${fs_calls}" = "0" ] && ok "[${label}] zero fs/*-terminal/* requests reached the editor (client-fs neutralization)" \
                            || bad "[${label}] ${fs_calls} client fs/terminal call(s) leaked through the bridge"

    local n_loc bad_loc
    n_loc=$(jq_py "${report}" "len(d['locations'] + d['diffPaths'])")
    bad_loc=$(jq_py "${report}" "len([p for p in d['locations'] + d['diffPaths'] if not os.path.realpath(p).startswith(os.path.realpath('${mirror}'))])")
    if [ "${n_loc}" != "0" ] && [ -n "${n_loc}" ]; then
        [ "${bad_loc}" = "0" ] && ok "[${label}] all ${n_loc} tool_call location/diff paths are LOCAL (under the mirror)" \
                               || bad "[${label}] ${bad_loc}/${n_loc} location/diff paths NOT under the mirror"
    else
        warn "[${label}] no locations reported by the agent this turn (cannot assert path translation on locations)"
    fi

    if [ -f "${mirror}/${agent_file}" ] && grep -q "${marker}" "${mirror}/${agent_file}"; then
        ok "[${label}] file created by the agent in /mnt/workspace/repo appeared in the local mirror with the expected content"
    else
        bad "[${label}] agent-created '${agent_file}' missing (or wrong content) in the mirror"
    fi

    local text2
    text2=$(jq_py "${report}" "d['agentText2']")
    if echo "${text2}" | grep -q "saved-by-operator-${marker}"; then
        ok "[${label}] local mid-session save was visible to the agent on the following prompt (local->remote sync)"
    else
        bad "[${label}] agent did not see the operator save (agentText2: $(echo "${text2}" | head -c 200))"
    fi
    echo
}

run_harness_suite "${WS_OC}" "opencode"
run_harness_suite "${WS_CL}" "claude"
# The bridge exits asynchronously after fake-zed closes stdio; let its fs
# session release before the next independent lease acquisition.
sleep 3

# ===========================================================================
# C. Reconnection mid-sync (once — the fs channel is harness-independent).
#    Force physical reconnects every 6s while pushing ~6MB up, then pull
#    everything back into a FRESH mirror and compare digests.
# ===========================================================================
echo "== C: reconnection mid-sync (forced physical drops) =="
MIRROR_A="${WORK}/recon/mirror-a/repo"
MIRROR_B="${WORK}/recon/mirror-b/repo"
mkdir -p "${MIRROR_A}" "${MIRROR_B}"
for i in 1 2 3; do
    python3 -c "
import os, sys
path, seed = sys.argv[1], int(sys.argv[2])
rnd = os.urandom(2 * 1024 * 1024)
open(path, 'wb').write(rnd)
" "${MIRROR_A}/blob-${i}.bin" "${i}"
done
SHA_1=$(sha1_of "${MIRROR_A}/blob-1.bin"); SHA_2=$(sha1_of "${MIRROR_A}/blob-2.bin"); SHA_3=$(sha1_of "${MIRROR_A}/blob-3.bin")

RECON_ERR_A="${WORK}/recon-a-stderr.log"
SCH_TUNNEL_PROACTIVE_RECONNECT_MS=6000 run_fake_zed "${WS_OC}" "${MIRROR_A}" "${WORK}/recon-a.json" "${RECON_ERR_A}" --linger 45
rc_a=$?
sleep 3
if [ "${rc_a}" -eq 0 ]; then
    ok "push session survived (rc=0) with forced reconnects"
else
    bad "push session failed (rc=${rc_a}) — see ${RECON_ERR_A}"
fi
if grep -q "fs channel connection interrupted" "${RECON_ERR_A}" && grep -q "fs channel connection (re)established" "${RECON_ERR_A}"; then
    ok "physical drops actually happened mid-session on the fs channel ($(grep -c 'fs channel connection interrupted' "${RECON_ERR_A}") interruptions)"
else
    warn "no fs-channel reconnect evidence in stderr — the drop window may not have fired during sync"
fi

RECON_ERR_B="${WORK}/recon-b-stderr.log"
SCH_TUNNEL_PROACTIVE_RECONNECT_MS=6000 run_fake_zed "${WS_OC}" "${MIRROR_B}" "${WORK}/recon-b.json" "${RECON_ERR_B}" --linger 5
rc_b=$?
if [ "${rc_b}" -eq 0 ]; then
    ok "re-hydration session into a fresh mirror completed"
else
    bad "re-hydration session failed (rc=${rc_b}) — see ${RECON_ERR_B}"
fi
recon_ok=1
for i in 1 2 3; do
    f="${MIRROR_B}/blob-${i}.bin"
    [ -f "${f}" ] || { recon_ok=0; bad "blob-${i}.bin missing after re-hydration"; continue; }
    want_var="SHA_${i}"
    got=$(sha1_of "${f}")
    if [ "${got}" != "${!want_var}" ]; then
        recon_ok=0
        bad "blob-${i}.bin digest mismatch after reconnect-heavy sync (got ${got})"
    fi
done
[ "${recon_ok}" -eq 1 ] && ok "mirror converged byte-exact across forced mid-sync drops (3x2MB, sha1 verified round-trip)"
echo

# ===========================================================================
# D. LWW conflict with warning (once): local file with FUTURE mtime beats
#    the incoming remote change; stderr carries the explicit CONFLICT line.
# ===========================================================================
echo "== D: LWW conflict on concurrent modification =="
LWW_MIRROR="${WORK}/lww/mirror/repo"
mkdir -p "${LWW_MIRROR}"
LWW_FILE="lww-clash-$$.txt"
LWW_ERR="${WORK}/lww-stderr.log"
# Session: agent creates the file (turn 1); between turns we overwrite it
# locally with a FUTURE mtime (so any later remote event is "older" and the
# local copy must win); turn 2 makes the agent modify the same file remotely.
run_fake_zed "${WS_OC}" "${LWW_MIRROR}" "${WORK}/lww.json" "${LWW_ERR}" \
    --prompt "Create a file named ${LWW_FILE} containing exactly the single line remote-v1 in the current directory, then stop. Do not ask questions." \
    --between "printf 'local-edit-wins\n' > '${LWW_MIRROR}/${LWW_FILE}'; touch -t 203001010000 '${LWW_MIRROR}/${LWW_FILE}'" \
    --settle 5 \
    --prompt2 "Overwrite the file ${LWW_FILE} in the current directory so it contains exactly the single line remote-v2, then stop. Do not ask questions." \
    --linger 12
rc_lww=$?
[ "${rc_lww}" -eq 0 ] || warn "LWW session ended rc=${rc_lww} (continuing with artifact checks)"
if grep -q "CONFLICT on '${LWW_FILE}'" "${LWW_ERR}"; then
    ok "explicit LWW CONFLICT warning emitted on stderr identifying the file and the overwritten side"
else
    bad "no CONFLICT warning for ${LWW_FILE} on stderr — see ${LWW_ERR}"
fi
if grep -q "local-edit-wins" "${LWW_MIRROR}/${LWW_FILE}" 2>/dev/null; then
    ok "newer local copy survived the concurrent remote change (last-writer-wins)"
else
    bad "local copy was overwritten despite being newer: $(cat "${LWW_MIRROR}/${LWW_FILE}" 2>/dev/null | head -1)"
fi
echo

# ===========================================================================
# cleanup
# ===========================================================================
echo "== cleanup =="
if [ "${KEEP}" -eq 0 ]; then
    "${SCH}" stop "${WS_OC}" >/dev/null 2>&1 && echo "-- stopped workspace '${WS_OC}'" || warn "could not stop '${WS_OC}'"
    "${SCH}" stop "${WS_CL}" >/dev/null 2>&1 && echo "-- stopped workspace '${WS_CL}'" || warn "could not stop '${WS_CL}'"
else
    echo "-- --keep: leaving workspaces running"
fi
echo
echo "############################################"
echo "# ACP editor result: ${PASS} passed, ${FAIL} failed, ${WARN} warnings"
echo "# scratch kept at: ${WORK}"
echo "############################################"
[ "${FAIL}" -eq 0 ] || exit 1
exit 0
