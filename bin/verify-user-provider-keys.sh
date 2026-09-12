#!/bin/bash
# verify-user-provider-keys.sh — verification of the per-user provider API keys
# (spec: user-provider-keys; add-user-provider-keys tasks 5.2/5.3).
#
# Two sections, run independently:
#
#   offline  no AWS, no deployed runtime, no microVM: drives the REAL `sch` with
#            a stub `aws` on PATH that captures the invoke payload, and asserts
#            the client-side contract end-to-end — which keys reach the payload
#            from a real ~/.sch/env, the allowlist, the 0600 gate, the tolerant
#            parse, and the secrecy of every value in the command's own output.
#            This section is always runnable (CI, laptop, microVM).
#
#   live     needs a DEPLOYED runtime whose image already carries this
#            capability, `agentcore`, and AWS credentials with the same surface
#            as `sch`. Asserts the microVM half: the staged key set reaches the
#            session, the tmpfs staging file is 0600 and outside the
#            checkpointed mount, the harness dispatcher maps the canonical
#            names, removing a key really removes it, the claude
#            Bedrock/API reconciliation flips both ways, and no L2/S3 artifact
#            contains a key value.
#
# The live section MUTATES the workspace it is given (it opens sessions, submits
# a headless task and checkpoints). Use a disposable workspace name.
#
# Usage:
#   ./verify-user-provider-keys.sh                       # offline section only
#   ./verify-user-provider-keys.sh <workspace>           # offline + live
#   ./verify-user-provider-keys.sh <workspace> live      # live section only
#   ./verify-user-provider-keys.sh <workspace> live --harness claude
#
# Live section, optional inputs (never printed):
#   SCH_VERIFY_PROVIDER_KEY   a real key value to inject (default: a synthetic,
#                             non-working value — enough for every assertion
#                             here except an actual model call)
#   SCH_VERIFY_PROVIDER_NAME  which of the allowlisted names to use (default:
#                             OPENROUTER_API_KEY; BEDROCK_API_KEY exercises the
#                             cross-account bearer-token mapping of TASK-19)
set -uo pipefail

WS="${1:-}"
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
    SECTION=$([ -n "${WS}" ] && echo "full" || echo "offline")
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCH="${SCRIPT_DIR}/sch"
SCH_REGION="${SCH_REGION:-eu-west-1}"

PASS=0
FAIL=0
ok()  { echo "PASS: $*"; PASS=$((PASS+1)); }
bad() { echo "FAIL: $*"; FAIL=$((FAIL+1)); }

# A key-shaped value that is NOT a credential: every assertion below is about
# plumbing and secrecy, so a synthetic value is enough and nothing real leaks
# into a terminal or a CI log.
FAKE_KEY="sk-verify-0123456789abcdefghijKLMNOPQRSTUV"
FAKE_ROUTER="sk-or-v1-verify-0123456789abcdef"

# ============================================================================
# Offline section: ~/.sch/env -> invoke payload (no AWS)
# ============================================================================
offline_section() {
    echo "############################################"
    echo "# offline: ~/.sch/env -> invoke payload"
    echo "############################################"
    local root
    root="$(mktemp -d)"
    # shellcheck disable=SC2064
    trap "rm -rf '${root}'" RETURN

    # Stub `aws`: records every --payload it is given and answers the shim
    # contract (status/storage) so `sch` proceeds as it would for real.
    mkdir -p "${root}/bin"
    cat > "${root}/bin/aws" <<'STUB'
#!/bin/bash
# Stub AWS CLI: record invoke payloads, answer the runtime contract.
out=""
payload=""
prev=""
for arg in "$@"; do
    case "${prev}" in
        --payload) payload="${arg}" ;;
    esac
    out="${arg}"
    prev="${arg}"
done
case " $* " in
    *" invoke-agent-runtime "*)
        printf '%s\n' "${payload}" >> "${SCH_VERIFY_PAYLOAD_LOG}"
        printf '{"status": "ok", "action": "noop", "storage": "s3", "harness": "opencode", "task_id": "t-stub", "state": "running"}' > "${out}"
        ;;
    *" get-agent-runtime "*) echo "V1" ;;
    *" describe-stacks "*) echo "arn:aws:bedrock-agentcore:eu-west-1:111122223333:runtime/stub-runtime-id" ;;
    *) echo "{}" ;;
esac
exit 0
STUB
    chmod 755 "${root}/bin/aws"

    local home="${root}/home"
    mkdir -p "${home}/.sch"
    local log="${root}/payloads.jsonl"

    run_sch() { # <label> -> stdout+stderr of the command, payloads in $log
        : > "${log}"
        env -i \
            PATH="${root}/bin:/usr/bin:/bin" \
            HOME="${home}" \
            TMPDIR="${root}" \
            XDG_CONFIG_HOME="${home}/.config" \
            SCH_REGION="${SCH_REGION}" \
            SCH_RUNTIME_ARN="arn:aws:bedrock-agentcore:${SCH_REGION}:111122223333:runtime/stub" \
            SCH_CHECKPOINT_BUCKET="stub-bucket" \
            SCH_VERIFY_PAYLOAD_LOG="${log}" \
            "${SCH}" task "verify-keys-ws" "noop prompt" 2>&1
    }

    payload_keys() { # -> sorted names of provider_keys across all payloads
        python3 - "$@" <<'PY'
import json, sys
names = set()
for line in open(sys.argv[1], encoding="utf-8"):
    line = line.strip()
    if not line:
        continue
    try:
        data = json.loads(line)
    except ValueError:
        continue
    names.update((data.get("provider_keys") or {}))
print(",".join(sorted(names)))
PY
    }

    payload_count_with_keys() {
        python3 - "$1" <<'PY'
import json, sys
total = with_keys = 0
for line in open(sys.argv[1], encoding="utf-8"):
    line = line.strip()
    if not line:
        continue
    try:
        data = json.loads(line)
    except ValueError:
        continue
    total += 1
    if data.get("provider_keys"):
        with_keys += 1
print(f"{with_keys}/{total}")
PY
    }

    # --- A1: configured keys reach every payload of the command -------------
    cat > "${home}/.sch/env" <<EOF
# my keys
ANTHROPIC_API_KEY=${FAKE_KEY}
OPENROUTER_API_KEY=${FAKE_ROUTER}
EOF
    chmod 600 "${home}/.sch/env"
    local out
    out="$(run_sch)"
    if [ "$(payload_keys "${log}")" = "ANTHROPIC_API_KEY,OPENROUTER_API_KEY" ]; then
        ok "A1 configured keys reach the invoke payload"
    else
        bad "A1 payload provider_keys = '$(payload_keys "${log}")' (expected the two configured names)"
    fi
    local ratio
    ratio="$(payload_count_with_keys "${log}")"
    if [ "${ratio%%/*}" = "${ratio##*/}" ] && [ "${ratio%%/*}" != "0" ]; then
        ok "A1b every payload of the command carries the keys (${ratio})"
    else
        bad "A1b only ${ratio} payloads carry the keys (warmup or action left uncovered)"
    fi

    # --- A2: secrecy of the values in the command's own output --------------
    if echo "${out}" | grep -qF "${FAKE_KEY}" || echo "${out}" | grep -qF "${FAKE_ROUTER}"; then
        bad "A2 a key value appeared in the command output"
    else
        ok "A2 no key value in the command output"
    fi

    # --- A3: allowlist ------------------------------------------------------
    cat > "${home}/.sch/env" <<EOF
AWS_SECRET_ACCESS_KEY=must-not-travel
TELEGRAM_BOT_TOKEN=must-not-travel
SCH_REGION=eu-central-1
KILO_API_KEY=${FAKE_KEY}
EOF
    chmod 600 "${home}/.sch/env"
    run_sch >/dev/null
    if [ "$(payload_keys "${log}")" = "KILO_API_KEY" ] && ! grep -qF "must-not-travel" "${log}"; then
        ok "A3 only the allowlisted names reach the payload"
    else
        bad "A3 allowlist not enforced: $(payload_keys "${log}")"
    fi

    # --- A4: tolerant parse -------------------------------------------------
    cat > "${home}/.sch/env" <<EOF
this line has no equals sign
9INVALID=x
=novalue

export OPENCODE_API_KEY='${FAKE_KEY}'
KILO_API_KEY=
EOF
    chmod 600 "${home}/.sch/env"
    out="$(run_sch)"
    if [ "$(payload_keys "${log}")" = "OPENCODE_API_KEY" ]; then
        ok "A4 malformed lines skipped, valid ones forwarded, empty value dropped"
    else
        bad "A4 tolerant parse failed: $(payload_keys "${log}")"
    fi

    # --- A5: permission gate ------------------------------------------------
    cat > "${home}/.sch/env" <<EOF
ANTHROPIC_API_KEY=${FAKE_KEY}
EOF
    chmod 644 "${home}/.sch/env"
    out="$(run_sch)"
    if [ -z "$(payload_keys "${log}")" ] && echo "${out}" | grep -q "chmod 600"; then
        ok "A5 world-readable file: keys withheld, remedy printed, command proceeded"
    else
        bad "A5 permission gate failed (payload='$(payload_keys "${log}")')"
    fi
    if echo "${out}" | grep -qF "${FAKE_KEY}"; then
        bad "A5b the warning leaked the key value"
    else
        ok "A5b the warning names the file and the remedy, never the value"
    fi

    # --- A6: no file at all -------------------------------------------------
    rm -f "${home}/.sch/env"
    run_sch >/dev/null
    if [ -z "$(payload_keys "${log}")" ] && ! grep -q "provider_keys" "${log}"; then
        ok "A6 no file: the field is absent from the payload (Bedrock-only)"
    else
        bad "A6 a payload carried provider keys with no ~/.sch/env present"
    fi
}

# ============================================================================
# Live section: the microVM half
# ============================================================================
runtime_arn() {
    aws cloudformation describe-stacks \
        --stack-name "${SCH_PROJECT:-sch}-${SCH_ENV:-dev}-runtime" \
        --region "${SCH_REGION}" \
        --query "Stacks[0].Outputs[?OutputKey=='RuntimeArn'].OutputValue" \
        --output text
}

checkpoint_bucket() {
    aws cloudformation describe-stacks \
        --stack-name "${SCH_PROJECT:-sch}-${SCH_ENV:-dev}-runtime" \
        --region "${SCH_REGION}" \
        --query "Stacks[0].Outputs[?OutputKey=='CheckpointBucketName'].OutputValue" \
        --output text
}

session_id() {
    python3 - "${XDG_CONFIG_HOME:-${HOME}/.config}/sch/workspaces/${WS}" <<'PY'
import json, sys
try:
    raw = open(sys.argv[1], encoding="utf-8").read().strip()
except OSError:
    print("")
    raise SystemExit(0)
try:
    data = json.loads(raw)
except ValueError:
    print(raw)
else:
    print(data.get("runtimeSessionId") or data.get("sessionId") or "")
PY
}

live_info() { # -> path of a JSON file with the shim's `info` response
    local out="${TMPDIR:-/tmp}/sch-verify-keys-info-$$.json"
    aws bedrock-agentcore invoke-agent-runtime \
        --cli-binary-format raw-in-base64-out \
        --agent-runtime-arn "${ARN}" \
        --runtime-session-id "${SID}" \
        --payload "{\"action\": \"info\", \"workspace\": \"${WS}\", \"harness\": \"${HARNESS}\"}" \
        --region "${SCH_REGION}" \
        "${out}" >/dev/null 2>&1
    echo "${out}"
}

remote_stdout() { # <command string, single-quote free>
    agentcore exec \
        --runtime "${ARN}" \
        --session-id "${SID}" \
        --region "${SCH_REGION}" \
        --timeout 300 \
        --json \
        -- sh -c "'$1'" 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin).get("stdout",""), end="")'
}

live_section() {
    echo "############################################"
    echo "# live: microVM staging + dispatcher + reconciliation"
    echo "# workspace: ${WS} (harness: ${HARNESS})"
    echo "############################################"
    command -v agentcore >/dev/null || { bad "agentcore not on PATH"; return; }
    ARN="$(runtime_arn)" || { bad "cannot resolve the runtime ARN"; return; }
    local key_name="${SCH_VERIFY_PROVIDER_NAME:-OPENROUTER_API_KEY}"
    local key_value="${SCH_VERIFY_PROVIDER_KEY:-${FAKE_ROUTER}}"
    local env_file="${HOME}/.sch/env"
    local backup=""
    if [ -f "${env_file}" ]; then
        backup="${env_file}.verify-backup-$$"
        cp -p "${env_file}" "${backup}"
    fi
    restore_env_file() {
        if [ -n "${backup}" ]; then
            mv "${backup}" "${env_file}"
        else
            rm -f "${env_file}"
        fi
    }
    # shellcheck disable=SC2064
    trap restore_env_file RETURN

    # --- B1: a key configured by the user reaches the session ---------------
    mkdir -p "$(dirname "${env_file}")"
    printf '%s=%s\n' "${key_name}" "${key_value}" > "${env_file}"
    chmod 600 "${env_file}"
    "${SCH}" task "${WS}" --harness "${HARNESS}" "reply with the single word ok" >/dev/null 2>&1
    SID="$(session_id)"
    [ -n "${SID}" ] || { bad "no session id recorded for '${WS}'"; return; }
    local info
    info="$(live_info)"
    local staged
    staged="$(python3 -c "import json,sys; print(','.join(json.load(open(sys.argv[1])).get('provider_keys',{}).get('staged',[])))" "${info}" 2>/dev/null)"
    if [ "${staged}" = "SCH_${key_name}" ]; then
        ok "B1 the shim staged exactly the configured key (${staged})"
    else
        bad "B1 staged set is '${staged}' (expected SCH_${key_name})"
    fi
    if grep -qF "${key_value}" "${info}"; then
        bad "B2 the info response contains a key VALUE"
    else
        ok "B2 the info response names the key without its value"
    fi
    local staging_file
    staging_file="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('provider_keys',{}).get('staging_file',''))" "${info}" 2>/dev/null)"
    case "${staging_file}" in
        /run/*) ok "B3 the staging file is on tmpfs (${staging_file})" ;;
        *) bad "B3 unexpected staging path '${staging_file}' (must be under /run)" ;;
    esac

    # --- B4: file permissions + dispatcher mapping, from inside the microVM --
    local mode
    mode="$(remote_stdout "stat -c %a ${staging_file} 2>/dev/null")"
    if [ "${mode}" = "600" ]; then
        ok "B4 the staging file is 0600"
    else
        bad "B4 staging file mode is '${mode}' (expected 600)"
    fi
    local staged_line
    staged_line="$(remote_stdout "grep -c ^SCH_${key_name}= ${staging_file} 2>/dev/null")"
    if [ "${staged_line}" = "1" ]; then
        ok "B5 the staging file carries SCH_${key_name} (dispatcher input present)"
    else
        bad "B5 SCH_${key_name} not found in the staging file"
    fi
    # The dispatcher is the only thing that maps SCH_* onto the canonical name.
    # Drive the REAL wrapper with `env` as its "real binary" (same trick as the
    # unit tests) so the mapping is observed rather than assumed — and, at the
    # same time, prove a shell that reaches the wrapper WITHOUT the container ENV
    # still gets the key from the tmpfs file.
    # TASK-19: the Bedrock API key is the one name whose canonical form differs
    # from the staged name — BEDROCK_API_KEY maps onto AWS_BEARER_TOKEN_BEDROCK.
    local canonical="${key_name}"
    case "${key_name}" in
        BEDROCK_API_KEY) canonical="AWS_BEARER_TOKEN_BEDROCK" ;;
    esac
    local mapped
    mapped="$(remote_stdout "SCH_HARNESS_REAL=/usr/bin/env SCH_HARNESS=${HARNESS} SCH_HARNESS_WAIT=0 /usr/local/bin/${HARNESS} 2>/dev/null | grep -c ^${canonical}=")"
    if [ "${mapped}" = "1" ]; then
        ok "B5b the dispatcher mapped SCH_${key_name} onto ${canonical} for ${HARNESS}"
    else
        bad "B5b the dispatcher did not export ${canonical} for ${HARNESS} (matches=${mapped})"
    fi

    # --- B6: removing the key really removes it ----------------------------
    : > "${env_file}"
    chmod 600 "${env_file}"
    "${SCH}" status "${WS}" --live >/dev/null 2>&1
    info="$(live_info)"
    staged="$(python3 -c "import json,sys; print(','.join(json.load(open(sys.argv[1])).get('provider_keys',{}).get('staged',[])))" "${info}" 2>/dev/null)"
    if [ -z "${staged}" ]; then
        ok "B6 removing the key from ~/.sch/env clears the staged set"
    else
        bad "B6 staged set survived the removal: '${staged}'"
    fi

    # --- B7: no key value in the L2 checkpoint artifacts --------------------
    "${SCH}" stop "${WS}" >/dev/null 2>&1
    local bucket
    bucket="$(checkpoint_bucket)"
    if [ -n "${bucket}" ] && [ "${bucket}" != "None" ]; then
        local dump
        dump="$(mktemp -d)"
        aws s3 cp "s3://${bucket}/checkpoints/${WS}/" "${dump}/" \
            --recursive --region "${SCH_REGION}" >/dev/null 2>&1
        if grep -RqF "${key_value}" "${dump}" 2>/dev/null; then
            bad "B7 a key value was found in the L2 checkpoint artifacts"
        else
            ok "B7 no key value in any L2/S3 checkpoint artifact"
        fi
        rm -rf "${dump}"
    else
        echo "     (skipped B7: no checkpoint bucket resolved)"
    fi

    # --- B8: claude reconciliation, both directions ------------------------
    if [ "${HARNESS}" = "claude" ]; then
        printf 'ANTHROPIC_API_KEY=%s\n' "${FAKE_KEY}" > "${env_file}"
        chmod 600 "${env_file}"
        "${SCH}" task "${WS}" --harness claude "reply ok" >/dev/null 2>&1
        SID="$(session_id)"
        local setting
        setting="$(remote_stdout "python3 -c \"import json;print(json.load(open('/home/sch/.claude/settings.json'))['env']['CLAUDE_CODE_USE_BEDROCK'])\"")"
        if [ "${setting}" = "0" ]; then
            ok "B8 with an Anthropic key the seeding switches settings.json to the API"
        else
            bad "B8 settings.json env.CLAUDE_CODE_USE_BEDROCK='${setting}' (expected 0)"
        fi
        : > "${env_file}"
        chmod 600 "${env_file}"
        "${SCH}" stop "${WS}" >/dev/null 2>&1
        "${SCH}" reset-session "${WS}" --yes >/dev/null 2>&1 || true
        "${SCH}" task "${WS}" --harness claude "reply ok" >/dev/null 2>&1
        SID="$(session_id)"
        setting="$(remote_stdout "python3 -c \"import json;print(json.load(open('/home/sch/.claude/settings.json'))['env']['CLAUDE_CODE_USE_BEDROCK'])\"")"
        if [ "${setting}" = "1" ]; then
            ok "B9 without the key the next bootstrap restores Bedrock"
        else
            bad "B9 settings.json env.CLAUDE_CODE_USE_BEDROCK='${setting}' (expected 1)"
        fi
        local approved
        approved="$(remote_stdout "python3 -c \"import json,os;p='/home/sch/.claude/.claude.json';d=json.load(open(p)) if os.path.exists(p) else {};print(len(d.get('customApiKeyResponses',{}).get('approved',[])))\"")"
        if [ "${approved}" = "0" ]; then
            ok "B10 the pre-approved key suffix was withdrawn"
        else
            bad "B10 ${approved} approval(s) survived the key removal"
        fi
    else
        echo "     (skipped B8-B10: claude-only, re-run with --harness claude)"
    fi
}

case "${SECTION}" in
    offline) offline_section ;;
    live)
        [ -n "${WS}" ] || { echo "usage: $0 <workspace> live"; exit 2; }
        live_section
        ;;
    full)
        offline_section
        echo
        live_section
        ;;
    *) echo "unknown section '${SECTION}' (offline|live|full)" >&2; exit 2 ;;
esac

echo
echo "############################################"
echo "# user-provider-keys result: ${PASS} passed, ${FAIL} failed"
echo "############################################"
[ "${FAIL}" -eq 0 ] || exit 1
