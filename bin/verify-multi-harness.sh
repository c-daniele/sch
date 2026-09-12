#!/bin/bash
# verify-multi-harness.sh — end-to-end verification of the multi-harness
# contract (sch-multi-harness, tasks 8.2–8.5).
#
# Runs the supplied harness paths (opencode + claude, optionally pi) end-to-end:
#   - interactive shell + brainstorm → `--continue` headless + L2
#     checkpoint/restore + `sch status` for each harness
#   - mutual-exclusivity rejection (task 8.4)
#   - upgrade-reconcile of a legacy workspace index (task 8.5)
#
# Per-harness assertions (task 8.3): sentinel file in repo, harness session
# retrieved (OpenCode session row / Claude JSONL resume id), config preserved
# across resume, `harness` field present in `sch status` output.
#
# This script delegates the per-harness headless flow to
# `verify-headless-tasks.sh --harness <x>`, then layers the multi-harness-
# specific assertions on top. It requires a deployed runtime (ARN + checkpoint
# bucket) and AWS credentials with the same surface as `sch`.
#
# Usage:
#   ./verify-multi-harness.sh <opencode-workspace> <claude-workspace>
#   ./verify-multi-harness.sh <opencode-workspace> <claude-workspace> <pi-workspace>
#   ./verify-multi-harness.sh <opencode-workspace> <claude-workspace> --skip-claude
#
# Exit non-zero if any harness path fails; the per-harness PASS/FAIL counts
# are surfaced from the delegate script.
set -uo pipefail

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
    sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
fi

OC_WS="${1:-}"
CL_WS="${2:-}"
[ -n "${OC_WS}" ] && [ -n "${CL_WS}" ] || {
    echo "usage: $0 <opencode-workspace> <claude-workspace> [pi-workspace] [--skip-claude]"
    exit 2
}
shift 2 || true
SKIP_CLAUDE=0
PI_WS=""
while [ $# -gt 0 ]; do
    case "$1" in
        --skip-claude) SKIP_CLAUDE=1 ;;
        --*) echo "unknown option '$1'" >&2; exit 2 ;;
        *)
            if [ -z "${PI_WS}" ]; then
                PI_WS="$1"
            else
                echo "unexpected positional argument '$1'" >&2
                exit 2
            fi
            ;;
    esac
    shift
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCH="${SCRIPT_DIR}/sch"
HL_VERIFY="${SCRIPT_DIR}/verify-headless-tasks.sh"
SCH_REGION="${SCH_REGION:-eu-west-1}"

PASS=0
FAIL=0
ok()   { echo "PASS [multi-harness]: $*"; PASS=$((PASS+1)); }
bad()  { echo "FAIL [multi-harness]: $*"; FAIL=$((FAIL+1)); }

echo "############################################"
echo "# Multi-harness verification"
echo "# opencode workspace: ${OC_WS}"
echo "# claude workspace:    ${CL_WS}"
[ -z "${PI_WS}" ] || echo "# pi workspace:        ${PI_WS}"
echo "############################################"
echo

# --- Task 8.5: upgrade-reconcile of a legacy workspace index ---------------
echo "== 8.5 upgrade-reconcile: legacy workspace index without harness field =="
LEGACY_WS="legacy-$OC_WS-$$"
LEGACY_WS_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/sch/workspaces/${LEGACY_WS}"
mkdir -p "$(dirname "${LEGACY_WS_FILE}")"
# Write a legacy bare-sid index (pre-multi-harness format).
LEGACY_SID="sch-${LEGACY_WS}-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
echo "${LEGACY_SID}" > "${LEGACY_WS_FILE}"
# First access WITHOUT --harness → reconcile to opencode (no sessionId change).
# Use `sch list` to trigger the read path (resolve_harness is called by
# shell/task/open; list does NOT call it). To exercise the reconcile without
# opening a TUI, invoke `sch shell` with a fake agentcore on PATH so the exec
# is a no-op — but that requires agentcore. Instead, exercise the reconcile
# via the task path: `sch task <legacy-ws> "..."` calls resolve_harness first.
# Since the workspace has no runtime session provisioned, the task invocation
# will fail at the AWS call, but resolve_harness runs BEFORE the AWS call and
# persists the harness. So: expect the task to fail at invocation, but the
# index file should now carry harness=opencode.
"${SCH}" task "${LEGACY_WS}" "noop prompt to trigger reconcile" >/dev/null 2>&1 || true
LEGACY_HARNESS=$(python3 -c 'import json,sys
try:
    d=json.load(open(sys.argv[1]))
    print(d.get("harness",""))
except Exception:
    print("")' "${LEGACY_WS_FILE}" 2>/dev/null || echo "")
LEGACY_SID_AFTER=$(python3 -c 'import json,sys
try:
    d=json.load(open(sys.argv[1]))
    print(d.get("runtimeSessionId") or d.get("sessionId") or "")
except Exception:
    print("")' "${LEGACY_WS_FILE}" 2>/dev/null || echo "")
if [ "${LEGACY_HARNESS}" = "opencode" ] && [ "${LEGACY_SID_AFTER}" = "${LEGACY_SID}" ]; then
    ok "legacy workspace reconciled to harness=opencode (sessionId preserved)"
else
    bad "legacy reconcile failed: harness='${LEGACY_HARNESS}' sid_before='${LEGACY_SID}' sid_after='${LEGACY_SID_AFTER}'"
fi

# Explicit upgrade handoff: legacy ws + --harness claude → persist claude.
LEGACY_WS2="legacy-cl-$OC_WS-$$"
LEGACY_WS2_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/sch/workspaces/${LEGACY_WS2}"
LEGACY_SID2="sch-${LEGACY_WS2}-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
echo "${LEGACY_SID2}" > "${LEGACY_WS2_FILE}"
"${SCH}" task "${LEGACY_WS2}" --harness claude "noop prompt to trigger handoff" >/dev/null 2>&1 || true
LEGACY_HARNESS2=$(python3 -c 'import json,sys
try:
    d=json.load(open(sys.argv[1]))
    print(d.get("harness",""))
except Exception:
    print("")' "${LEGACY_WS2_FILE}" 2>/dev/null || echo "")
if [ "${LEGACY_HARNESS2}" = "claude" ]; then
    ok "legacy workspace with --harness claude → upgrade handoff persisted claude"
else
    bad "legacy handoff failed: harness='${LEGACY_HARNESS2}' (expected claude)"
fi

# Explicit third-value upgrade: a legacy index selected with --harness pi must
# retain its runtime session id and persist pi rather than taking the default.
LEGACY_WS3="legacy-pi-$OC_WS-$$"
LEGACY_WS3_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/sch/workspaces/${LEGACY_WS3}"
LEGACY_SID3="sch-${LEGACY_WS3}-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
echo "${LEGACY_SID3}" > "${LEGACY_WS3_FILE}"
"${SCH}" task "${LEGACY_WS3}" --harness pi "noop prompt to trigger handoff" >/dev/null 2>&1 || true
LEGACY_PI_STATE=$(python3 -c 'import json,sys
try:
    d=json.load(open(sys.argv[1]))
    print((d.get("harness", "")) + " " + (d.get("runtimeSessionId") or d.get("sessionId") or ""))
except Exception:
    print("")' "${LEGACY_WS3_FILE}" 2>/dev/null || echo "")
if [ "${LEGACY_PI_STATE}" = "pi ${LEGACY_SID3}" ]; then
    ok "legacy workspace with --harness pi persisted pi (sessionId preserved)"
else
    bad "legacy pi handoff failed: state='${LEGACY_PI_STATE}'"
fi
echo

# --- Tasks 8.2/8.3: per-harness end-to-end via the delegate script ---------
echo "== 8.2/8.3 opencode path: verify-headless-tasks.sh --harness opencode =="
if "${HL_VERIFY}" "${OC_WS}" --harness opencode; then
    ok "opencode harness path end-to-end green"
else
    bad "opencode harness path had failures (see output above)"
fi
echo

if [ "${SKIP_CLAUDE}" -eq 1 ]; then
    echo "(skipping claude path per --skip-claude)"
else
    echo "== 8.2/8.3 claude path: verify-headless-tasks.sh --harness claude =="
    if "${HL_VERIFY}" "${CL_WS}" --harness claude; then
        ok "claude harness path end-to-end green"
    else
        bad "claude harness path had failures (see output above)"
    fi
fi
echo

if [ -n "${PI_WS}" ]; then
    echo "== 10.1 pi path: verify-headless-tasks.sh --harness pi =="
    if "${HL_VERIFY}" "${PI_WS}" --harness pi; then
        ok "pi harness path end-to-end green"
    else
        bad "pi harness path had failures (see output above)"
    fi
    echo
fi

# --- Task 10.2: mutual-exclusivity rejection for all harness pairs ----------
echo "== 10.2 mutual-exclusivity: every bound workspace rejects other harnesses =="
check_mutex() { # <workspace> <bound-harness> <attempted-harness>
    local ws="$1" bound="$2" attempted="$3" out rc
    out=$("${SCH}" task "${ws}" --harness "${attempted}" "should be rejected" 2>&1)
    rc=$?
    if [ "${rc}" -ne 0 ] && echo "${out}" | grep -q "cannot switch to"; then
        ok "${bound} workspace rejected divergent --harness ${attempted}: ${out}"
    else
        bad "${bound} workspace did NOT reject --harness ${attempted} (rc=${rc}): ${out}"
    fi
}

check_mutex "${OC_WS}" opencode claude
check_mutex "${OC_WS}" opencode pi
if [ "${SKIP_CLAUDE}" -ne 1 ]; then
    check_mutex "${CL_WS}" claude opencode
    check_mutex "${CL_WS}" claude pi
fi
if [ -n "${PI_WS}" ]; then
    check_mutex "${PI_WS}" pi opencode
    if [ "${SKIP_CLAUDE}" -ne 1 ]; then
        check_mutex "${PI_WS}" pi claude
    fi
fi
echo

# --- Task 10.6: unsupported live entry points reject a pi workspace ---------
if [ -n "${PI_WS}" ]; then
    echo "== 10.6 pi capability rejections: web/attach/acp/handoff =="
    check_pi_rejection() { # <command> <error-pattern> [args...]
        local command="$1" error_pattern="$2" out rc
        shift 2
        if [ "${command}" = "handoff" ]; then
            out=$(PATH="${HANDOFF_STUB_DIR}:${PATH}" "${SCH}" "${command}" "${PI_WS}" "$@" 2>&1)
        else
            out=$("${SCH}" "${command}" "${PI_WS}" "$@" 2>&1)
        fi
        rc=$?
        if [ "${rc}" -ne 0 ] && echo "${out}" | grep -Eqi "${error_pattern}"; then
            ok "sch ${command} rejected pi workspace with explicit error (rc=${rc}): ${out}"
        else
            bad "sch ${command} did not explicitly reject pi workspace (rc=${rc}): ${out}"
        fi
    }

    # Handoff validates/exports a local OpenCode session before inspecting the
    # remote binding. A deterministic stub lets this live CLI check reach the
    # pi-specific gate without depending on the operator's local sessions.
    HANDOFF_STUB_DIR=$(mktemp -d "${TMPDIR:-/tmp}/sch-pi-handoff.XXXXXX")
    cat > "${HANDOFF_STUB_DIR}/opencode" <<'EOF'
#!/bin/sh
if [ "$1" = "--version" ]; then
    printf '%s\n' 'verify-stub'
elif [ "$1" = "export" ]; then
    printf '%s\n' '{"info":{"id":"verify-pi-rejection"},"messages":[]}'
else
    exit 1
fi
EOF
    chmod +x "${HANDOFF_STUB_DIR}/opencode"

    check_pi_rejection web "pi.*no web UI" --harness pi --no-browser
    check_pi_rejection attach "pi.*no client/server split" --harness pi
    check_pi_rejection acp "pi.*no ACP agent" --harness pi
    check_pi_rejection handoff "harness='pi'.*supports only.*opencode" --session verify-pi-rejection
    rm -rf "${HANDOFF_STUB_DIR}"
    echo
fi

echo "############################################"
echo "# Multi-harness result: ${PASS} passed, ${FAIL} failed"
echo "############################################"
[ "${FAIL}" -eq 0 ] || exit 1
