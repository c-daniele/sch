#!/bin/bash
# test-local.sh — task 2.6 local verification of the SCH runtime image.
#
# Checks (spec: runtime-image):
#   1. image platform is linux/arm64
#   2. shim answers /ping on :8080
#   3. shim answers /invocations with action=info
#   4. opencode --version matches the pinned OPENCODE_VERSION
#   5. XDG env vars point at the canonical /mnt/workspace paths
#   6. init-workspace.sh is idempotent across repeated container runs
#      (simulated session storage via a docker volume mounted at /mnt/workspace),
#      including the per-file agent/AGENTS.md seeding (sch-remote-agents)
#   7. MCP server entrypoints (aws-docs, aws-mcp proxy, context7) resolvable in PATH,
#      executable by the non-root user, versions match the pinned ARGs
#   8. openspec and backlog base tools are available and match their pins;
#      (8b) baked Claude and OpenCode OpenSpec templates (12 commands and 12
#      skills per harness, including every non-core workflow)
#      and NOTHING pre-baked into ~/.claude (would break the L2 restore)
#   9. seeded opencode.json contains providers.amazon-bedrock (native V2
#      shape: settings.region, no V1 `provider` key, explicit Claude
#      maxTokens for the default model, TASK-9),
#      default_agent=remote-interactive (sch-remote-agents) and
#      mcp.aws-docs / mcp.aws-mcp (enabled, aws-mcp read-only) +
#      mcp.context7 (DISABLED by default, sch-context7-builtin);
#      custom agents (remote-interactive, remote-auto) + global AGENTS.md
#      seeded under state/config/opencode/
#  9.1 Pi dispatcher/binary pin and required flags; ENV bridge in normal/login
#      shells; harness-specific settings/AGENTS/roles/extension/trust seeding
#      and practical per-file non-overwrite checks
#  10. seeded Claude config (harness=claude): .mcp.json (at the project
#      root, REPO_DIR — sch-context7-builtin follow-up fix; Claude Code
#      does not read $CLAUDE_CONFIG_DIR/.mcp.json) has context7 entry,
#      settings.json has context7 in disabledMcpjsonServers (official Claude
#      Code mechanism; not the per-server "disabled" field of third-party
#      clients); (10b) OpenSpec /opsx:* commands + skills seeded per-file
#      into the user scope (CLAUDE_CONFIG_DIR) and the OpenSpec global
#      config seeded only-if-absent (v26 add-claude-openspec-commands)
#  11. dev-environment autonomy (v29 add-dev-env-autonomy): native toolchain and
#      jq on the runtime user's PATH (also in a login shell), uv pinned, uv
#      Python policy effective (bare `uv venv` -> system python3.11, no managed
#      CPython under XDG_DATA_HOME), a Python C extension built from source in a
#      project venv, and `npm ci` + node-gyp building a native addon. The seeded
#      dev-env contract is asserted in sections 9 and 10b (both harnesses).
set -uo pipefail

IMAGE="${1:-sch-runtime:dev}"
VOLUME="sch-test-workspace"
CONTAINER="sch-test-shim"
SEED_VOLUME="sch-test-seed-workspace"
SEED_CONTAINER="sch-test-seed-shim"
CLAUDE_VOLUME="sch-test-claude-workspace"
CLAUDE_CONTAINER="sch-test-claude-shim"
PI_VOLUME="sch-test-pi-workspace"
PI_CONTAINER="sch-test-pi-shim"
PASS=0
FAIL=0

check() { # <name> <cmd...>
    local name="$1"; shift
    if "$@" >/dev/null 2>&1; then
        echo "PASS: ${name}"; PASS=$((PASS+1))
    else
        echo "FAIL: ${name}"; FAIL=$((FAIL+1))
    fi
}

cleanup() {
    docker rm -f "${CONTAINER}" >/dev/null 2>&1 || true
    docker volume rm "${VOLUME}" >/dev/null 2>&1 || true
    docker rm -f "${SEED_CONTAINER}" >/dev/null 2>&1 || true
    docker volume rm "${SEED_VOLUME}" >/dev/null 2>&1 || true
    docker rm -f "${CLAUDE_CONTAINER}" >/dev/null 2>&1 || true
    docker volume rm "${CLAUDE_VOLUME}" >/dev/null 2>&1 || true
    docker rm -f "${PI_CONTAINER}" >/dev/null 2>&1 || true
    docker volume rm "${PI_VOLUME}" >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup

echo "== 1. platform =="
ARCH=$(docker image inspect "${IMAGE}" --format '{{.Os}}/{{.Architecture}}')
[ "${ARCH}" = "linux/arm64" ] && { echo "PASS: platform ${ARCH}"; PASS=$((PASS+1)); } \
    || { echo "FAIL: platform is ${ARCH}, expected linux/arm64"; FAIL=$((FAIL+1)); }

echo "== 2/3. shim on :8080 =="
docker volume create "${VOLUME}" >/dev/null
docker run -d --name "${CONTAINER}" -p 18080:8080 \
    -v "${VOLUME}:/mnt/workspace" "${IMAGE}" >/dev/null
# wait for the shim
for _ in $(seq 1 30); do
    curl -sf http://localhost:18080/ping >/dev/null 2>&1 && break
    sleep 1
done
# Hint the shim that this session storage is fresh (same as `sch shell` does
# on first invocation, main.py _wait_for_mount_restore): skips the ~120s
# no-hint mount-settle wait, since /invocations is served as soon as /ping is
# (bootstrap runs in a background thread).
curl -sf -X POST http://localhost:18080/invocations \
    -H 'Content-Type: application/json' -d '{"action": "noop", "storage": "fresh"}' >/dev/null 2>&1 || true
check "shim /ping responds" curl -sf http://localhost:18080/ping
INFO=$(curl -sf -X POST http://localhost:18080/invocations \
    -H 'Content-Type: application/json' -d '{"action": "info"}')
echo "info response: ${INFO}"
echo "${INFO}" | grep -q '"status": *"ok"' && { echo "PASS: info invocation"; PASS=$((PASS+1)); } \
    || { echo "FAIL: info invocation"; FAIL=$((FAIL+1)); }

echo "== 4. opencode pin =="
PINNED=$(docker exec "${CONTAINER}" printenv OPENCODE_VERSION)
# OpenCode 2 prints `opencode v2.0.18`; compare the bare version with the pin.
INSTALLED=$(docker exec "${CONTAINER}" opencode --version | sed -E 's/^opencode[[:space:]]+v?//' | tr -d '[:space:]')
echo "pinned=${PINNED} installed=${INSTALLED}"
[ "${INSTALLED}" = "${PINNED}" ] && { echo "PASS: opencode version matches pin"; PASS=$((PASS+1)); } \
    || { echo "FAIL: opencode ${INSTALLED} != pin ${PINNED}"; FAIL=$((FAIL+1)); }

# By now the opencode wrapper (which gates on the readiness marker) has
# already returned, so the shim's bootstrap — including the verify-after-
# write loop guarding against the mount-attach race (main.py
# _verify_workspace_seeded) — is guaranteed to have finished.
INFO2=$(curl -sf -X POST http://localhost:18080/invocations \
    -H 'Content-Type: application/json' -d '{"action": "info"}')
echo "${INFO2}" | grep -q '"seed_verified": *true' && { echo "PASS: seed_verified true"; PASS=$((PASS+1)); } \
    || { echo "FAIL: seed_verified not true: ${INFO2}"; FAIL=$((FAIL+1)); }

echo "== 5. XDG env =="
check "XDG_DATA_HOME"   docker exec "${CONTAINER}" sh -c '[ "$XDG_DATA_HOME" = "/mnt/workspace/state/data" ]'
check "XDG_CONFIG_HOME" docker exec "${CONTAINER}" sh -c '[ "$XDG_CONFIG_HOME" = "/mnt/workspace/state/config" ]'
check "OPENCODE_DB on local disk" docker exec "${CONTAINER}" sh -c '[ "$OPENCODE_DB" = "/home/sch/.opencode/opencode.db" ]'
# OpenCode 2: `debug paths db` (1.x had `db path`). Does not start a server.
check "opencode debug paths db honors OPENCODE_DB" docker exec "${CONTAINER}" sh -c '[ "$(opencode debug paths db 2>/dev/null)" = "/home/sch/.opencode/opencode.db" ]'
check "git safe.directory configured" docker exec "${CONTAINER}" sh -c 'git config --system --get-all safe.directory | grep -q /mnt/workspace/repo'
check "non-root user"   docker exec "${CONTAINER}" sh -c '[ "$(id -u)" = "1000" ]'
# add-user-provider-keys (design D3): the provider-key staging dir must exist,
# be owned by the runtime user and be 0700 — /run is root-owned in the running
# microVM, so the shim cannot create it itself and a missing dir would silently
# leave every session without the user's external providers. Also asserted at
# build time in the Dockerfile; re-checked here against the running image, plus
# an actual write as uid 1000 and the absence of any staged file by default.
check "provider-key staging dir is sch:sch 0700" docker exec "${CONTAINER}" sh -c '[ "$(stat -c %U:%G:%a /run/sch)" = "sch:sch:700" ]'
check "provider-key staging dir is writable by the runtime user" docker exec "${CONTAINER}" sh -c 'touch /run/sch/.probe && rm -f /run/sch/.probe'
check "provider-key staging dir is outside every checkpointed root" docker exec "${CONTAINER}" sh -c 'case /run/sch in /mnt/workspace/*|/home/sch/*) exit 1 ;; esac'
check "no provider key staged without a payload carrying one" docker exec "${CONTAINER}" sh -c '[ ! -e /run/sch/provider-keys.env ]'
# extend-pi-gateway-keys (TASK-18): the build-time generated gateway catalog
# must exist in the template dir, carry ENV VAR references only (never key
# values) and cover exactly the two gateways.
check "pi gateway models.json generated with env refs only" docker exec "${CONTAINER}" python3.11 -c "import json; d=json.load(open('/app/pi-templates/gateway-models.json')); ps=d['providers']; assert set(ps)=={'opencode','kilo'}; assert ps['opencode']['apiKey']=='\$OPENCODE_API_KEY' and ps['kilo']['apiKey']=='\$KILO_API_KEY'; assert all(p['models'] for p in ps.values())"

echo "== 6. init-workspace idempotency across restarts =="
check "config seeded" docker exec "${CONTAINER}" test -f /mnt/workspace/state/config/opencode/opencode.json
check "repo is git worktree" docker exec "${CONTAINER}" test -d /mnt/workspace/repo/.git
check "agent remote-interactive seeded" docker exec "${CONTAINER}" test -f /mnt/workspace/state/config/opencode/agents/remote-interactive.md
check "agent remote-auto seeded" docker exec "${CONTAINER}" test -f /mnt/workspace/state/config/opencode/agents/remote-auto.md
check "global AGENTS.md seeded" docker exec "${CONTAINER}" test -f /mnt/workspace/state/config/opencode/AGENTS.md
# user state that must survive a container restart on the same volume
docker exec "${CONTAINER}" sh -c 'echo "{\"custom\":true}" > /mnt/workspace/state/config/opencode/opencode.json'
docker exec "${CONTAINER}" sh -c 'echo "# operator-edit" > /mnt/workspace/state/config/opencode/agents/remote-auto.md'
docker exec "${CONTAINER}" touch /mnt/workspace/repo/sentinel.txt
docker rm -f "${CONTAINER}" >/dev/null
docker run -d --name "${CONTAINER}" -v "${VOLUME}:/mnt/workspace" "${IMAGE}" >/dev/null
sleep 3
check "config NOT overwritten on 2nd run" docker exec "${CONTAINER}" grep -q custom /mnt/workspace/state/config/opencode/opencode.json
check "agent file NOT overwritten on 2nd run" docker exec "${CONTAINER}" grep -q operator-edit /mnt/workspace/state/config/opencode/agents/remote-auto.md
check "repo untouched on 2nd run" docker exec "${CONTAINER}" test -f /mnt/workspace/repo/sentinel.txt

echo "== 7. MCP server entrypoints (aws-docs, aws-mcp proxy, context7) =="
check "aws-docs entrypoint in PATH" docker exec "${CONTAINER}" sh -c 'command -v awslabs.aws-documentation-mcp-server'
check "aws-mcp proxy entrypoint in PATH"  docker exec "${CONTAINER}" sh -c 'command -v mcp-proxy-for-aws-cli'
check "context7 entrypoint in PATH (sch-context7-builtin)" docker exec "${CONTAINER}" sh -c 'command -v context7-mcp'
check "aws-docs executable as non-root" docker exec "${CONTAINER}" sh -c 'timeout 8 awslabs.aws-documentation-mcp-server --help >/dev/null 2>&1; rc=$?; [ "$rc" -ne 126 ] && [ "$rc" -ne 127 ]'
check "aws-mcp proxy executable as non-root"  docker exec "${CONTAINER}" sh -c 'timeout 8 mcp-proxy-for-aws-cli --help >/dev/null 2>&1; rc=$?; [ "$rc" -ne 126 ] && [ "$rc" -ne 127 ]'
check "context7 binary on local disk (not shadowed by mount)" docker exec "${CONTAINER}" sh -c 'readlink -f "$(command -v context7-mcp)" | grep -qv "^/mnt/workspace/"'
check "aws-docs venv under /opt (local disk)" docker exec "${CONTAINER}" sh -c 'readlink -f "$(command -v awslabs.aws-documentation-mcp-server)" | grep -q "^/opt/uv-tools/"'
check "aws-mcp proxy venv under /opt (local disk)"  docker exec "${CONTAINER}" sh -c 'readlink -f "$(command -v mcp-proxy-for-aws-cli)" | grep -q "^/opt/uv-tools/"'
DOCS_PINNED=$(docker exec "${CONTAINER}" printenv AWS_DOCS_MCP_VERSION)
PROXY_PINNED=$(docker exec "${CONTAINER}" printenv MCP_PROXY_VERSION)
DOCS_INSTALLED=$(docker exec "${CONTAINER}" sh -c "/opt/uv-tools/awslabs-aws-documentation-mcp-server/bin/python -c \"import importlib.metadata as m; print(m.version('awslabs.aws-documentation-mcp-server'))\"" 2>/dev/null)
PROXY_INSTALLED=$(docker exec "${CONTAINER}" sh -c "/opt/uv-tools/mcp-proxy-for-aws-cli/bin/python -c \"import importlib.metadata as m; print(m.version('mcp-proxy-for-aws-cli'))\"" 2>/dev/null)
# context7 version: read via `npm ls -g --json` (design D2 note: the npm-22
# global prefix on AL2023 is /usr/lib/nodejs22, NOT /usr/local/, so a
# hardcoded /usr/local/lib/node_modules/... path is wrong here; npm ls -g
# resolves the actual prefix regardless of where it lands).
CTX7_PINNED=$(docker exec "${CONTAINER}" printenv CONTEXT7_MCP_VERSION)
CTX7_INSTALLED=$(docker exec "${CONTAINER}" sh -c "npm ls -g @upstash/context7-mcp --depth=0 --json 2>/dev/null" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["dependencies"]["@upstash/context7-mcp"]["version"])' 2>/dev/null)
echo "aws-docs pinned=${DOCS_PINNED} installed=${DOCS_INSTALLED}"
echo "aws-mcp  pinned=${PROXY_PINNED} installed=${PROXY_INSTALLED}"
echo "context7 pinned=${CTX7_PINNED} installed=${CTX7_INSTALLED}"
[ "${DOCS_INSTALLED}" = "${DOCS_PINNED}" ] && { echo "PASS: aws-docs version matches pin"; PASS=$((PASS+1)); } \
    || { echo "FAIL: aws-docs ${DOCS_INSTALLED} != pin ${DOCS_PINNED}"; FAIL=$((FAIL+1)); }
[ "${PROXY_INSTALLED}" = "${PROXY_PINNED}" ] && { echo "PASS: aws-mcp proxy version matches pin"; PASS=$((PASS+1)); } \
    || { echo "FAIL: aws-mcp proxy ${PROXY_INSTALLED} != pin ${PROXY_PINNED}"; FAIL=$((FAIL+1)); }
[ "${CTX7_INSTALLED}" = "${CTX7_PINNED}" ] && { echo "PASS: context7 version matches pin"; PASS=$((PASS+1)); } \
    || { echo "FAIL: context7 ${CTX7_INSTALLED} != pin ${CTX7_PINNED}"; FAIL=$((FAIL+1)); }

echo "== 7b. AWS CLI v2 in the image (v23) =="
check "aws on PATH" docker exec "${CONTAINER}" sh -c 'command -v aws'
check "aws on local disk (not shadowed by mount)" docker exec "${CONTAINER}" sh -c 'readlink -f "$(command -v aws)" | grep -q "^/usr/local/aws-cli/"'
check "aws executable as non-root" docker exec "${CONTAINER}" sh -c 'aws --version >/dev/null 2>&1'
check "aws visible in a login shell" docker exec "${CONTAINER}" bash --login -c 'command -v aws >/dev/null'
check "AWS_PAGER empty (no less in PTY sessions)" docker exec "${CONTAINER}" bash --login -c '[ -z "${AWS_PAGER-unset}" ]'
CLI_PINNED=$(docker exec "${CONTAINER}" printenv SCH_AWS_CLI_VERSION)
CLI_INSTALLED=$(docker exec "${CONTAINER}" sh -c 'aws --version 2>&1' | awk '{print $1}' | sed 's|^aws-cli/||')
echo "aws-cli  pinned=${CLI_PINNED} installed=${CLI_INSTALLED}"
[ -n "${CLI_PINNED}" ] && [ "${CLI_INSTALLED}" = "${CLI_PINNED}" ] \
    && { echo "PASS: aws-cli version matches pin"; PASS=$((PASS+1)); } \
    || { echo "FAIL: aws-cli ${CLI_INSTALLED} != pin ${CLI_PINNED}"; FAIL=$((FAIL+1)); }

echo "== 7c. sch-build-image (v23, add-codebuild-image-rebuild) =="
check "sch-build-image on PATH" docker exec "${CONTAINER}" sh -c 'command -v sch-build-image'
check "sch-build-image executable as non-root" docker exec "${CONTAINER}" sh -c 'sch-build-image --help >/dev/null'
# Capability disabled (no SCH_IMAGE_REBUILD_PROJECT in the local container):
# must fail fast with a non-zero exit and an explicit message, and must NOT
# have reached any AWS API (verified indirectly: it exits before the CLI runs,
# so no credential/endpoint error can appear in the output).
REBUILD_ERR=$(docker exec "${CONTAINER}" sh -c 'sch-build-image 2>&1 >/dev/null; echo "rc=$?"' || true)
echo "disabled-capability output: ${REBUILD_ERR}"
echo "${REBUILD_ERR}" | grep -q 'rc=1' \
    && { echo "PASS: sch-build-image exits 1 when capability disabled"; PASS=$((PASS+1)); } \
    || { echo "FAIL: sch-build-image did not exit 1 when capability disabled"; FAIL=$((FAIL+1)); }
echo "${REBUILD_ERR}" | grep -qi 'not enabled' \
    && { echo "PASS: sch-build-image explains the capability is not enabled"; PASS=$((PASS+1)); } \
    || { echo "FAIL: sch-build-image message does not mention the disabled capability"; FAIL=$((FAIL+1)); }
echo "${REBUILD_ERR}" | grep -qiE 'credential|endpoint|Unable to locate' \
    && { echo "FAIL: sch-build-image reached the AWS CLI with the capability disabled"; FAIL=$((FAIL+1)); } \
    || { echo "PASS: sch-build-image made no AWS call with the capability disabled"; PASS=$((PASS+1)); }

echo "== 8. base workflow tools (v24) =="
check "openspec on PATH" docker exec "${CONTAINER}" sh -c 'command -v openspec'
check "backlog on PATH" docker exec "${CONTAINER}" sh -c 'command -v backlog'
check "openspec on local disk (not shadowed by mount)" docker exec "${CONTAINER}" sh -c 'readlink -f "$(command -v openspec)" | grep -qv "^/mnt/workspace/"'
check "backlog on local disk (not shadowed by mount)" docker exec "${CONTAINER}" sh -c 'readlink -f "$(command -v backlog)" | grep -qv "^/mnt/workspace/"'
OPENSPEC_PINNED=$(docker exec "${CONTAINER}" printenv OPENSPEC_VERSION)
BACKLOG_PINNED=$(docker exec "${CONTAINER}" printenv BACKLOG_MD_VERSION)
OPENSPEC_INSTALLED=$(docker exec "${CONTAINER}" openspec --version | tr -d '[:space:]')
BACKLOG_INSTALLED=$(docker exec "${CONTAINER}" backlog --version | tr -d '[:space:]')
echo "openspec pinned=${OPENSPEC_PINNED} installed=${OPENSPEC_INSTALLED}"
echo "backlog  pinned=${BACKLOG_PINNED} installed=${BACKLOG_INSTALLED}"
[ "${OPENSPEC_INSTALLED}" = "${OPENSPEC_PINNED}" ] && { echo "PASS: openspec version matches pin"; PASS=$((PASS+1)); } \
    || { echo "FAIL: openspec ${OPENSPEC_INSTALLED} != pin ${OPENSPEC_PINNED}"; FAIL=$((FAIL+1)); }
[ "${BACKLOG_INSTALLED}" = "${BACKLOG_PINNED}" ] && { echo "PASS: backlog version matches pin"; PASS=$((PASS+1)); } \
    || { echo "FAIL: backlog ${BACKLOG_INSTALLED} != pin ${BACKLOG_PINNED}"; FAIL=$((FAIL+1)); }

echo "== 8b. baked Claude OpenSpec templates (v26, add-claude-openspec-commands) =="
# Generated at build time from the pinned CLI (Dockerfile asserts the same
# counts; re-checked here against the running image). The full 12-workflow
# set matters: the CLI's default core profile would emit only 6.
check "12 opsx command templates baked" docker exec "${CONTAINER}" sh -c '[ "$(find /app/claude-templates/commands/opsx -name "*.md" | wc -l)" = "12" ]'
check "opsx ff.md template present (non-core workflow)" docker exec "${CONTAINER}" test -f /app/claude-templates/commands/opsx/ff.md
check "12 openspec skill templates baked" docker exec "${CONTAINER}" sh -c '[ "$(find /app/claude-templates/skills -mindepth 2 -maxdepth 2 -name SKILL.md | wc -l)" = "12" ]'
check "12 OpenCode opsx command templates baked" docker exec "${CONTAINER}" sh -c '[ "$(find /app/opencode-templates/commands -maxdepth 1 -name "opsx-*.md" | wc -l)" = "12" ]'
check "12 OpenCode openspec skill templates baked" docker exec "${CONTAINER}" sh -c '[ "$(find /app/opencode-templates/skills -mindepth 2 -maxdepth 2 -name SKILL.md | wc -l)" = "12" ]'
check "complete OpenCode workflow template set baked" docker exec "${CONTAINER}" sh -c 'for pair in propose:openspec-propose explore:openspec-explore new:openspec-new-change continue:openspec-continue-change apply:openspec-apply-change update:openspec-update-change ff:openspec-ff-change sync:openspec-sync-specs archive:openspec-archive-change bulk-archive:openspec-bulk-archive-change verify:openspec-verify-change onboard:openspec-onboard; do workflow=${pair%%:*}; skill=${pair#*:}; test -f "/app/opencode-templates/commands/opsx-${workflow}.md" && test -f "/app/opencode-templates/skills/${skill}/SKILL.md" || exit 1; done'
# CRITICAL guard (design D3): the image must NOT pre-populate ~/.claude with
# the templates — a non-empty local dir permanently disables the L2
# claude-state restore (_restore_claude_state's never-overwrite-L1 check).
# This container's bootstrap already ran init-workspace.sh with the default
# harness=opencode, so the same check also proves the opencode branch never
# seeds the Claude artifacts.
check "no templates in ~/.claude (not baked, not seeded on harness=opencode)" docker exec "${CONTAINER}" sh -c '[ ! -e /home/sch/.claude/commands ] && [ ! -e /home/sch/.claude/skills ]'

echo "== 9. seeded config: providers.amazon-bedrock + mcp.aws-docs/aws-mcp + mcp.context7(disabled) + agents (sch-remote-agents) =="
# Fresh workspace, isolated from the idempotency test above (section 6 wrote
# a custom, non-schema config into ${VOLUME} on purpose).
docker rm -f "${SEED_CONTAINER}" >/dev/null 2>&1 || true
docker volume rm "${SEED_VOLUME}" >/dev/null 2>&1 || true
docker volume create "${SEED_VOLUME}" >/dev/null
docker run -d --name "${SEED_CONTAINER}" -p 18081:8080 -v "${SEED_VOLUME}:/mnt/workspace" "${IMAGE}" >/dev/null
for _ in $(seq 1 30); do
    curl -sf http://localhost:18081/ping >/dev/null 2>&1 && break
    sleep 1
done
# Same "fresh" hint as above (task 5.2): skips the ~120s no-hint mount-settle
# wait so this check does not need a multi-minute poll.
curl -sf -X POST http://localhost:18081/invocations \
    -H 'Content-Type: application/json' -d '{"action": "noop", "storage": "fresh"}' >/dev/null 2>&1 || true
for _ in $(seq 1 30); do
    docker exec "${SEED_CONTAINER}" test -f /mnt/workspace/state/config/opencode/opencode.json >/dev/null 2>&1 && break
    sleep 1
done
CFG=$(docker exec "${SEED_CONTAINER}" cat /mnt/workspace/state/config/opencode/opencode.json 2>/dev/null)
echo "seeded config: ${CFG}"
echo "${CFG}" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["providers"]["amazon-bedrock"]["settings"]["region"]; assert "provider" not in d' 2>/dev/null \
    && { echo "PASS: providers.amazon-bedrock.settings.region present, no V1 provider key"; PASS=$((PASS+1)); } \
    || { echo "FAIL: providers.amazon-bedrock.settings.region missing or V1 provider key present"; FAIL=$((FAIL+1)); }
# TASK-9: OpenCode 2 sends no Bedrock output cap unless configured, so the
# seeded default model must carry an explicit inferenceConfig.maxTokens.
echo "${CFG}" | python3 -c 'import json,sys; d=json.load(sys.stdin); m=d["model"].split("/",1)[1]; v=d["providers"]["amazon-bedrock"]["models"][m]["body"]["inferenceConfig"]["maxTokens"]; assert isinstance(v,int) and v>4096' 2>/dev/null \
    && { echo "PASS: default model has an explicit Bedrock maxTokens"; PASS=$((PASS+1)); } \
    || { echo "FAIL: default model lacks an explicit Bedrock maxTokens"; FAIL=$((FAIL+1)); }
echo "${CFG}" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["mcp"]["aws-docs"]["enabled"] is True' 2>/dev/null \
    && { echo "PASS: mcp.aws-docs enabled"; PASS=$((PASS+1)); } \
    || { echo "FAIL: mcp.aws-docs missing/disabled"; FAIL=$((FAIL+1)); }
# aws-mcp is the managed AWS MCP Server via the mcp-proxy-for-aws-cli SigV4
# proxy (TASK-25): local stdio command, --read-only, operation region via
# --metadata AWS_REGION, endpoint https://aws-mcp.<region>.api.aws/mcp, and
# NO environment block (the migration guide drops env entirely — credentials
# come from the execution-role chain, never static keys).
echo "${CFG}" | python3 -c 'import json,sys; d=json.load(sys.stdin); m=d["mcp"]["aws-mcp"]; assert m["enabled"] is True; assert m["type"] == "local"; c=m["command"]; assert c[0] == "mcp-proxy-for-aws-cli"; assert any(a.startswith("https://aws-mcp.") and a.endswith("/mcp") for a in c); assert "--read-only" in c; i=c.index("--metadata"); assert c[i+1].startswith("AWS_REGION=")' 2>/dev/null \
    && { echo "PASS: mcp.aws-mcp enabled (proxy, endpoint, --read-only, --metadata AWS_REGION)"; PASS=$((PASS+1)); } \
    || { echo "FAIL: mcp.aws-mcp missing/disabled/misconfigured"; FAIL=$((FAIL+1)); }
echo "${CFG}" | python3 -c 'import json,sys; d=json.load(sys.stdin); m=d["mcp"]["aws-mcp"]; assert "environment" not in m' 2>/dev/null \
    && { echo "PASS: mcp.aws-mcp has no environment block (no static credentials possible)"; PASS=$((PASS+1)); } \
    || { echo "FAIL: mcp.aws-mcp has an unexpected environment block"; FAIL=$((FAIL+1)); }
# sch-context7-builtin: context7 is shipped as a built-in MCP server but
# disabled by default. OpenCode uses the official `enabled: false` flag.
echo "${CFG}" | python3 -c 'import json,sys; d=json.load(sys.stdin); m=d["mcp"]["context7"]; assert m["enabled"] is False; assert m["type"] == "local"; assert m["command"] == ["context7-mcp"]' 2>/dev/null \
    && { echo "PASS: mcp.context7 disabled (type=local, command=context7-mcp)"; PASS=$((PASS+1)); } \
    || { echo "FAIL: mcp.context7 missing or not disabled"; FAIL=$((FAIL+1)); }
# sch-remote-agents: default_agent points at the seeded custom primary agent;
# both agent files + the global AGENTS.md land under state/config/opencode/.
echo "${CFG}" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["default_agent"] == "remote-interactive"' 2>/dev/null \
    && { echo "PASS: default_agent=remote-interactive"; PASS=$((PASS+1)); } \
    || { echo "FAIL: default_agent missing or not remote-interactive"; FAIL=$((FAIL+1)); }
check "remote-interactive agent seeded (fresh ws)" docker exec "${SEED_CONTAINER}" test -f /mnt/workspace/state/config/opencode/agents/remote-interactive.md
check "remote-auto agent seeded (fresh ws)" docker exec "${SEED_CONTAINER}" test -f /mnt/workspace/state/config/opencode/agents/remote-auto.md
check "global AGENTS.md seeded (fresh ws)" docker exec "${SEED_CONTAINER}" test -f /mnt/workspace/state/config/opencode/AGENTS.md
check "fresh repo has no project-scoped .opencode" docker exec "${SEED_CONTAINER}" test ! -e /mnt/workspace/repo/.opencode
check "12 global OpenCode opsx commands seeded" docker exec "${SEED_CONTAINER}" sh -c '[ "$(find /mnt/workspace/state/config/opencode/commands -maxdepth 1 -name "opsx-*.md" | wc -l)" = "12" ]'
check "12 global OpenCode openspec skills seeded" docker exec "${SEED_CONTAINER}" sh -c '[ "$(find /mnt/workspace/state/config/opencode/skills -mindepth 2 -maxdepth 2 -name SKILL.md | wc -l)" = "12" ]'
check "global opsx-explore/ff/apply available without sync" docker exec "${SEED_CONTAINER}" sh -c 'test -f /mnt/workspace/state/config/opencode/commands/opsx-explore.md && test -f /mnt/workspace/state/config/opencode/commands/opsx-ff.md && test -f /mnt/workspace/state/config/opencode/commands/opsx-apply.md'
# Frontmatter sanity on the seeded agents: primary mode on both, and the
# headless agent must not expose the question tool (no user is present).
check "remote-interactive is mode: primary" docker exec "${SEED_CONTAINER}" grep -q '^mode: primary$' /mnt/workspace/state/config/opencode/agents/remote-interactive.md
check "remote-auto is mode: primary" docker exec "${SEED_CONTAINER}" grep -q '^mode: primary$' /mnt/workspace/state/config/opencode/agents/remote-auto.md
check "remote-auto disables question tool" docker exec "${SEED_CONTAINER}" grep -q 'question: false' /mnt/workspace/state/config/opencode/agents/remote-auto.md
# add-dev-env-autonomy (v29): the dev-environment autonomy contract must reach
# the harness through the SEEDED files, not just the baked templates — the brief
# is appended to every agent's system prompt, and the remote-auto delta is what
# stops a headless run from reporting a missing dependency as a blocker.
check "seeded AGENTS.md carries the dev-env section" docker exec "${SEED_CONTAINER}" grep -q '^## Development environments$' /mnt/workspace/state/config/opencode/AGENTS.md
check "seeded AGENTS.md mandates uv/npm lockfile-first bootstrap" docker exec "${SEED_CONTAINER}" sh -c 'grep -q "uv sync" /mnt/workspace/state/config/opencode/AGENTS.md && grep -q "npm ci" /mnt/workspace/state/config/opencode/AGENTS.md'
check "seeded remote-auto treats a missing dep as a bootstrap" docker exec "${SEED_CONTAINER}" grep -q 'bootstrap to run, not a blocker' /mnt/workspace/state/config/opencode/agents/remote-auto.md
check "seeded remote-interactive bootstraps only on explicit request" docker exec "${SEED_CONTAINER}" grep -q 'only on the user.s explicit' /mnt/workspace/state/config/opencode/agents/remote-interactive.md
# Exercise top-up and coexistence in place. Global operator edits and unrelated
# global state must survive byte-for-byte; removed generated files must return;
# project-scoped fixtures and ordinary repository files must not be touched.
docker exec "${SEED_CONTAINER}" sh -c 'mkdir -p /mnt/workspace/repo/.opencode/commands /mnt/workspace/repo/.opencode/skills/openspec-explore /mnt/workspace/state/config/opencode/session && printf "PROJECT COMMAND\n" > /mnt/workspace/repo/.opencode/commands/opsx-explore.md && printf "PROJECT SKILL\n" > /mnt/workspace/repo/.opencode/skills/openspec-explore/SKILL.md && printf "REPO SENTINEL\n" > /mnt/workspace/repo/sentinel.txt && printf "SESSION STATE\n" > /mnt/workspace/state/config/opencode/session/operator-state && printf "{\"custom\":true}\n" > /mnt/workspace/state/config/opencode/opencode.json && printf "OPERATOR AGENT\n" > /mnt/workspace/state/config/opencode/agents/remote-auto.md && printf "OPERATOR PLUGIN\n" > /mnt/workspace/state/config/opencode/plugin/sch-telegram.js && printf "OPERATOR COMMAND\n" > /mnt/workspace/state/config/opencode/commands/opsx-ff.md && printf "OPERATOR SKILL\n" > /mnt/workspace/state/config/opencode/skills/openspec-ff-change/SKILL.md && rm /mnt/workspace/state/config/opencode/commands/opsx-new.md /mnt/workspace/state/config/opencode/skills/openspec-new-change/SKILL.md'
docker exec -e SCH_HARNESS=opencode "${SEED_CONTAINER}" bash /app/init-workspace.sh >/dev/null
check "operator-edited global OpenCode command preserved" docker exec "${SEED_CONTAINER}" sh -c '[ "$(cat /mnt/workspace/state/config/opencode/commands/opsx-ff.md)" = "OPERATOR COMMAND" ]'
check "operator-edited global OpenCode skill preserved" docker exec "${SEED_CONTAINER}" sh -c '[ "$(cat /mnt/workspace/state/config/opencode/skills/openspec-ff-change/SKILL.md)" = "OPERATOR SKILL" ]'
check "removed global OpenCode command re-seeded" docker exec "${SEED_CONTAINER}" test -s /mnt/workspace/state/config/opencode/commands/opsx-new.md
check "removed global OpenCode skill re-seeded" docker exec "${SEED_CONTAINER}" test -s /mnt/workspace/state/config/opencode/skills/openspec-new-change/SKILL.md
check "project-scoped OpenCode command untouched" docker exec "${SEED_CONTAINER}" sh -c '[ "$(cat /mnt/workspace/repo/.opencode/commands/opsx-explore.md)" = "PROJECT COMMAND" ]'
check "project-scoped OpenCode skill untouched" docker exec "${SEED_CONTAINER}" sh -c '[ "$(cat /mnt/workspace/repo/.opencode/skills/openspec-explore/SKILL.md)" = "PROJECT SKILL" ]'
check "repository file untouched by OpenSpec seed" docker exec "${SEED_CONTAINER}" sh -c '[ "$(cat /mnt/workspace/repo/sentinel.txt)" = "REPO SENTINEL" ]'
check "OpenCode session state untouched by OpenSpec seed" docker exec "${SEED_CONTAINER}" sh -c '[ "$(cat /mnt/workspace/state/config/opencode/session/operator-state)" = "SESSION STATE" ]'
check "pre-existing OpenCode config/agents/plugins remain unchanged" docker exec "${SEED_CONTAINER}" sh -c '[ "$(cat /mnt/workspace/state/config/opencode/opencode.json)" = "{\"custom\":true}" ] && [ "$(cat /mnt/workspace/state/config/opencode/agents/remote-auto.md)" = "OPERATOR AGENT" ] && [ "$(cat /mnt/workspace/state/config/opencode/plugin/sch-telegram.js)" = "OPERATOR PLUGIN" ]'
docker rm -f "${SEED_CONTAINER}" >/dev/null 2>&1 || true
docker volume rm "${SEED_VOLUME}" >/dev/null 2>&1 || true

echo "== 9.1. pi harness: dispatcher, ENV bridge, seed and idempotency =="
docker volume create "${PI_VOLUME}" >/dev/null
docker run -d --name "${PI_CONTAINER}" -p 18083:8080 \
    -v "${PI_VOLUME}:/mnt/workspace" "${IMAGE}" >/dev/null
for _ in $(seq 1 30); do
    curl -sf http://localhost:18083/ping >/dev/null 2>&1 && break
    sleep 1
done
# Select Pi before bootstrap and skip the no-hint mount-settle wait. Re-running
# init explicitly makes this section independent of the noop/readiness race.
curl -sf -X POST http://localhost:18083/invocations \
    -H 'Content-Type: application/json' -d '{"action": "noop", "storage": "fresh", "harness": "pi"}' >/dev/null 2>&1 || true
docker exec -e SCH_HARNESS=pi "${PI_CONTAINER}" bash /app/init-workspace.sh >/dev/null
for _ in $(seq 1 30); do
    docker exec "${PI_CONTAINER}" test -f /home/sch/.pi/agent/.ready >/dev/null 2>&1 && break
    sleep 1
done

# Repeat the Dockerfile's Pi installation assertions against the running image:
# the PATH entry is the SCH dispatcher, its embedded real binary exists, the
# installed version matches PI_VERSION, and every CLI capability SCH uses is
# still exposed by the pinned pre-1.0 release.
check "pi dispatcher is /usr/local/bin/pi and executable" docker exec "${PI_CONTAINER}" sh -c '[ "$(command -v pi)" = "/usr/local/bin/pi" ] && [ -x /usr/local/bin/pi ] && grep -q "single dispatcher" /usr/local/bin/pi'
check "pi dispatcher real binary exists" docker exec "${PI_CONTAINER}" sh -c 'real=$(sed -n '\''s/^REAL="\(.*\)"$/\1/p'\'' /usr/local/bin/pi); [ -n "$real" ] && [ "$real" != "/usr/local/bin/pi" ] && [ -x "$real" ]'
PI_PINNED=$(docker exec "${PI_CONTAINER}" printenv PI_VERSION)
PI_INSTALLED=$(docker exec -e SCH_HARNESS=pi -e SCH_HARNESS_WAIT=0 "${PI_CONTAINER}" pi --version | tr -d '[:space:]')
echo "pi       pinned=${PI_PINNED} installed=${PI_INSTALLED}"
[ -n "${PI_PINNED}" ] && [ "${PI_INSTALLED}" = "${PI_PINNED}" ] \
    && { echo "PASS: pi version matches pin"; PASS=$((PASS+1)); } \
    || { echo "FAIL: pi ${PI_INSTALLED} != pin ${PI_PINNED}"; FAIL=$((FAIL+1)); }
check "pi required CLI flags present" docker exec -e SCH_HARNESS=pi -e SCH_HARNESS_WAIT=0 "${PI_CONTAINER}" sh -c 'help=$(pi --help); for flag in " -p" "--session" "--append-system-prompt" "--provider" "--model" "--mode" " -e"; do printf "%s\n" "$help" | grep -q -- "$flag" || exit 1; done'

check "pi ENV bridge in normal shell" docker exec "${PI_CONTAINER}" sh -c '[ "$PI_CODING_AGENT_DIR" = "/home/sch/.pi/agent" ] && [ "$PI_TELEMETRY" = "0" ] && [ "$PI_SKIP_VERSION_CHECK" = "1" ]'
check "pi ENV bridge in login shell" docker exec "${PI_CONTAINER}" bash --login -c '[ "$PI_CODING_AGENT_DIR" = "/home/sch/.pi/agent" ] && [ "$PI_TELEMETRY" = "0" ] && [ "$PI_SKIP_VERSION_CHECK" = "1" ]'
check "pi dispatcher restores its ENV bridge" docker exec "${PI_CONTAINER}" bash -c '
    sed '\''s|^REAL=".*"|REAL="${SCH_HARNESS_REAL:?SCH_HARNESS_REAL not set}"|'\'' /usr/local/bin/pi > /tmp/pi
    printf "#!/bin/sh\nenv\n" > /tmp/pi-real
    chmod +x /tmp/pi
    chmod +x /tmp/pi-real
    env -u PI_CODING_AGENT_DIR -u PI_TELEMETRY -u PI_SKIP_VERSION_CHECK SCH_HARNESS=pi SCH_HARNESS_REAL=/tmp/pi-real SCH_HARNESS_WAIT=0 /tmp/pi \
      | grep -q "^PI_CODING_AGENT_DIR=/home/sch/.pi/agent$"
    env -u PI_CODING_AGENT_DIR -u PI_TELEMETRY -u PI_SKIP_VERSION_CHECK SCH_HARNESS=pi SCH_HARNESS_REAL=/tmp/pi-real SCH_HARNESS_WAIT=0 /tmp/pi \
      | grep -q "^PI_TELEMETRY=0$"
    env -u PI_CODING_AGENT_DIR -u PI_TELEMETRY -u PI_SKIP_VERSION_CHECK SCH_HARNESS=pi SCH_HARNESS_REAL=/tmp/pi-real SCH_HARNESS_WAIT=0 /tmp/pi \
      | grep -q "^PI_SKIP_VERSION_CHECK=1$"'
check "pi readiness marker created" docker exec "${PI_CONTAINER}" test -f /home/sch/.pi/agent/.ready

PI_SETTINGS=$(docker exec "${PI_CONTAINER}" cat /home/sch/.pi/agent/settings.json 2>/dev/null)
echo "seeded Pi settings: ${PI_SETTINGS}"
echo "${PI_SETTINGS}" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["defaultProvider"] == "amazon-bedrock"; assert d["defaultModel"].endswith(".anthropic.claude-sonnet-4-6"); assert d["defaultProjectTrust"] == "always"; assert d["enableInstallTelemetry"] is False; assert d["enableAnalytics"] is False' 2>/dev/null \
    && { echo "PASS: Pi settings seed has Bedrock/model/trust/telemetry defaults"; PASS=$((PASS+1)); } \
    || { echo "FAIL: Pi settings seed missing required defaults"; FAIL=$((FAIL+1)); }
check "Pi AGENTS.md seeded" docker exec "${PI_CONTAINER}" grep -q '^# Execution environment: AWS microVM$' /home/sch/.pi/agent/AGENTS.md
check "Pi remote-auto role seeded" docker exec "${PI_CONTAINER}" grep -q '^You are running unattended:' /home/sch/.pi/agent/roles/remote-auto.md
check "Pi remote-interactive role seeded" docker exec "${PI_CONTAINER}" grep -q '^You are in an interactive working session' /home/sch/.pi/agent/roles/remote-interactive.md
check "Pi extension and ownership sidecar seeded" docker exec "${PI_CONTAINER}" sh -c 'grep -q "SCH extension for Pi" /home/sch/.pi/agent/extensions/sch-pi.ts && test -s /home/sch/.pi/agent/extensions/sch-pi.ts.sch-seeded'
check "Pi canonical worktree trust seeded" docker exec "${PI_CONTAINER}" python3 -c 'import json; d=json.load(open("/home/sch/.pi/agent/trust.json")); assert d["/mnt/workspace/repo"] is True'

# Per-file idempotency: preserve operator-owned files and trust decisions while
# restoring an absent role template. An edited extension no longer matches its
# SCH sidecar and must therefore be treated as operator-owned.
docker exec "${PI_CONTAINER}" sh -c 'printf "{\"operator\":true}\n" > /home/sch/.pi/agent/settings.json && printf "OPERATOR AGENTS\n" > /home/sch/.pi/agent/AGENTS.md && printf "OPERATOR ROLE\n" > /home/sch/.pi/agent/roles/remote-interactive.md && rm /home/sch/.pi/agent/roles/remote-auto.md && printf "OPERATOR EXTENSION\n" > /home/sch/.pi/agent/extensions/sch-pi.ts && printf "{\"/mnt/workspace/repo\":false,\"/operator/repo\":true}\n" > /home/sch/.pi/agent/trust.json'
docker exec -e SCH_HARNESS=pi "${PI_CONTAINER}" bash /app/init-workspace.sh >/dev/null
check "Pi settings and AGENTS.md not overwritten" docker exec "${PI_CONTAINER}" sh -c '[ "$(cat /home/sch/.pi/agent/settings.json)" = "{\"operator\":true}" ] && [ "$(cat /home/sch/.pi/agent/AGENTS.md)" = "OPERATOR AGENTS" ]'
check "Pi operator role not overwritten" docker exec "${PI_CONTAINER}" sh -c '[ "$(cat /home/sch/.pi/agent/roles/remote-interactive.md)" = "OPERATOR ROLE" ]'
check "Pi deleted role re-seeded" docker exec "${PI_CONTAINER}" grep -q '^You are running unattended:' /home/sch/.pi/agent/roles/remote-auto.md
check "Pi operator extension not overwritten" docker exec "${PI_CONTAINER}" sh -c '[ "$(cat /home/sch/.pi/agent/extensions/sch-pi.ts)" = "OPERATOR EXTENSION" ]'
check "Pi trust decisions not overwritten" docker exec "${PI_CONTAINER}" python3 -c 'import json; d=json.load(open("/home/sch/.pi/agent/trust.json")); assert d["/mnt/workspace/repo"] is False; assert d["/operator/repo"] is True'
docker rm -f "${PI_CONTAINER}" >/dev/null 2>&1 || true
docker volume rm "${PI_VOLUME}" >/dev/null 2>&1 || true

echo "== 10. claude harness seed: .mcp.json + settings.json(context7 disabled) =="
# Run init-workspace.sh directly with SCH_HARNESS=claude on a fresh volume
# to exercise the claude branch (the shim's bootstrap path is OpenCode by
# default, so we drive the claude branch via direct docker exec on the
# /app/init-workspace.sh script). Asserts the merge of `disabledMcpjsonServers`
# in `settings.json` (official Claude Code mechanism, NOT per-server
# "disabled" in .mcp.json).
docker volume create "${CLAUDE_VOLUME:-sch-test-claude-workspace}" >/dev/null
docker run -d --name "${CLAUDE_CONTAINER:-sch-test-claude-shim}" -p 18082:8080 \
    -v "${CLAUDE_VOLUME:-sch-test-claude-workspace}:/mnt/workspace" "${IMAGE}" >/dev/null
for _ in $(seq 1 30); do
    curl -sf http://localhost:18082/ping >/dev/null 2>&1 && break
    sleep 1
done
# Drive init-workspace.sh with SCH_HARNESS=claude (and a fresh-storage hint
# so the shim doesn't pre-seed opencode.json on the same volume).
curl -sf -X POST http://localhost:18082/invocations \
    -H 'Content-Type: application/json' -d '{"action": "noop", "storage": "fresh", "harness": "claude"}' >/dev/null 2>&1 || true
# Re-run init-workspace.sh explicitly with SCH_HARNESS=claude to be sure the
# claude branch fires (the shim is harness-aware, but the noop may race the
# ready marker; this guarantees the seed has happened by test time).
docker exec -e SCH_HARNESS=claude "${CLAUDE_CONTAINER:-sch-test-claude-shim}" bash /app/init-workspace.sh 2>&1 | tail -10
MCP_JSON=$(docker exec "${CLAUDE_CONTAINER:-sch-test-claude-shim}" cat /mnt/workspace/repo/.mcp.json 2>/dev/null)
SETTINGS_JSON=$(docker exec "${CLAUDE_CONTAINER:-sch-test-claude-shim}" cat /home/sch/.claude/settings.json 2>/dev/null)
echo "seeded .mcp.json: ${MCP_JSON}"
echo "seeded settings.json: ${SETTINGS_JSON}"
# context7 is listed in .mcp.json so `claude mcp list` shows it.
echo "${MCP_JSON}" | python3 -c 'import json,sys; d=json.load(sys.stdin); m=d["mcpServers"]["context7"]; assert m["command"] == "context7-mcp"; assert m["env"] == {}' 2>/dev/null \
    && { echo "PASS: .mcp.json mcpServers.context7 present (command=context7-mcp, env={})"; PASS=$((PASS+1)); } \
    || { echo "FAIL: .mcp.json context7 missing or wrong shape"; FAIL=$((FAIL+1)); }
# aws-mcp is the managed AWS MCP Server via the proxy (TASK-25): same
# guarantees as the OpenCode seed — proxy command, managed endpoint,
# --read-only, --metadata AWS_REGION, empty env (credentials from the chain).
echo "${MCP_JSON}" | python3 -c 'import json,sys; d=json.load(sys.stdin); m=d["mcpServers"]["aws-mcp"]; assert m["command"] == "mcp-proxy-for-aws-cli"; a=m["args"]; assert any(x.startswith("https://aws-mcp.") and x.endswith("/mcp") for x in a); assert "--read-only" in a; i=a.index("--metadata"); assert a[i+1].startswith("AWS_REGION="); assert m["env"] == {}' 2>/dev/null \
    && { echo "PASS: .mcp.json mcpServers.aws-mcp present (proxy, endpoint, --read-only, AWS_REGION, env={})"; PASS=$((PASS+1)); } \
    || { echo "FAIL: .mcp.json aws-mcp missing or wrong shape"; FAIL=$((FAIL+1)); }
# Official disable mechanism: `disabledMcpjsonServers` in settings.json.
echo "${SETTINGS_JSON}" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert "disabledMcpjsonServers" in d and "context7" in d["disabledMcpjsonServers"]' 2>/dev/null \
    && { echo "PASS: settings.json has context7 in disabledMcpjsonServers (official Claude Code mechanism)"; PASS=$((PASS+1)); } \
    || { echo "FAIL: settings.json missing context7 in disabledMcpjsonServers"; FAIL=$((FAIL+1)); }
# settings.json must NOT have a per-server "disabled" field on context7 in
# .mcp.json (that's a third-party-client convention, not Claude Code's
# schema). If a future Claude Code version does pick it up, this is the
# place to start; for now we assert it's not relied upon.
echo "${MCP_JSON}" | python3 -c 'import json,sys; d=json.load(sys.stdin); m=d["mcpServers"]["context7"]; assert "disabled" not in m' 2>/dev/null \
    && { echo "PASS: .mcp.json context7 has no per-server 'disabled' field (relies on settings.json)"; PASS=$((PASS+1)); } \
    || { echo "FAIL: .mcp.json context7 unexpectedly uses per-server 'disabled'"; FAIL=$((FAIL+1)); }
# Idempotency of the merge: write a custom settings.json and re-run the
# seeding, verify custom fields survive and context7 stays in the disabled
# list. Mirrors the scenario the actual /app/init-workspace.sh helper guards
# against.
docker exec "${CLAUDE_CONTAINER:-sch-test-claude-shim}" sh -c 'cat > /home/sch/.claude/settings.json <<JSON
{
  "env": {"FOO": "bar"},
  "cleanupPeriodDays": 30,
  "disabledMcpjsonServers": ["other-server"]
}
JSON'
docker exec -e SCH_HARNESS=claude "${CLAUDE_CONTAINER:-sch-test-claude-shim}" bash /app/init-workspace.sh 2>&1 | tail -3
MERGED=$(docker exec "${CLAUDE_CONTAINER:-sch-test-claude-shim}" cat /home/sch/.claude/settings.json 2>/dev/null)
echo "merged settings.json: ${MERGED}"
echo "${MERGED}" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["env"]["FOO"] == "bar"; assert d["cleanupPeriodDays"] == 30; assert "other-server" in d["disabledMcpjsonServers"]; assert "context7" in d["disabledMcpjsonServers"]; assert d["disabledMcpjsonServers"].count("context7") == 1' 2>/dev/null \
    && { echo "PASS: settings.json merge is idempotent (FOO/cleanupPeriodDays/other-server preserved, context7 added once)"; PASS=$((PASS+1)); } \
    || { echo "FAIL: settings.json merge lost or duplicated fields"; FAIL=$((FAIL+1)); }

echo "== 10b. claude harness seed: OpenSpec /opsx:* commands + skills + global config (v26) =="
# Same container/volume as section 10 — init-workspace.sh has already run
# with SCH_HARNESS=claude above, so the OpenSpec artifacts must be seeded
# into the user scope (CLAUDE_CONFIG_DIR) by now.
check "12 opsx commands seeded in ~/.claude/commands/opsx" docker exec "${CLAUDE_CONTAINER:-sch-test-claude-shim}" sh -c '[ "$(find /home/sch/.claude/commands/opsx -name "*.md" | wc -l)" = "12" ]'
check "opsx ff.md seeded (non-core workflow)" docker exec "${CLAUDE_CONTAINER:-sch-test-claude-shim}" test -f /home/sch/.claude/commands/opsx/ff.md
check "12 openspec skills seeded in ~/.claude/skills" docker exec "${CLAUDE_CONTAINER:-sch-test-claude-shim}" sh -c '[ "$(find /home/sch/.claude/skills -mindepth 2 -maxdepth 2 -name SKILL.md | wc -l)" = "12" ]'
# add-dev-env-autonomy (v29): same normative content on the claude harness.
check "seeded CLAUDE.md carries the dev-env section" docker exec "${CLAUDE_CONTAINER:-sch-test-claude-shim}" grep -q '^## Development environments$' /home/sch/.claude/CLAUDE.md
check "seeded CLAUDE.md mandates uv/npm lockfile-first bootstrap" docker exec "${CLAUDE_CONTAINER:-sch-test-claude-shim}" sh -c 'grep -q "uv sync" /home/sch/.claude/CLAUDE.md && grep -q "npm ci" /home/sch/.claude/CLAUDE.md'
check "seeded claude remote-auto treats a missing dep as a bootstrap" docker exec "${CLAUDE_CONTAINER:-sch-test-claude-shim}" grep -q 'bootstrap to run, not a blocker' /home/sch/.claude/agents/remote-auto.md
check "seeded claude remote-interactive bootstraps only on explicit request" docker exec "${CLAUDE_CONTAINER:-sch-test-claude-shim}" grep -q 'only on the operator.s explicit request' /home/sch/.claude/agents/remote-interactive.md
OPENSPEC_CFG=$(docker exec "${CLAUDE_CONTAINER:-sch-test-claude-shim}" cat /mnt/workspace/state/config/openspec/config.json 2>/dev/null)
echo "seeded openspec config: ${OPENSPEC_CFG}"
echo "${OPENSPEC_CFG}" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["profile"] == "custom"; assert len(d["workflows"]) == 12; assert "ff" in d["workflows"]; assert d["telemetry"]["noticeSeen"] is True' 2>/dev/null \
    && { echo "PASS: openspec global config seeded (custom profile, 12 workflows incl. ff)"; PASS=$((PASS+1)); } \
    || { echo "FAIL: openspec global config missing or wrong shape"; FAIL=$((FAIL+1)); }
# Per-file idempotency (same contract as the OpenCode agents seeding):
# operator edits survive re-seeding, deleted files are re-seeded, an
# existing openspec config is never modified.
docker exec "${CLAUDE_CONTAINER:-sch-test-claude-shim}" sh -c 'echo "OPERATOR EDIT" > /home/sch/.claude/commands/opsx/ff.md && rm /home/sch/.claude/commands/opsx/new.md && echo "{\"profile\":\"core\"}" > /mnt/workspace/state/config/openspec/config.json'
docker exec -e SCH_HARNESS=claude "${CLAUDE_CONTAINER:-sch-test-claude-shim}" bash /app/init-workspace.sh 2>&1 | tail -3
check "operator-edited opsx command preserved" docker exec "${CLAUDE_CONTAINER:-sch-test-claude-shim}" sh -c '[ "$(cat /home/sch/.claude/commands/opsx/ff.md)" = "OPERATOR EDIT" ]'
check "deleted opsx command re-seeded" docker exec "${CLAUDE_CONTAINER:-sch-test-claude-shim}" sh -c 'grep -q "OPSX: New" /home/sch/.claude/commands/opsx/new.md'
check "existing openspec config never overwritten" docker exec "${CLAUDE_CONTAINER:-sch-test-claude-shim}" sh -c '[ "$(cat /mnt/workspace/state/config/openspec/config.json)" = "{\"profile\":\"core\"}" ]'
# Isolated direct bootstrap proves harness=claude never receives the OpenCode
# global command/skill trees. This uses a separate root so the container shim's
# earlier bootstrap cannot affect the assertion.
docker exec "${CLAUDE_CONTAINER:-sch-test-claude-shim}" rm -rf /tmp/claude-only-workspace /tmp/claude-only-config
docker exec -e SCH_HARNESS=claude -e SCH_WORKSPACE_ROOT=/tmp/claude-only-workspace -e XDG_DATA_HOME=/tmp/claude-only-workspace/state/data -e XDG_CONFIG_HOME=/tmp/claude-only-workspace/state/config -e CLAUDE_CONFIG_DIR=/tmp/claude-only-config "${CLAUDE_CONTAINER:-sch-test-claude-shim}" bash /app/init-workspace.sh >/dev/null
check "claude harness receives no global OpenCode workflows" docker exec "${CLAUDE_CONTAINER:-sch-test-claude-shim}" sh -c '[ ! -e /tmp/claude-only-workspace/state/config/opencode/commands ] && [ ! -e /tmp/claude-only-workspace/state/config/opencode/skills ]'
docker rm -f "${CLAUDE_CONTAINER:-sch-test-claude-shim}" >/dev/null 2>&1 || true
docker volume rm "${CLAUDE_VOLUME:-sch-test-claude-workspace}" >/dev/null 2>&1 || true

echo "== 11. dev-environment autonomy: toolchain, uv policy, native builds (v29) =="
# add-dev-env-autonomy tasks 4.1-4.3. The Dockerfile already asserts the cheap,
# offline half of this at build time (toolchain on sch's PATH, uv pin, bare
# `uv venv` -> python3.11); repeated here against the RUNNING image, plus the two
# checks that need a container and network: source-building a Python C extension
# and `npm ci` on a project with a node-gyp native module.
check "gcc on PATH (user sch)"  docker exec "${CONTAINER}" sh -c 'command -v gcc'
check "g++ on PATH (user sch)"  docker exec "${CONTAINER}" sh -c 'command -v g++'
check "make on PATH (user sch)" docker exec "${CONTAINER}" sh -c 'command -v make'
check "jq on PATH (user sch)"   docker exec "${CONTAINER}" sh -c 'jq --version >/dev/null'
check "toolchain visible in a login shell" docker exec "${CONTAINER}" bash --login -c 'command -v gcc >/dev/null && command -v make >/dev/null && command -v jq >/dev/null'
check "python3.11 C headers installed (python3.11-devel)" docker exec "${CONTAINER}" sh -c 'test -f "$(python3.11 -c "import sysconfig; print(sysconfig.get_paths()[\"include\"])")/Python.h"'
UV_PINNED=$(docker exec "${CONTAINER}" printenv UV_VERSION)
UV_INSTALLED=$(docker exec "${CONTAINER}" sh -c 'uv --version 2>&1' | awk '{print $2}')
echo "uv       pinned=${UV_PINNED} installed=${UV_INSTALLED}"
[ -n "${UV_PINNED}" ] && [ "${UV_INSTALLED}" = "${UV_PINNED}" ] \
    && { echo "PASS: uv version matches pin"; PASS=$((PASS+1)); } \
    || { echo "FAIL: uv ${UV_INSTALLED} != pin ${UV_PINNED}"; FAIL=$((FAIL+1)); }
check "UV_PYTHON_DOWNLOADS=never in container ENV" docker exec "${CONTAINER}" sh -c '[ "$UV_PYTHON_DOWNLOADS" = "never" ]'
check "UV_PYTHON=python3.11 in container ENV" docker exec "${CONTAINER}" sh -c '[ "$UV_PYTHON" = "python3.11" ]'
check "uv policy also holds in a login shell (no container ENV inherited)" docker exec "${CONTAINER}" bash --login -c '[ "$UV_PYTHON_DOWNLOADS" = "never" ] && [ "$UV_PYTHON" = "python3.11" ]'
# 4.2: bare `uv venv` (no explicit interpreter) must yield a python3.11 env built
# on the SYSTEM interpreter, and must leave no uv-managed CPython on the
# checkpointed mount (XDG_DATA_HOME).
check "bare uv venv uses the system python3.11" docker exec "${CONTAINER}" sh -c '
    set -e
    rm -rf /tmp/devcheck-venv
    cd /tmp && uv venv /tmp/devcheck-venv >/dev/null 2>&1
    /tmp/devcheck-venv/bin/python -c "import sys; assert sys.version_info[:2] == (3, 11), sys.version"
    [ "$(readlink -f /tmp/devcheck-venv/bin/python)" = "$(readlink -f /usr/bin/python3.11)" ]'
check "no uv-managed CPython under XDG_DATA_HOME" docker exec "${CONTAINER}" sh -c '[ ! -d "${XDG_DATA_HOME}/uv/python" ]'
# 4.3a: a Python package with a C extension, forced to build from source
# (--no-binary), must compile in a project-local venv.
check "C extension builds from source in a project venv" docker exec "${CONTAINER}" sh -c '
    set -e
    rm -rf /tmp/devcheck-py && mkdir -p /tmp/devcheck-py && cd /tmp/devcheck-py
    uv venv .venv >/dev/null 2>&1
    UV_NATIVE_TLS=1 uv pip install --python .venv/bin/python --no-binary :all: markupsafe==3.0.2 >/dev/null 2>&1
    ./.venv/bin/python -c "import markupsafe._speedups"'
# 4.3b: `npm ci` on a project with a node-gyp native module must build it. The
# addon is authored here (rather than depending on a third-party package staying
# healthy) so the check exercises exactly node-gyp -> make -> gcc.
check "npm ci builds a node-gyp native module" docker exec "${CONTAINER}" bash -c '
    set -e
    rm -rf /tmp/devcheck-node && mkdir -p /tmp/devcheck-node && cd /tmp/devcheck-node
    cat > package.json <<JSON
{ "name": "devcheck-addon", "version": "1.0.0", "private": true,
  "dependencies": { "node-addon-api": "8.3.0" } }
JSON
    # The lockfile must be produced BEFORE binding.gyp exists: npm auto-adds an
    # `install: node-gyp rebuild` script to any package with a binding.gyp and
    # runs it even for --package-lock-only, which would fail with no node_modules.
    npm install --package-lock-only --ignore-scripts --no-audit --no-fund >/dev/null 2>&1
    cat > binding.gyp <<GYP
{ "targets": [ { "target_name": "devcheck",
    "sources": [ "devcheck.cc" ],
    "include_dirs": [ "/tmp/devcheck-node/node_modules/node-addon-api" ],
    "defines": [ "NAPI_DISABLE_CPP_EXCEPTIONS" ] } ] }
GYP
    cat > devcheck.cc <<CC
#include <napi.h>
Napi::Value Answer(const Napi::CallbackInfo& info) {
  return Napi::Number::New(info.Env(), 42);
}
Napi::Object Init(Napi::Env env, Napi::Object exports) {
  exports.Set("answer", Napi::Function::New(env, Answer));
  return exports;
}
NODE_API_MODULE(devcheck, Init)
CC
    test -f package-lock.json
    # npm ci itself drives node-gyp -> make -> gcc for the root package.
    npm ci --no-audit --no-fund >/dev/null 2>&1
    test -f build/Release/devcheck.node
    [ "$(node -e "console.log(require(\"./build/Release/devcheck.node\").answer())")" = "42" ]'
docker exec "${CONTAINER}" sh -c 'rm -rf /tmp/devcheck-venv /tmp/devcheck-py /tmp/devcheck-node' >/dev/null 2>&1 || true

echo
echo "Results: ${PASS} passed, ${FAIL} failed"
[ "${FAIL}" -eq 0 ]
