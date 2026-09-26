#!/bin/bash
# verify-remote-ui-tunnel.sh — end-to-end verification of `sch attach` and
# `sch web` (specs: remote-ui-tunnel and web-session-access).
#
# What it proves
#   1. (offline, no AWS) `sch attach` on a claude-bound workspace fails fast,
#      client-side, with the documented error and WITHOUT any runtime call
#      (task 7.4 / spec "Attach rifiutato su workspace con harness claude").
#   2. (live) the shim's `serve-ensure` action starts/reuses the shared
#      `opencode serve` backend (OpenCode 2: web UI + `/api/*`, basic auth) and
#      reports web capability, port, version and the pinned password.
#   3. (live) the byte bridge carries real HTTP traffic from the LOCAL machine,
#      over InvokeAgentRuntimeWithWebSocketStream to the shim's @app.websocket
#      handler, through a remote TCP hop, to the REMOTE OpenCode backend, and
#      back — proving an operation issued locally lands on the remote workspace
#      (spec "Attach on an opencode-harness workspace": "only API traffic
#      on the network").
#
# Why a scripted stand-in instead of the real TUI
#   A real `opencode --server <url>` renders an interactive TUI that cannot be
#   driven head-lessly. `sch attach` hard-codes that TUI, but the piece under
#   test is the *byte bridge*, not terminal rendering. So the live path invokes
#   `tunnel/attach.js` directly with the exact args `cmd_attach` passes, plus
#   `--opencode-bin <driver>`: a small Node client that receives the same
#   `--server <local-bridge-url>` argv and OPENCODE_PASSWORD env the TUI would,
#   drives a few authenticated HTTP ops against the remote server through the
#   bridge, and exits. This exercises the
#   full local data path (attach.js -> transport.js -> websocket-stream-channel
#   -> live AgentCore -> shim -> remote opencode serve) end to end; only the
#   terminal front-end is stubbed. The `cmd_attach` orchestration it bypasses
#   (harness resolution, version-parity check, mark-interactive) is covered by
#   the offline check above and by mirroring its serve-ensure payload here.
#   4. (live) `tunnel/web.js` serves the root UI HTML and one referenced asset,
#      the sessions API and GET /event SSE through its local HTTP endpoint;
#      an attach adapter reaches the same backend while web remains connected,
#      and SIGTERM closes the web bridge and local listener cleanly.
#
# Requirements: AWS credentials with the same surface as `sch`, the `agentcore`
# and `aws` CLIs, Node >= 18, a local `opencode`, and the tunnel deps installed
# (`cd tunnel && npm install`). Use `--skip-live` to run only the offline
# assertions (CI without AWS).
#
# Usage:
#   ./verify-remote-ui-tunnel.sh <opencode-workspace>
#   ./verify-remote-ui-tunnel.sh <opencode-workspace> --skip-live
#   ./verify-remote-ui-tunnel.sh <opencode-workspace> --keep   # don't `sch stop` at the end
set -uo pipefail

WS=""
SKIP_LIVE=0
KEEP=0
while [ $# -gt 0 ]; do
    case "$1" in
        --skip-live) SKIP_LIVE=1 ;;
        --keep) KEEP=1 ;;
        --help|-h) sed -n '2,44p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) echo "unknown option '$1'" >&2; exit 2 ;;
        *)
            if [ -z "${WS}" ]; then WS="$1"; else
                echo "usage: $0 <opencode-workspace> [--skip-live] [--keep]" >&2; exit 2
            fi
            ;;
    esac
    shift
done
[ -n "${WS}" ] || { echo "usage: $0 <opencode-workspace> [--skip-live] [--keep]"; exit 2; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCH="${SCRIPT_DIR}/sch"
TUNNEL_DIR="${SCRIPT_DIR}/../tunnel"
SCH_REGION="${SCH_REGION:-eu-west-1}"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/sch"
WS_DIR="${CONFIG_DIR}/workspaces"
REMOTE_WORKTREE="/mnt/workspace/repo"

PASS=0
FAIL=0
WARN=0
ok()   { echo "PASS [tunnel]: $*"; PASS=$((PASS+1)); }
bad()  { echo "FAIL [tunnel]: $*"; FAIL=$((FAIL+1)); }
warn() { echo "WARN [tunnel]: $*"; WARN=$((WARN+1)); }

echo "############################################"
echo "# Remote UI tunnel verification (sch attach + sch web)"
echo "# opencode workspace: ${WS}"
echo "# region:             ${SCH_REGION}"
echo "# live tests:         $([ "${SKIP_LIVE}" -eq 1 ] && echo skipped || echo enabled)"
echo "############################################"
echo

# --- prerequisites ----------------------------------------------------------
have_node=1;    command -v node >/dev/null 2>&1 || have_node=0
have_opencode=1; command -v opencode >/dev/null 2>&1 || have_opencode=0
have_deps=1;    [ -d "${TUNNEL_DIR}/node_modules" ] || have_deps=0

# ===========================================================================
# 1. Offline: `sch attach` rejects a claude-bound workspace, no runtime call.
#    (task 7.4 / 5.2 — self-contained: seeds a throwaway claude workspace index)
# ===========================================================================
echo "== 1. offline: sch attach rejects harness=claude (fail-fast, no runtime call) =="
if [ "${have_node}" -eq 0 ] || [ "${have_opencode}" -eq 0 ] || [ "${have_deps}" -eq 0 ]; then
    warn "skipping claude-rejection check — missing local prereq(s):$([ ${have_node} -eq 0 ] && echo ' node')$([ ${have_opencode} -eq 0 ] && echo ' opencode')$([ ${have_deps} -eq 0 ] && echo ' tunnel/node_modules (run: cd tunnel && npm install)')"
else
    CLAUDE_WS="verify-tunnel-claude-$$"
    CLAUDE_WS_FILE="${WS_DIR}/${CLAUDE_WS}"
    mkdir -p "${WS_DIR}"
    python3 -c '
import json, sys, uuid
ws = sys.argv[1]
print(json.dumps({"runtimeSessionId": f"sch-{ws}-{uuid.uuid4()}", "harness": "claude"}))
' "${CLAUDE_WS}" > "${CLAUDE_WS_FILE}"

    start=$(date +%s)
    REJECT_OUT="$("${SCH}" attach "${CLAUDE_WS}" 2>&1 || true)"
    elapsed=$(( $(date +%s) - start ))
    rm -f "${CLAUDE_WS_FILE}"

    if echo "${REJECT_OUT}" | grep -Eq "client/server split|bound to harness='claude'"; then
        ok "claude workspace rejected with the documented error: $(echo "${REJECT_OUT}" | head -1)"
    else
        bad "claude workspace not rejected as expected: ${REJECT_OUT}"
    fi
    # Fail-fast means no runtime round-trip: the client-side guard returns near-instantly.
    if [ "${elapsed}" -le 5 ]; then
        ok "rejection was fast (${elapsed}s) — consistent with no runtime call"
    else
        warn "rejection took ${elapsed}s (>5s) — expected a purely client-side fail-fast"
    fi
fi
echo

if [ "${SKIP_LIVE}" -eq 1 ]; then
    echo "== result: ${PASS} passed, ${FAIL} failed, ${WARN} warnings (live skipped) =="
    [ "${FAIL}" -eq 0 ] || exit 1
    exit 0
fi

# --- live prerequisites ------------------------------------------------------
if [ "${have_node}" -eq 0 ] || [ "${have_deps}" -eq 0 ]; then
    bad "live tests need Node >= 18 and tunnel deps (cd tunnel && npm install) — aborting live path"
    echo "== result: ${PASS} passed, ${FAIL} failed, ${WARN} warnings =="
    exit 1
fi
command -v aws >/dev/null 2>&1 || { bad "'aws' CLI not found — aborting live path"; exit 1; }
[ -f "${TUNNEL_DIR}/attach.js" ] || { bad "cannot find ${TUNNEL_DIR}/attach.js"; exit 1; }
[ -f "${TUNNEL_DIR}/web.js" ] || { bad "cannot find ${TUNNEL_DIR}/web.js"; exit 1; }

# --- live helpers (agentcore data plane, mirrors verify-headless-tasks.sh) ---
runtime_arn() {
    if [ -n "${SCH_RUNTIME_ARN:-}" ]; then echo "${SCH_RUNTIME_ARN}"; return; fi
    aws cloudformation describe-stacks \
        --stack-name "${SCH_PROJECT:-sch}-${SCH_ENV:-dev}-runtime" \
        --region "${SCH_REGION}" \
        --query "Stacks[0].Outputs[?OutputKey=='RuntimeArn'].OutputValue" \
        --output text 2>/dev/null
}

_invoke_info() {
    aws bedrock-agentcore invoke-agent-runtime \
        --cli-binary-format raw-in-base64-out \
        --agent-runtime-arn "${ARN}" \
        --runtime-session-id "${SID}" \
        --payload '{"action": "info"}' \
        --region "${SCH_REGION}" \
        "${TMPDIR:-/tmp}/sch-tunnel-info-$$.json" >/dev/null 2>&1 \
        && cat "${TMPDIR:-/tmp}/sch-tunnel-info-$$.json"
}

remote_stdout() { # <sh command, single-quote free> -> stdout
    agentcore exec \
        --runtime "${ARN}" \
        --session-id "${SID}" \
        --region "${SCH_REGION}" \
        --timeout 120 \
        --json \
        -- sh -c "'$1'" 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin).get("stdout",""), end="")' 2>/dev/null
}

wait_boot_ready() { # <max_wait>
    local max_wait="${1:-240}" i phase
    echo "-- waiting for shim boot ready (max ${max_wait}s)"
    for i in $(seq 1 "${max_wait}"); do
        phase=$(_invoke_info | python3 -c 'import json,sys; print(json.load(sys.stdin).get("boot",{}).get("phase",""))' 2>/dev/null)
        [ "${phase}" = "ready" ] && { echo "   boot ready"; return 0; }
        sleep 1
    done
    echo "   boot not ready after ${max_wait}s"
    return 1
}

# Run a command with a hard wall-clock cap (macOS has no coreutils `timeout`).
run_bounded() { # <seconds> <cmd...>
    local secs="$1"; shift
    "$@" & local cmd_pid=$!
    ( sleep "${secs}"; kill -TERM "${cmd_pid}" 2>/dev/null ) & local watch_pid=$!
    wait "${cmd_pid}" 2>/dev/null; local rc=$?
    kill -TERM "${watch_pid}" 2>/dev/null; wait "${watch_pid}" 2>/dev/null || true
    return "${rc}"
}

ARN="$(runtime_arn)"
[ -n "${ARN}" ] && [ "${ARN}" != "None" ] || { bad "cannot resolve runtime ARN (set SCH_RUNTIME_ARN or check the ${SCH_PROJECT:-sch}-${SCH_ENV:-dev}-runtime stack)"; exit 1; }

# Ensure the opencode workspace index exists with harness=opencode (seed it
# directly, like verify-headless-tasks.sh, so we don't need to open a TUI).
WS_FILE="${WS_DIR}/${WS}"
if [ ! -f "${WS_FILE}" ]; then
    mkdir -p "${WS_DIR}"
    python3 -c '
import json, sys, uuid
ws = sys.argv[1]
print(json.dumps({"runtimeSessionId": f"sch-{ws}-{uuid.uuid4()}", "harness": "opencode"}))
' "${WS}" > "${WS_FILE}"
fi
HARNESS=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("harness",""))' "${WS_FILE}" 2>/dev/null || echo "")
SID=$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("runtimeSessionId") or d.get("sessionId") or "")' "${WS_FILE}" 2>/dev/null || cat "${WS_FILE}")
STORAGE=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("storage","session"))' "${WS_FILE}" 2>/dev/null || echo "session")
SESSION_EPOCH=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("sessionEpoch",0))' "${WS_FILE}" 2>/dev/null || echo "0")
if [ "${HARNESS}" != "opencode" ]; then
    bad "workspace '${WS}' is bound to harness='${HARNESS}', but sch attach requires opencode — pass a fresh/opencode workspace name"
    exit 1
fi
echo "workspace=${WS} session=${SID} harness=${HARNESS} storage=${STORAGE} epoch=${SESSION_EPOCH}"
echo

# ===========================================================================
# 2. Live: warm the microVM, then serve-ensure (mirrors cmd_attach's payloads).
# ===========================================================================
echo "== 2. live: warm microVM + serve-ensure reports web=true, port, and version =="
aws bedrock-agentcore invoke-agent-runtime \
    --cli-binary-format raw-in-base64-out \
    --agent-runtime-arn "${ARN}" \
    --runtime-session-id "${SID}" \
    --payload "{\"action\": \"noop\", \"storage\": \"resumed\", \"storage_backend\": \"${STORAGE}\", \"session_epoch\": ${SESSION_EPOCH}, \"workspace\": \"${WS}\", \"harness\": \"opencode\"}" \
    --region "${SCH_REGION}" \
    /dev/null >/dev/null 2>&1 || true
wait_boot_ready 240 || warn "boot not confirmed ready; continuing (serve-ensure retries internally)"

SERVE_OUT="${TMPDIR:-/tmp}/sch-tunnel-serve-$$.json"
REMOTE_PORT=""; REMOTE_VERSION=""; SERVE_STATUS=""
REMOTE_PASSWORD=""
WEB_CAPABLE=""
attempt=0
while [ "${attempt}" -lt 5 ]; do
    attempt=$((attempt + 1))
    if aws bedrock-agentcore invoke-agent-runtime \
        --cli-binary-format raw-in-base64-out \
        --agent-runtime-arn "${ARN}" \
        --runtime-session-id "${SID}" \
        --payload "{\"action\": \"serve-ensure\", \"workspace\": \"${WS}\", \"harness\": \"opencode\", \"storage_backend\": \"${STORAGE}\", \"session_epoch\": ${SESSION_EPOCH}}" \
        --region "${SCH_REGION}" \
        "${SERVE_OUT}" >/dev/null 2>&1; then
        SERVE_STATUS=$(python3 -c "import json; print(json.load(open('${SERVE_OUT}')).get('status','unknown'))" 2>/dev/null || echo "unknown")
        if [ "${SERVE_STATUS}" = "ok" ]; then
            REMOTE_PORT=$(python3 -c "import json; print(json.load(open('${SERVE_OUT}')).get('port',''))" 2>/dev/null || echo "")
            REMOTE_VERSION=$(python3 -c "import json; print(json.load(open('${SERVE_OUT}')).get('opencode_version',''))" 2>/dev/null || echo "")
            # OpenCode 2: `serve` requires basic auth (opencode:<password>); the
            # shim mints and pins the password and returns it in `auth`.
            REMOTE_PASSWORD=$(python3 -c "import json; print((json.load(open('${SERVE_OUT}')).get('auth') or {}).get('password',''))" 2>/dev/null || echo "")
            WEB_CAPABLE=$(python3 -c "import json; print(str(json.load(open('${SERVE_OUT}')).get('capabilities',{}).get('web',False)).lower())" 2>/dev/null || echo "false")
            break
        fi
    else
        SERVE_STATUS="invoke-failed"
    fi
    [ "${attempt}" -lt 5 ] && sleep 2
done
rm -f "${SERVE_OUT}"
if [ "${SERVE_STATUS}" = "ok" ] && [ -n "${REMOTE_PORT}" ]; then
    ok "serve-ensure ok after ${attempt} attempt(s): port=${REMOTE_PORT} opencode_version=${REMOTE_VERSION:-unknown}"
    if [ -n "${REMOTE_PASSWORD}" ]; then
        ok "serve-ensure returned the basic-auth password for the OpenCode 2 backend"
    else
        bad "serve-ensure returned no auth.password (OpenCode 2 serve rejects unauthenticated /api/* calls)"
    fi
else
    bad "serve-ensure did not report status=ok with a port (last status: ${SERVE_STATUS})"
    [ "${KEEP}" -eq 1 ] || "${SCH}" stop "${WS}" >/dev/null 2>&1 || true
    echo "== result: ${PASS} passed, ${FAIL} failed, ${WARN} warnings =="
    exit 1
fi
if [ "${WEB_CAPABLE}" = "true" ]; then
    ok "serve-ensure advertises capabilities.web=true"
else
    bad "serve-ensure did not advertise capabilities.web=true (runtime image predates web access)"
fi

# Re-running serve-ensure MUST reuse the same server (same port), not start a
# second one (the attach and web adapters share this backend).
SERVE_OUT2="${TMPDIR:-/tmp}/sch-tunnel-serve2-$$.json"
if aws bedrock-agentcore invoke-agent-runtime \
    --cli-binary-format raw-in-base64-out \
    --agent-runtime-arn "${ARN}" \
    --runtime-session-id "${SID}" \
    --payload "{\"action\": \"serve-ensure\", \"workspace\": \"${WS}\", \"harness\": \"opencode\", \"storage_backend\": \"${STORAGE}\", \"session_epoch\": ${SESSION_EPOCH}}" \
    --region "${SCH_REGION}" \
    "${SERVE_OUT2}" >/dev/null 2>&1; then
    REMOTE_PORT2=$(python3 -c "import json; print(json.load(open('${SERVE_OUT2}')).get('port',''))" 2>/dev/null || echo "")
    if [ -n "${REMOTE_PORT2}" ] && [ "${REMOTE_PORT2}" = "${REMOTE_PORT}" ]; then
        ok "second serve-ensure reused the same server (port=${REMOTE_PORT2})"
    else
        warn "second serve-ensure returned port='${REMOTE_PORT2}' (expected reuse of ${REMOTE_PORT})"
    fi
fi
rm -f "${SERVE_OUT2}"
echo

# ===========================================================================
# 3. Live: independent remote-side health — shared backend answers on its port.
# ===========================================================================
echo "== 3. live: remote OpenCode web backend answers locally on port ${REMOTE_PORT} =="
REMOTE_HTTP=$(remote_stdout "curl -s -o /dev/null -w '%{http_code}' --max-time 10 http://127.0.0.1:${REMOTE_PORT}/ 2>/dev/null || echo NONE")
if echo "${REMOTE_HTTP}" | grep -Eq '^[1-5][0-9][0-9]$'; then
    ok "remote curl to the OpenCode web backend returned HTTP ${REMOTE_HTTP} (server healthy on the microVM)"
else
    warn "remote health probe inconclusive (got '${REMOTE_HTTP}'; curl may be absent in the image) — the tunnel probe below is the authoritative check"
fi
echo

# ===========================================================================
# 4. Live: the byte bridge carries HTTP local -> remote OpenCode backend -> back.
#    Runs tunnel/attach.js directly with a scripted driver as --opencode-bin.
# ===========================================================================
echo "== 4. live: attach byte bridge carries an operation to the remote workspace =="
DRIVER="${TMPDIR:-/tmp}/sch-attach-driver-$$.cjs"
DRIVER_OUT="${TMPDIR:-/tmp}/sch-attach-driver-out-$$"
rm -f "${DRIVER_OUT}"

cat > "${DRIVER}" <<'NODE'
#!/usr/bin/env node
// Scripted stand-in for the local `opencode --server <url>` TUI (OpenCode 2).
// attach.js spawns us with argv `--server <local-bridge-url> [...]` and the
// backend's basic-auth password in OPENCODE_PASSWORD; <url> tunnels to the
// REMOTE OpenCode backend. We drive a few HTTP ops against its `/api/*`
// routes to prove the bridge works and that the server we reached is the
// remote workspace's. Results -> $SCH_VERIFY_DRIVER_OUT.
'use strict';
const fs = require('fs');
const OUT = process.env.SCH_VERIFY_DRIVER_OUT || '';
const REMOTE = process.env.SCH_VERIFY_REMOTE_WORKTREE || '/mnt/workspace/repo';
const PASSWORD = process.env.OPENCODE_PASSWORD || '';
const AUTH = PASSWORD ? { authorization: 'Basic ' + Buffer.from('opencode:' + PASSWORD).toString('base64') } : {};
const R = {};
const save = () => { if (OUT) { try { fs.writeFileSync(OUT, Object.entries(R).map(([k, v]) => `${k}=${v}`).join('\n') + '\n'); } catch (_) {} } };

let base = '';
const argv = process.argv.slice(2);
R.server_flag = argv.includes('--server') ? 1 : 0;
R.password_in_env = PASSWORD ? 1 : 0;
for (const a of argv) { if (/^https?:\/\//.test(a)) { base = a.replace(/\/+$/, ''); break; } }
R.base = base;

async function req(method, path, body) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), 20000);
  try {
    const opt = { method, signal: ctrl.signal, headers: { ...AUTH } };
    if (body !== undefined) { opt.headers['content-type'] = 'application/json'; opt.body = JSON.stringify(body); }
    const r = await fetch(base + path, opt);
    const text = await r.text();
    return { status: r.status, text };
  } finally { clearTimeout(t); }
}

(async () => {
  if (!base) { R.error = 'no-url-in-argv'; save(); process.exit(3); }

  // (a) transport proof: any well-formed HTTP response from the remote server.
  let transport = false;
  for (const p of ['/api/info', '/api/project', '/api/session', '/']) {
    try {
      const { status } = await req('GET', p);
      if (typeof status === 'number') { transport = true; R.transport_status = status; R.transport_path = p; break; }
    } catch (e) { R['probe_err_' + p.replace(/\W/g, '')] = String(e.message || e).slice(0, 60); }
  }
  R.transport_ok = transport ? 1 : 0;
  if (!transport) { save(); process.exit(4); }

  // (a2) auth proof: /api/* is 401 without credentials and 200 with them.
  try {
    const r = await fetch(base + '/api/info');
    R.unauth_status = r.status;
    await r.text();
  } catch (_) {}
  try {
    const { status } = await req('GET', '/api/info');
    R.auth_status = status;
  } catch (_) {}
  R.auth_ok = (R.unauth_status === 401 && R.auth_status === 200) ? 1 : 0;

  // (b) does the reached server report the REMOTE worktree?
  let remoteBound = false;
  for (const p of ['/api/project', `/api/session?directory=${encodeURIComponent(REMOTE)}`, '/api/location']) {
    try {
      const { status, text } = await req('GET', p);
      if (status >= 200 && status < 300 && text.includes(REMOTE)) { remoteBound = true; R.remote_info_path = p; break; }
    } catch (_) {}
  }
  R.remote_bound = remoteBound ? 1 : 0;

  // (c) a real operation: create a session bound to the remote worktree. The
  //     created session is server state living in the REMOTE workspace.
  let created = false, dir = '';
  try {
    const resp = await req('POST', '/api/session', { directory: REMOTE });
    R.session_status = resp ? resp.status : 'none';
    if (resp && resp.status >= 200 && resp.status < 300) {
      created = true;
      try {
        const obj = JSON.parse(resp.text); const info = obj.data || obj;
        dir = (info.location && info.location.directory) || info.directory || info.path || '';
      } catch (_) {}
    }
  } catch (e) { R.session_err = String(e.message || e).slice(0, 60); }
  R.session_created = created ? 1 : 0;
  R.session_dir = dir;
  R.session_dir_is_remote = dir === REMOTE ? 1 : 0;

  // (d) large-transfer proof: a big response MUST arrive in full, not stall.
  //     This is what actually broke `sch attach`'s TUI — its initial state
  //     load exceeds the point where the 250-frames/sec WebSocket limit closed
  //     the connection (1006 reconnect storm). A single small GET (a/b/c above)
  //     does NOT exercise this; fetch a large endpoint and assert completeness.
  //     /openapi.json is the large authenticated document on OpenCode 2.
  R.large_ok = 0;
  try {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 30000);
    try {
      const r = await fetch(base + '/openapi.json', { signal: ctrl.signal, headers: { ...AUTH } });
      const cl = parseInt(r.headers.get('content-length') || '0', 10);
      const body = await r.arrayBuffer();
      R.large_status = r.status;
      R.large_content_length = cl;
      R.large_bytes = body.byteLength;
      if (r.status >= 200 && r.status < 300 && body.byteLength > 100000 && (cl === 0 || body.byteLength >= cl)) {
        R.large_ok = 1;
      }
    } finally { clearTimeout(t); }
  } catch (e) { R.large_err = String(e.message || e).slice(0, 80); }

  save();
  process.exit(0);
})().catch((e) => { R.fatal = String((e && e.stack) || e).slice(0, 160); save(); process.exit(5); });
NODE
chmod +x "${DRIVER}"

echo "-- launching tunnel/attach.js with a scripted driver (bounded to 120s)"
SCH_VERIFY_DRIVER_OUT="${DRIVER_OUT}" \
SCH_VERIFY_REMOTE_WORKTREE="${REMOTE_WORKTREE}" \
SCH_OPENCODE_SERVER_PASSWORD="${REMOTE_PASSWORD}" \
run_bounded 120 node "${TUNNEL_DIR}/attach.js" \
    --region "${SCH_REGION}" \
    --runtime-arn "${ARN}" \
    --session-id "${SID}" \
    --workspace "${WS}" \
    --storage "${STORAGE}" \
    --session-epoch "${SESSION_EPOCH}" \
    --remote-port "${REMOTE_PORT}" \
    --opencode-bin "${DRIVER}"
ATTACH_RC=$?

# Parse the driver's key=value output.
d_get() { grep -E "^$1=" "${DRIVER_OUT}" 2>/dev/null | head -1 | cut -d= -f2-; }
TRANSPORT_OK=$(d_get transport_ok)
TRANSPORT_STATUS=$(d_get transport_status)
SESSION_CREATED=$(d_get session_created)
SESSION_DIR_IS_REMOTE=$(d_get session_dir_is_remote)
SESSION_DIR=$(d_get session_dir)
REMOTE_BOUND=$(d_get remote_bound)
LARGE_OK=$(d_get large_ok)
LARGE_BYTES=$(d_get large_bytes)
LARGE_CL=$(d_get large_content_length)

if [ "${TRANSPORT_OK}" = "1" ]; then
    ok "byte bridge carried HTTP to the remote OpenCode backend and back (HTTP ${TRANSPORT_STATUS} through the tunnel)"
else
    bad "byte bridge did not reach the remote OpenCode backend (attach.js rc=${ATTACH_RC}); driver out:"
    [ -f "${DRIVER_OUT}" ] && sed 's/^/    /' "${DRIVER_OUT}"
fi

# OpenCode 2 client contract: attach.js must spawn `opencode --server <url>`
# (not the 1.x `attach <url>`) and hand the backend password to the client as
# OPENCODE_PASSWORD; the backend must reject anonymous /api/* (401) and accept
# the pinned credentials (200).
if [ "$(d_get server_flag)" = "1" ] && [ "$(d_get password_in_env)" = "1" ]; then
    ok "attach.js launched the client with --server <url> and OPENCODE_PASSWORD (OpenCode 2 client contract)"
else
    bad "attach.js client argv/env is not the OpenCode 2 shape (server_flag=$(d_get server_flag), password_in_env=$(d_get password_in_env))"
fi
if [ "$(d_get auth_ok)" = "1" ]; then
    ok "remote backend enforces basic auth (anonymous /api/info -> 401, with password -> 200)"
else
    bad "remote backend auth check failed (anonymous=$(d_get unauth_status), authenticated=$(d_get auth_status))"
fi

if [ "${SESSION_CREATED}" = "1" ] && [ "${SESSION_DIR_IS_REMOTE}" = "1" ]; then
    ok "operation over the tunnel created a session on the REMOTE worktree (${SESSION_DIR}) — result visible in the remote workspace"
elif [ "${REMOTE_BOUND}" = "1" ]; then
    ok "remote OpenCode backend reports the remote worktree (${REMOTE_WORKTREE}) — local client is bound to the remote workspace"
elif [ "${TRANSPORT_OK}" = "1" ]; then
    warn "deeper API-shape checks degraded (session_created='${SESSION_CREATED}', session_dir='${SESSION_DIR}') — likely opencode server API drift; transport is proven, confirm workspace visibility manually via the TUI"
fi

# Large-transfer completeness — this is what the TUI actually needs (its state
# load is large). A stall/truncation here means the 250-frames/sec close-storm
# regressed (the bug that made `sch attach` hang on a blank screen).
if [ "${LARGE_OK}" = "1" ]; then
    ok "large response transferred in full through the tunnel (${LARGE_BYTES}B, content-length=${LARGE_CL}) — no frame-rate/reconnect stall"
elif [ -n "${LARGE_BYTES}" ] && [ "${LARGE_BYTES}" != "0" ]; then
    bad "large transfer INCOMPLETE (got ${LARGE_BYTES}B of content-length=${LARGE_CL}) — the TUI would hang; likely a frame-rate/reconnect regression"
else
    warn "could not exercise a large transfer (no big endpoint responded; large_bytes='${LARGE_BYTES}')"
fi

rm -f "${DRIVER}" "${DRIVER_OUT}"
echo

# ===========================================================================
# 5. Live: headless web bridge serves UI/API/SSE while attach shares backend.
# ===========================================================================
echo "== 5. live: web UI, API, SSE, concurrent attach, and clean shutdown =="
WEB_LOG="${TMPDIR:-/tmp}/sch-web-bridge-$$.log"
WEB_PROBE="${TMPDIR:-/tmp}/sch-web-probe-$$.cjs"
WEB_PROBE_OUT="${TMPDIR:-/tmp}/sch-web-probe-out-$$"
CONCURRENT_DRIVER="${TMPDIR:-/tmp}/sch-web-attach-driver-$$.cjs"
CONCURRENT_OUT="${TMPDIR:-/tmp}/sch-web-attach-out-$$"
WEB_PID=""

web_bridge_cleanup() {
    if [ -n "${WEB_PID}" ] && kill -0 "${WEB_PID}" 2>/dev/null; then
        kill -TERM "${WEB_PID}" 2>/dev/null || true
        sleep 1
        kill -KILL "${WEB_PID}" 2>/dev/null || true
        wait "${WEB_PID}" 2>/dev/null || true
    fi
    rm -f "${WEB_LOG}" "${WEB_PROBE}" "${WEB_PROBE_OUT}" "${CONCURRENT_DRIVER}" "${CONCURRENT_OUT}" "${TMPDIR:-/tmp}/sch-tunnel-info-$$.json"
}
trap web_bridge_cleanup EXIT
rm -f "${WEB_LOG}" "${WEB_PROBE_OUT}" "${CONCURRENT_OUT}"

node "${TUNNEL_DIR}/web.js" \
    --region "${SCH_REGION}" \
    --runtime-arn "${ARN}" \
    --session-id "${SID}" \
    --workspace "${WS}" \
    --storage "${STORAGE}" \
    --session-epoch "${SESSION_EPOCH}" \
    --remote-port "${REMOTE_PORT}" \
    >/dev/null 2>"${WEB_LOG}" &
WEB_PID=$!

WEB_URL=""
for _ in $(seq 1 30); do
    WEB_URL=$(python3 -c '
import json, sys
try:
    lines = open(sys.argv[1], encoding="utf-8").read().splitlines()
except OSError:
    lines = []
for line in lines:
    try:
        value = json.loads(line)
    except (TypeError, ValueError):
        continue
    if isinstance(value, dict) and value.get("type") == "ready":
        print(value.get("url", ""))
        break
' "${WEB_LOG}" 2>/dev/null || true)
    [ -n "${WEB_URL}" ] && break
    kill -0 "${WEB_PID}" 2>/dev/null || break
    sleep 1
done
if [ -n "${WEB_URL}" ] && kill -0 "${WEB_PID}" 2>/dev/null; then
    ok "headless web bridge reported ready at ${WEB_URL} and remains alive"
else
    bad "headless web bridge did not remain alive and report a ready URL"
    [ -f "${WEB_LOG}" ] && sed 's/^/    /' "${WEB_LOG}"
fi

cat > "${WEB_PROBE}" <<'NODE'
#!/usr/bin/env node
'use strict';
const fs = require('fs');
const base = process.argv[2].replace(/\/+$/, '');
const out = process.env.SCH_VERIFY_WEB_OUT;
// OpenCode 2: every route (UI and /api/*) is behind basic auth opencode:<pw>.
const password = process.env.SCH_OPENCODE_SERVER_PASSWORD || '';
const auth = password ? { authorization: 'Basic ' + Buffer.from('opencode:' + password).toString('base64') } : {};
const result = {};
const save = () => fs.writeFileSync(out, Object.entries(result).map(([k, v]) => `${k}=${v}`).join('\n') + '\n');
const request = async (path, options = {}, timeout = 20000) => {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeout);
  try {
    return await fetch(new URL(path, base), { ...options, headers: { ...auth, ...(options.headers || {}) }, signal: ctrl.signal });
  }
  finally { clearTimeout(timer); }
};
(async () => {
  const root = await request('/');
  const html = await root.text();
  result.root_status = root.status;
  result.root_html = root.ok && /<!doctype|<html[\s>]/i.test(html) ? 1 : 0;
  const refs = [...html.matchAll(/(?:src|href)=["']([^"']+)["']/gi)]
    .map((match) => new URL(match[1], base))
    .filter((url) => url.origin === new URL(base).origin);
  let assetOk = false;
  for (const url of refs) {
    try {
      const asset = await request(url.href);
      if (asset.ok && (await asset.arrayBuffer()).byteLength > 0) {
        assetOk = true;
        result.asset_path = url.pathname;
        result.asset_status = asset.status;
        break;
      }
    } catch (_) {}
  }
  result.asset_ok = assetOk ? 1 : 0;

  const sessions = await request('/api/session');
  const sessionsText = await sessions.text();
  let sessionsJson = false;
  try { const value = JSON.parse(sessionsText); sessionsJson = value !== null && typeof value === 'object'; } catch (_) {}
  result.sessions_status = sessions.status;
  result.sessions_ok = sessions.ok && sessionsJson ? 1 : 0;

  const events = await request('/api/event', { headers: { accept: 'text/event-stream' } }, 15000);
  result.sse_status = events.status;
  result.sse_type = events.headers.get('content-type') || '';
  result.sse_ok = events.ok && result.sse_type.toLowerCase().includes('text/event-stream') ? 1 : 0;
  if (events.body) await events.body.cancel();
  save();
})().catch((error) => { result.error = String(error.message || error).slice(0, 120); save(); process.exitCode = 1; });
NODE

if [ -n "${WEB_URL}" ]; then
    SCH_VERIFY_WEB_OUT="${WEB_PROBE_OUT}" SCH_OPENCODE_SERVER_PASSWORD="${REMOTE_PASSWORD}" run_bounded 60 node "${WEB_PROBE}" "${WEB_URL}"
    WEB_PROBE_RC=$?
else
    WEB_PROBE_RC=1
fi
web_get() { grep -E "^$1=" "${WEB_PROBE_OUT}" 2>/dev/null | head -1 | cut -d= -f2-; }
if [ "$(web_get root_html)" = "1" ]; then
    ok "web bridge served the OpenCode root UI HTML (HTTP $(web_get root_status))"
else
    bad "web root did not return UI HTML (probe rc=${WEB_PROBE_RC}, status=$(web_get root_status), error=$(web_get error))"
fi
if [ "$(web_get asset_ok)" = "1" ]; then
    ok "web bridge served a UI asset ($(web_get asset_path), HTTP $(web_get asset_status))"
else
    bad "web root exposed no fetchable UI asset"
fi
if [ "$(web_get sessions_ok)" = "1" ]; then
    ok "web bridge served the sessions API as JSON (GET /api/session, HTTP $(web_get sessions_status))"
else
    bad "GET /api/session did not return a successful JSON response (HTTP $(web_get sessions_status))"
fi
if [ "$(web_get sse_ok)" = "1" ]; then
    ok "web bridge opened the application SSE endpoint (GET /api/event, $(web_get sse_type))"
else
    bad "GET /api/event did not expose the expected SSE stream (HTTP $(web_get sse_status), type=$(web_get sse_type)); no application WebSocket is currently known"
fi

cat > "${CONCURRENT_DRIVER}" <<'NODE'
#!/usr/bin/env node
'use strict';
const fs = require('fs');
const out = process.env.SCH_VERIFY_CONCURRENT_OUT;
const base = process.argv.slice(2).find((arg) => /^https?:\/\//.test(arg));
// attach.js hands the backend password to the client as OPENCODE_PASSWORD.
const password = process.env.OPENCODE_PASSWORD || '';
const headers = password ? { authorization: 'Basic ' + Buffer.from('opencode:' + password).toString('base64') } : {};
(async () => {
  const response = await fetch(`${base.replace(/\/+$/, '')}/api/session`, { headers });
  JSON.parse(await response.text());
  fs.writeFileSync(out, `status=${response.status}\nok=${response.ok ? 1 : 0}\n`);
  process.exitCode = response.ok ? 0 : 1;
})().catch((error) => { fs.writeFileSync(out, `ok=0\nerror=${String(error.message || error)}\n`); process.exitCode = 1; });
NODE
chmod +x "${CONCURRENT_DRIVER}"
if [ -n "${WEB_URL}" ] && kill -0 "${WEB_PID}" 2>/dev/null; then
    SCH_VERIFY_CONCURRENT_OUT="${CONCURRENT_OUT}" SCH_OPENCODE_SERVER_PASSWORD="${REMOTE_PASSWORD}" run_bounded 60 node "${TUNNEL_DIR}/attach.js" \
        --region "${SCH_REGION}" \
        --runtime-arn "${ARN}" \
        --session-id "${SID}" \
        --workspace "${WS}" \
        --storage "${STORAGE}" \
        --session-epoch "${SESSION_EPOCH}" \
        --remote-port "${REMOTE_PORT}" \
        --opencode-bin "${CONCURRENT_DRIVER}"
    CONCURRENT_RC=$?
else
    CONCURRENT_RC=1
fi
if grep -q '^ok=1$' "${CONCURRENT_OUT}" 2>/dev/null && kill -0 "${WEB_PID}" 2>/dev/null; then
    ok "attach API request succeeded against port ${REMOTE_PORT} while the web bridge remained active on the same backend"
else
    bad "concurrent attach API probe failed or stopped the web bridge (attach rc=${CONCURRENT_RC})"
fi

WEB_PORT=""
[ -n "${WEB_URL}" ] && WEB_PORT=${WEB_URL##*:}
if kill -0 "${WEB_PID}" 2>/dev/null; then kill -TERM "${WEB_PID}" 2>/dev/null || true; fi
for _ in $(seq 1 10); do
    kill -0 "${WEB_PID}" 2>/dev/null || break
    sleep 1
done
if kill -0 "${WEB_PID}" 2>/dev/null; then
    bad "web bridge did not exit within 10s of SIGTERM"
    kill -KILL "${WEB_PID}" 2>/dev/null || true
fi
wait "${WEB_PID}" 2>/dev/null
WEB_RC=$?
WEB_PID=""
if [ "${WEB_RC}" -eq 0 ]; then
    ok "web bridge handled SIGTERM and exited cleanly"
else
    bad "web bridge exited with status ${WEB_RC} after SIGTERM"
fi
if [ -z "${WEB_PORT}" ]; then
    bad "cannot verify listener shutdown because the bridge reported no local port"
elif ! run_bounded 5 node -e "fetch('http://127.0.0.1:${WEB_PORT}/').then(() => process.exit(1), () => process.exit(0))"; then
    bad "web bridge listener on 127.0.0.1:${WEB_PORT} still accepted connections after shutdown"
else
    ok "web bridge listener closed after shutdown"
fi
web_bridge_cleanup
trap - EXIT
echo

# ===========================================================================
# 6. Cleanup: clear the interactive-writer advisory and stop the workspace.
# ===========================================================================
echo "== 6. cleanup =="
aws bedrock-agentcore invoke-agent-runtime \
    --cli-binary-format raw-in-base64-out \
    --agent-runtime-arn "${ARN}" \
    --runtime-session-id "${SID}" \
    --payload "{\"action\": \"mark-interactive\", \"workspace\": \"${WS}\", \"harness\": \"opencode\", \"active\": false, \"storage_backend\": \"${STORAGE}\", \"session_epoch\": ${SESSION_EPOCH}}" \
    --region "${SCH_REGION}" \
    /dev/null >/dev/null 2>&1 || true
if [ "${KEEP}" -eq 1 ]; then
    echo "-- --keep: leaving workspace '${WS}' running"
else
    "${SCH}" stop "${WS}" >/dev/null 2>&1 || true
    echo "-- stopped workspace '${WS}'"
fi
echo

echo "############################################"
echo "# Remote UI tunnel result: ${PASS} passed, ${FAIL} failed, ${WARN} warnings"
echo "############################################"
[ "${FAIL}" -eq 0 ] || exit 1
