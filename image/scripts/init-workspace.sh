#!/bin/bash
# init-workspace.sh — idempotent bootstrap of the SCH session workspace.
#
# Invoked by the shim (app/main.py) at container start, on EVERY start:
# on a fresh session storage it seeds the canonical layout; on a resumed
# session it must be a no-op for existing state (spec: runtime-image,
# "Idempotent workspace initialization").
#
# Layout (design D2):
#   /mnt/workspace/repo            worktree (stable absolute path => stable
#                                  OpenCode project hash / Claude encoded-cwd);
#                                  also where Claude's project-scoped
#                                  `.mcp.json` is seeded (sch-context7-builtin
#                                  follow-up fix — see section 4 below)
#   /mnt/workspace/state/data      XDG_DATA_HOME  (opencode.db, snapshot/, ...)
#   /mnt/workspace/state/config    XDG_CONFIG_HOME (opencode/opencode.json)
#   /mnt/workspace/state/claude    L2 replica of Claude Code state (JSONL
#                                  transcripts + settings.json config subset);
#                                  the LIVE path is $HOME/.claude on local disk
#                                  (CLAUDE_CONFIG_DIR), mirrored here by the
#                                  L2 checkpoint loop (design D2, sch-multi-
#                                  harness). `.mcp.json` is NOT part of this
#                                  mirror (sch-context7-builtin follow-up):
#                                  it lives in the repo worktree instead, so
#                                  it rides the existing repo.tar.gz L2
#                                  checkpoint for free.
#
# Harness selection (sch-multi-harness, design D3/D9): the shim propagates
# the workspace's persisted harness (from bin/sch → payload → mount marker)
# via the SCH_HARNESS env var when invoking this script. Seeding branches on
# it so the right config (opencode.json vs repo/.mcp.json + ~/.claude/
# settings.json) is written. Idempotent for both branches: never overwrite
# existing config.
#
# Repo bootstrap modes (design D9), selected via session env vars:
#   SCH_REPO_URL unset  -> git init on an empty /mnt/workspace/repo
#   SCH_REPO_URL set    -> HTTPS clone (optional token: SCH_REPO_TOKEN)
#   repo already a worktree -> no-op
set -uo pipefail

WORKSPACE_ROOT="${SCH_WORKSPACE_ROOT:-/mnt/workspace}"
REPO_DIR="${WORKSPACE_ROOT}/repo"
DATA_DIR="${XDG_DATA_HOME:-${WORKSPACE_ROOT}/state/data}"
CONFIG_DIR="${XDG_CONFIG_HOME:-${WORKSPACE_ROOT}/state/config}"
OPENCODE_CONFIG_DIR="${CONFIG_DIR}/opencode"
OPENCODE_CONFIG_FILE="${OPENCODE_CONFIG_DIR}/opencode.json"
# OpenCode global templates include custom agents, AGENTS.md, plugins, and the
# build-generated OpenSpec commands/skills. They persist with opencode.json on
# the mount. The override is for direct out-of-image testing of this script.
OPENCODE_AGENTS_DIR="${OPENCODE_CONFIG_DIR}/agents"
OPENCODE_GLOBAL_AGENTS_MD="${OPENCODE_CONFIG_DIR}/AGENTS.md"
OPENCODE_TEMPLATE_DIR="${SCH_OPENCODE_TEMPLATE_DIR:-/app/opencode-templates}"
# add-claude-openspec-commands: image-baked OpenSpec artifacts for Claude Code
# (generated at build time from the pinned CLI — see image/Dockerfile v26).
# Seeded per-file into CLAUDE_CONFIG_DIR (user scope: commands/opsx/ ->
# /opsx:* slash commands, skills/ -> openspec-* skills) on harness=claude,
# section 2c below. The override is for direct out-of-image testing only.
CLAUDE_TEMPLATE_DIR="${SCH_CLAUDE_TEMPLATE_DIR:-/app/claude-templates}"
# OpenSpec global config (both harnesses, section 2d): `openspec config path`
# resolves through XDG_CONFIG_HOME, so this lives on the mount and rides the
# L2 checkpoint like the rest of state/config.
OPENSPEC_CONFIG_DIR="${CONFIG_DIR}/openspec"
OPENSPEC_CONFIG_FILE="${OPENSPEC_CONFIG_DIR}/config.json"
# Managed AWS MCP Server endpoint (TASK-25): baked into the image as
# AWS_MCP_ENDPOINT (default eu-central-1, closest to eu-west-1); the shell
# default below keeps out-of-image testing working.
AWS_MCP_ENDPOINT="${AWS_MCP_ENDPOINT:-https://aws-mcp.eu-central-1.api.aws/mcp}"
# Claude Code local-disk root (design D2): CLAUDE_CONFIG_DIR is the override
# Claude Code honors for ~/.claude. The shim exports it via the dispatcher
# (scripts/harness-wrapper.sh) and via /etc/profile.d/sch-env.sh; here we
# only need the path for the seeding branch.
CLAUDE_CONFIG_DIR="${CLAUDE_CONFIG_DIR:-/home/sch/.claude}"
# sch-context7-builtin follow-up fix: Claude Code only ever reads a
# project-scoped `.mcp.json` from the PROJECT ROOT (cwd where `claude` is
# launched, i.e. REPO_DIR) — it does NOT read `$CLAUDE_CONFIG_DIR/.mcp.json`
# (code.claude.com/docs/en/mcp: "Claude Code doesn't read paths such as
# ~/.claude/.mcp.json"; confirmed empirically — with the file at the old
# location, `claude mcp list` reported no servers configured at all,
# disabled or otherwise). Seeded in section 4, AFTER repo bootstrap
# (section 3), so it never confuses the "is the repo dir empty" clone check.
CLAUDE_MCP_FILE="${REPO_DIR}/.mcp.json"
CLAUDE_SETTINGS_FILE="${CLAUDE_CONFIG_DIR}/settings.json"
# add-provider-api-keys (design D4): Claude Code's global config file, where the
# one-time "use this API key?" approval is persisted
# (`customApiKeyResponses.approved`, holding the LAST 20 CHARACTERS of the key —
# never the key itself). Path resolution mirrors the pinned Claude Code binary:
# `$CLAUDE_CONFIG_DIR/.config.json` when that file exists, otherwise
# `$CLAUDE_CONFIG_DIR/.claude.json` (with CLAUDE_CONFIG_DIR set, `~/.claude.json`
# is NOT the file the binary reads). Resolved lazily in section 2e, after the
# directory has been created.
CLAUDE_AGENTS_DIR="${CLAUDE_CONFIG_DIR}/agents"
CLAUDE_GLOBAL_MD="${CLAUDE_CONFIG_DIR}/CLAUDE.md"
CLAUDE_TEMPLATE_DIR="${SCH_CLAUDE_TEMPLATE_DIR:-/app/claude-templates}"
# Claude L2 replica on the mount (mirrored by the shim's checkpoint loop).
CLAUDE_STATE_REPLICA="${WORKSPACE_ROOT}/state/claude"

# --- Pi (add-pi-harness, design D2/D4/D8) -------------------------------------
# PI_CODING_AGENT_DIR is Pi's config-dir override, pointed at LOCAL disk by the
# image ENV / dispatcher / profile.d (the mount has no fcntl support). It holds
# settings.json, trust.json, AGENTS.md, roles/, extensions/ and the JSONL
# sessions; the shim's L2 loop mirrors it to ${WORKSPACE_ROOT}/state/pi.
PI_CONFIG_DIR="${PI_CODING_AGENT_DIR:-/home/sch/.pi/agent}"
PI_SETTINGS_FILE="${PI_CONFIG_DIR}/settings.json"
PI_TRUST_FILE="${PI_CONFIG_DIR}/trust.json"
PI_GLOBAL_MD="${PI_CONFIG_DIR}/AGENTS.md"
PI_ROLES_DIR="${PI_CONFIG_DIR}/roles"
PI_EXTENSIONS_DIR="${PI_CONFIG_DIR}/extensions"
PI_TEMPLATE_DIR="${SCH_PI_TEMPLATE_DIR:-/app/pi-templates}"
PI_STATE_REPLICA="${WORKSPACE_ROOT}/state/pi"

# Region for the seeded provider/mcp blocks (spec: runtime-image, "Amazon
# Bedrock provider selectable out-of-the-box"): prefer the environment
# (AWS_REGION/AWS_DEFAULT_REGION — task 1.3 confirmed AgentCore Runtime
# injects these into the process env), fall back to the image-baked default.
SEED_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-${SCH_AWS_REGION:-eu-west-1}}}"

# add-pi-harness: Bedrock cross-region inference profiles are prefixed by
# geography, and a profile from the wrong geography is not invokable from the
# deployment region. Pi's settings.json carries a bare model ID (no provider
# options block, unlike opencode.json), so the default model itself must carry
# the right prefix — derived from SEED_REGION rather than hardcoded. Unknown
# geographies fall back to `global.`, which is invokable from every region.
case "${SEED_REGION}" in
    eu-*)                   PI_PROFILE_PREFIX="eu" ;;
    us-*)                   PI_PROFILE_PREFIX="us" ;;
    ap-southeast-2|ap-southeast-4) PI_PROFILE_PREFIX="au" ;;
    ap-northeast-1|ap-northeast-3) PI_PROFILE_PREFIX="jp" ;;
    *)                      PI_PROFILE_PREFIX="global" ;;
esac
# Same model family as the other harnesses' interactive default. Overridable per
# deployment (no image rebuild) via SCH_PI_DEFAULT_MODEL, which is taken as an
# opaque, fully-qualified Pi model ID.
PI_SEED_MODEL="${SCH_PI_DEFAULT_MODEL:-${PI_PROFILE_PREFIX}.anthropic.claude-sonnet-4-6}"

# Harness selection (see header comment). Default to opencode for raw shells
# without `sch` (spec: harness-selection, "Assenza di harness nel payload non
# seleziona il default" — multi-harness is opt-in of the wrapper `sch`; raw
# shells keep the OpenCode-only behavior unchanged).
HARNESS="${SCH_HARNESS:-opencode}"
case "${HARNESS}" in
    opencode|claude|pi) ;;
    *)
        echo "[init-workspace] WARNING: unknown harness '${HARNESS}', falling back to opencode" >&2
        HARNESS="opencode"
        ;;
esac

log() { echo "[init-workspace] $*"; }

# add-provider-api-keys (design D3/D4): the PRESENCE of an Anthropic key means
# "use the Anthropic API for the claude harness", so the Bedrock-on marker this
# script seeds into settings.json must follow it. The value is never read here,
# only tested for emptiness — no key value ever reaches a log line or a file
# other than the approval SUFFIX written in section 2e.
if [ -n "${SCH_ANTHROPIC_API_KEY:-}" ]; then
    CLAUDE_BEDROCK_SETTING="0"
else
    CLAUDE_BEDROCK_SETTING="1"
fi

# Portable content hash (AL2023 has sha256sum; macOS dev/test boxes shasum).
sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    else
        shasum -a 256 "$1" | awk '{print $1}'
    fi
}

# --- 1. Canonical directories (idempotent) -----------------------------------
# state/claude is always created (cheap, harmless) so the L2 checkpoint loop
# has a stable mirror target even on harness=opencode workspaces that later
# flip (per D8 switching requires a new workspace, but the dir is harmless).
for dir in "${REPO_DIR}" "${DATA_DIR}" "${OPENCODE_CONFIG_DIR}" "${CLAUDE_STATE_REPLICA}" "${CLAUDE_CONFIG_DIR}" "${PI_STATE_REPLICA}" "${PI_CONFIG_DIR}"; do
    if [ ! -d "${dir}" ]; then
        mkdir -p "${dir}"
        log "created ${dir}"
    fi
done

# --- 2. Seed default harness config ONLY if absent ---------------------------
# Idempotent for BOTH harnesses (spec: runtime-image, "Inizializzazione
# idempotente"): never overwrite existing opencode.json, .mcp.json, or
# settings.json. The L2 restore path respects the same never-overwrite-L1
# property (it un-mirrors to these paths only when L1 is empty, see
# _restore_l2 + _restore_claude_state in app/main.py).
if [ "${HARNESS}" = "opencode" ]; then
    # Provider amazon-bedrock: credentials come from the runtime execution
    # role (design D4) — no API key, no auth.json. The provider block is
    # explicit (design D3) so it is registered independently of OpenCode's
    # env-based autoload gate. mcp.aws-docs and mcp.aws-mcp are enabled by
    # default (design D2); aws-mcp is the managed AWS MCP Server reached
    # through the mcp-proxy-for-aws-cli SigV4 proxy (TASK-25, replaces the
    # retired awslabs.aws-api-mcp-server). It is read-only (--read-only) with
    # no static credentials — it inherits AWS_PROFILE=default ->
    # credential_process -> IMDS, and the operation region comes from
    # --metadata AWS_REGION. IAM remains the enforcing boundary (spec R10).
    # mcp.context7 (sch-context7-builtin): built-in but DISABLED by default
    # via the official `enabled: false` flag (https://opencode.ai/docs/mcp-
    # servers/ — "disable a server by setting enabled to false"). `command`
    # points at the globally-installed binary `/usr/local/bin/context7-mcp`
    # (no `npx` at runtime, no env entries — anonymous access).
    # TASK-9: the provider block uses the native OpenCode 2 shape
    # (`providers`/`settings`) because it carries per-model overrides, and a V2
    # `providers.amazon-bedrock` entry next to a V1 `provider.amazon-bedrock`
    # one makes OpenCode drop the V1 block (region included). OpenCode 2 sends
    # no `inferenceConfig.maxTokens` to Bedrock unless configured, so Bedrock
    # caps Claude output at its own 4096-token default and long turns stall;
    # each `body` below sets the cap explicitly. Values stay within each
    # model's AWS max output (a larger value is rejected) and are not maximized
    # blindly: Bedrock reserves input + maxTokens from the TPM quota at request
    # start. A model missing from this map keeps the Bedrock default.
    if [ ! -f "${OPENCODE_CONFIG_FILE}" ]; then
        cat > "${OPENCODE_CONFIG_FILE}" <<EOF
{
  "\$schema": "https://opencode.ai/config.json",
  "model": "amazon-bedrock/eu.anthropic.claude-sonnet-4-6",
  "small_model": "amazon-bedrock/eu.anthropic.claude-haiku-4-5-20251001-v1:0",
  "default_agent": "remote-interactive",
  "providers": {
    "amazon-bedrock": {
      "settings": {
        "region": "${SEED_REGION}"
      },
      "models": {
        "eu.anthropic.claude-sonnet-4-6": {
          "body": { "inferenceConfig": { "maxTokens": 64000 } }
        },
        "global.anthropic.claude-fable-5": {
          "body": { "inferenceConfig": { "maxTokens": 64000 } }
        },
        "global.anthropic.claude-fable-5-1": {
          "body": { "inferenceConfig": { "maxTokens": 128000 } }
        },
        "global.anthropic.claude-opus-5": {
          "body": { "inferenceConfig": { "maxTokens": 64000 } }
        },
        "global.anthropic.claude-opus-5-5": {
          "body": { "inferenceConfig": { "maxTokens": 128000 } }
        },
        "eu.anthropic.claude-fable-5": {
          "body": { "inferenceConfig": { "maxTokens": 64000 } }
        },
        "eu.anthropic.claude-opus-5": {
          "body": { "inferenceConfig": { "maxTokens": 64000 } }
        },
        "eu.anthropic.claude-opus-5-5": {
          "body": { "inferenceConfig": { "maxTokens": 128000 } }
        }
      }
    }
  },
  "mcp": {
    "aws-docs": {
      "type": "local",
      "command": ["awslabs.aws-documentation-mcp-server"],
      "enabled": true,
      "environment": {
        "FASTMCP_LOG_LEVEL": "ERROR",
        "AWS_DOCUMENTATION_PARTITION": "aws"
      }
    },
    "aws-mcp": {
      "type": "local",
      "command": ["mcp-proxy-for-aws-cli", "${AWS_MCP_ENDPOINT}", "--metadata", "AWS_REGION=${SEED_REGION}", "--read-only", "--log-level", "ERROR"],
      "enabled": true
    },
    "context7": {
      "type": "local",
      "command": ["context7-mcp"],
      "enabled": false
    }
  }
}
EOF
        log "seeded default OpenCode config (providers.amazon-bedrock region=${SEED_REGION} with Claude maxTokens overrides, default_agent=remote-interactive, mcp.aws-docs, mcp.aws-mcp, mcp.context7 disabled) at ${OPENCODE_CONFIG_FILE}"
    else
        log "OpenCode config already present, leaving untouched"
    fi
elif [ "${HARNESS}" = "pi" ]; then
    # --- add-pi-harness (design D4/D8): per-file idempotent seeding -----------
    # Pi has no agent files and no MCP, so the whole SCH contract lives in four
    # artifacts under PI_CONFIG_DIR. Seeding is PER FILE and never overwrites:
    # an operator edit survives every boot, and deleting a file re-seeds the
    # current image version at the next boot (same contract as the opencode
    # agents and the claude templates).
    #
    # settings.json is the mandatory one: Pi's own `--provider` default is
    # `google`, so without this seed the TUI would start on a provider with no
    # credentials in this microVM (design.md OQ-PI-BEDROCK). It also carries
    # defaultProjectTrust=always, so no non-interactive path can ever park on
    # Pi's project-trust prompt (design D8).
    if [ -d "${PI_TEMPLATE_DIR}" ]; then
        if [ -f "${PI_TEMPLATE_DIR}/settings.json" ] && [ ! -f "${PI_SETTINGS_FILE}" ]; then
            # The template carries a __SCH_BEDROCK_MODEL__ placeholder so the
            # region-appropriate inference profile is chosen at seed time.
            sed "s|__SCH_BEDROCK_MODEL__|${PI_SEED_MODEL}|" \
                "${PI_TEMPLATE_DIR}/settings.json" > "${PI_SETTINGS_FILE}"
            log "seeded Pi settings.json (defaultProvider=amazon-bedrock, defaultModel=${PI_SEED_MODEL}, defaultProjectTrust=always) at ${PI_SETTINGS_FILE}"
        elif [ -f "${PI_SETTINGS_FILE}" ]; then
            log "Pi settings.json already present, leaving untouched"
        fi
        if [ -f "${PI_TEMPLATE_DIR}/AGENTS.md" ] && [ ! -f "${PI_GLOBAL_MD}" ]; then
            cp "${PI_TEMPLATE_DIR}/AGENTS.md" "${PI_GLOBAL_MD}"
            log "seeded global Pi AGENTS.md (microVM environment brief) at ${PI_GLOBAL_MD}"
        fi
        # Role system prompts (design D4): Pi has no agent files, so the
        # remote-auto / remote-interactive contracts are rendered as
        # --append-system-prompt inputs. The shim reads remote-auto.md for the
        # headless argv; the autostart reads remote-interactive.md for the TUI.
        for tmpl in "${PI_TEMPLATE_DIR}"/roles/*.md; do
            [ -f "${tmpl}" ] || continue
            mkdir -p "${PI_ROLES_DIR}"
            dest="${PI_ROLES_DIR}/$(basename "${tmpl}")"
            if [ ! -f "${dest}" ]; then
                cp "${tmpl}" "${dest}"
                log "seeded Pi role $(basename "${tmpl}" .md) at ${dest}"
            fi
        done
        # SCH extension (design D6). Like the opencode plugin — and unlike the
        # role prompts — sch-MANAGED extensions are REFRESHED on image upgrades:
        # the extension dir rides the checkpointed L2 replica, so a
        # never-overwrite policy would pin existing workspaces to the extension
        # version that first seeded them forever. Ownership is tracked with the
        # same `<name>.sch-seeded` sha256 sidecar contract: a destination that
        # still matches its sidecar is sch-managed and safe to refresh, a
        # destination without a sidecar is adopted only if it carries the
        # template's own header marker, and anything else is an operator file
        # and is never touched.
        for tmpl in "${PI_TEMPLATE_DIR}"/extensions/*.ts; do
            [ -f "${tmpl}" ] || continue
            mkdir -p "${PI_EXTENSIONS_DIR}"
            dest="${PI_EXTENSIONS_DIR}/$(basename "${tmpl}")"
            sidecar="${dest}.sch-seeded"
            tmpl_sha="$(sha256_of "${tmpl}")"
            if [ ! -f "${dest}" ]; then
                cp "${tmpl}" "${dest}"
                printf '%s\n' "${tmpl_sha}" > "${sidecar}"
                log "seeded Pi extension $(basename "${tmpl}") at ${dest}"
                continue
            fi
            dest_sha="$(sha256_of "${dest}")"
            if [ "${dest_sha}" = "${tmpl_sha}" ]; then
                printf '%s\n' "${tmpl_sha}" > "${sidecar}"
                continue
            fi
            managed="no"
            if [ -f "${sidecar}" ] && [ "$(head -n1 "${sidecar}" 2>/dev/null)" = "${dest_sha}" ]; then
                managed="yes"
            elif [ ! -f "${sidecar}" ] && grep -q "SCH extension for Pi" "${dest}" 2>/dev/null; then
                managed="yes"
            fi
            if [ "${managed}" = "yes" ]; then
                cp "${tmpl}" "${dest}"
                printf '%s\n' "${tmpl_sha}" > "${sidecar}"
                log "refreshed sch-managed Pi extension $(basename "${tmpl}") at ${dest}"
            else
                log "left operator Pi extension $(basename "${tmpl}") untouched at ${dest}"
            fi
        done
    else
        # Out-of-image runs only (the Dockerfile asserts the templates exist).
        log "WARNING: Pi template dir ${PI_TEMPLATE_DIR} missing, skipping Pi template seeding"
    fi
    # --- Gateway models.json reconciliation (extend-pi-gateway-keys, TASK-18) --
    # Pi consumes OPENCODE_API_KEY / KILO_API_KEY through its custom-provider
    # mechanism: models.json in the config dir. The gateway blocks come from
    # the build-time generated template (models.dev snapshot); this is a
    # RECONCILIATION in both directions, recomputed at every bootstrap: the
    # workspace state is checkpointed, so a registration made by a PREVIOUS
    # session must be withdrawn when the key is no longer staged, or /model
    # would keep offering a provider this session has no credential for.
    #
    # Ownership is per provider key, tracked in a sidecar listing the provider
    # ids SCH owns: an SCH-owned block is upserted/withdrawn freely, a block
    # the operator wrote under the same id is never touched (and never
    # overwritten by our upsert). Only "$ENV_VAR" references are ever written;
    # key values stay in the process environment. Same never-trace rule as the
    # claude approval reconciliation: no staged keys and no sidecar means no
    # file is created and nothing is written.
    PI_GATEWAY_TEMPLATE="${PI_TEMPLATE_DIR}/gateway-models.json"
    if [ -f "${PI_GATEWAY_TEMPLATE}" ]; then
        PI_MODELS_FILE="${PI_CONFIG_DIR}/models.json"
        PI_GATEWAY_SIDECAR="${PI_CONFIG_DIR}/models.json.sch-gateways"
        rec_output_file="$(mktemp)"
        rec_rc=0
        python3 - "${PI_MODELS_FILE}" "${PI_GATEWAY_SIDECAR}" "${PI_GATEWAY_TEMPLATE}" \
            >"${rec_output_file}" <<'PYEOF' || rec_rc=$?
import json, os, sys, tempfile

models_path, sidecar_path, template_path = sys.argv[1:4]
# provider id in models.json -> the SCH_-prefixed staged name carrying its
# key (init-workspace runs on the shim's child env, which stages the keys
# under the inert SCH_ prefix — same names the dispatcher maps onto the
# canonical ones per harness).
GATEWAYS = {"opencode": "SCH_OPENCODE_API_KEY", "kilo": "SCH_KILO_API_KEY"}
staged = {
    pid for pid, env_name in GATEWAYS.items()
    if os.environ.get(env_name, "").strip()
}

try:
    with open(template_path, "r", encoding="utf-8") as f:
        template = json.load(f)
    template_blocks = template.get("providers") or {}
except Exception as exc:
    print("template unreadable: {}".format(exc))
    sys.exit(4)

existing = {}
if os.path.exists(models_path):
    try:
        with open(models_path, "r", encoding="utf-8") as f:
            existing = json.load(f)
    except Exception:
        print("existing models.json is not parseable; leaving it untouched")
        sys.exit(3)
    if not isinstance(existing, dict):
        print("existing models.json is not a JSON object; leaving it untouched")
        sys.exit(3)
providers = existing.setdefault("providers", {})
if not isinstance(providers, dict):
    print("existing models.json providers is not an object; leaving it untouched")
    sys.exit(3)

owned = set()
if os.path.exists(sidecar_path):
    try:
        with open(sidecar_path, "r", encoding="utf-8") as f:
            owned = set(f.read().split())
    except OSError:
        owned = set()

changed, skipped = [], []
for pid in sorted(template_blocks):
    if pid not in GATEWAYS:
        continue
    block = template_blocks[pid]
    if pid in staged:
        if pid in providers and providers[pid] != block and pid not in owned:
            # An operator block under the same id: never ours to overwrite.
            skipped.append(pid)
            continue
        providers[pid] = block
        owned.add(pid)
        changed.append("merged {}".format(pid))
    elif pid in owned:
        del providers[pid]
        owned.discard(pid)
        changed.append("withdrew {}".format(pid))

if not changed:
    for pid in skipped:
        print("skipped {} (operator-owned block)".format(pid))
    print("unchanged")
    sys.exit(0)

for pid in skipped:
    print("skipped {} (operator-owned block)".format(pid))
print("; ".join(changed))

if not providers:
    del existing["providers"]

def dump(document, path):
    dirpath = os.path.dirname(path) or "."
    os.makedirs(dirpath, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".models-", suffix=".json", dir=dirpath)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(document, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        try: os.unlink(tmp)
        except Exception: pass
        raise

# Nothing left in the document (every provider was ours and was withdrawn):
# remove the file instead of shipping an empty {} — same no-trace posture as
# a session that never staged a key.
if existing:
    dump(existing, models_path)
elif os.path.exists(models_path):
    os.unlink(models_path)
if owned:
    with open(sidecar_path, "w", encoding="utf-8") as f:
        f.write(" ".join(sorted(owned)) + "\n")
    os.chmod(sidecar_path, 0o600)
elif os.path.exists(sidecar_path):
    os.unlink(sidecar_path)
PYEOF
        rec_output="$(<"${rec_output_file}")"
        rm -f "${rec_output_file}"
        case "${rec_rc}" in
            0) log "pi gateway models.json: ${rec_output}" ;;
            3) log "WARNING: pi gateway models.json: ${rec_output}" ;;
            *) log "WARNING: pi gateway models.json reconciliation failed (rc=${rec_rc}${rec_output:+, ${rec_output}})" ;;
        esac
    elif [ -n "${SCH_OPENCODE_API_KEY:-}" ] || [ -n "${SCH_KILO_API_KEY:-}" ]; then
        # The image was built while models.dev was unreachable (the build
        # degrades instead of failing — see the Dockerfile), so the gateway
        # providers cannot be offered even though the user configured their
        # keys. Say so: a staged key with no visible provider is precisely the
        # silent misconfiguration this capability exists to avoid. NAMES only.
        log "WARNING: gateway key staged but ${PI_GATEWAY_TEMPLATE} is missing;" \
            "OpenCode Zen / Kilo will not appear in pi (rebuild the image to" \
            "regenerate the catalog)"
    fi
    # --- Trust pre-seed (design D8) -------------------------------------------
    # defaultProjectTrust=always in the seeded settings already covers every
    # path, but an operator who edits that setting back to "ask" must still not
    # find non-interactive runs parked on a prompt for the canonical worktree.
    # Pre-decide trust for REPO_DIR explicitly, additively and idempotently:
    # other entries (the operator's own /trust decisions) are preserved, and an
    # existing decision for REPO_DIR — including a deliberate `false` — is never
    # overwritten.
    trust_output="$(python3 - "${PI_TRUST_FILE}" "${REPO_DIR}" <<'PYEOF'
import json, os, sys, tempfile
path, repo_dir = sys.argv[1], os.path.realpath(sys.argv[2])
data = {}
if os.path.exists(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        print("unparseable")
        sys.exit(3)
    if not isinstance(data, dict):
        print("not-object")
        sys.exit(3)
# Never overwrite an existing decision for this path (True or False).
if repo_dir in data:
    sys.exit(0)
data[repo_dir] = True
dirpath = os.path.dirname(path) or "."
os.makedirs(dirpath, exist_ok=True)
fd, tmp = tempfile.mkstemp(prefix=".trust-", suffix=".json", dir=dirpath)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(dict(sorted(data.items())), f, indent=2)
        f.write("\n")
    os.replace(tmp, path)
except Exception:
    try: os.unlink(tmp)
    except Exception: pass
    raise
print("written")
PYEOF
    )"
    trust_rc=$?
    case "${trust_rc}" in
        0)
            if [ "${trust_output}" = "written" ]; then
                log "pre-seeded Pi project trust for ${REPO_DIR} in ${PI_TRUST_FILE}"
            else
                log "Pi project trust for ${REPO_DIR} already decided, leaving untouched"
            fi
            ;;
        3) log "WARNING: ${PI_TRUST_FILE} is not parseable as a JSON object; leaving it unchanged" ;;
        *) log "WARNING: could not pre-seed Pi project trust (rc=${trust_rc}${trust_output:+, ${trust_output}})" ;;
    esac
else  # HARNESS = claude
    # Claude Code rides Bedrock via CLAUDE_CODE_USE_BEDROCK=1 (exported by the
    # image ENV and the dispatcher); no provider block is needed in any
    # config file. `.mcp.json` (project-scoped, aws-docs/aws-mcp/context7) is
    # seeded in section 4 below, AFTER the repo bootstrap (section 3) — it
    # must live at REPO_DIR/.mcp.json (sch-context7-builtin follow-up fix),
    # not under CLAUDE_CONFIG_DIR, so seeding it here (before the repo even
    # exists/is cloned) would be premature.
    #
    # settings.json is optional (Claude Code works without it). Seed a minimal
    # one only when absent, recording the Bedrock-on default so the TUI shows
    # the operator's intent. NEVER overwrite an existing settings.json.
    #
    # add-provider-api-keys (task 3.3): Claude Code applies `env` from
    # settings.json ON TOP of the process environment (verified against the
    # pinned binary: a settings value WINS over an inherited one), so this entry
    # would silently re-enable Bedrock and defeat the dispatcher's
    # CLAUDE_CODE_USE_BEDROCK=0 override. It therefore tracks the presence of
    # SCH_ANTHROPIC_API_KEY: "1" without a key, "0" with one.
    if [ ! -f "${CLAUDE_SETTINGS_FILE}" ]; then
        cat > "${CLAUDE_SETTINGS_FILE}" <<EOF
{
  "agent": "remote-interactive",
  "env": {
    "CLAUDE_CODE_USE_BEDROCK": "${CLAUDE_BEDROCK_SETTING}"
  }
}
EOF
        log "seeded Claude settings.json (agent=remote-interactive, env.CLAUDE_CODE_USE_BEDROCK=${CLAUDE_BEDROCK_SETTING}) at ${CLAUDE_SETTINGS_FILE}"
    else
        log "Claude settings.json already present, leaving untouched"
    fi
    # sch-context7-builtin: ensure `context7` is in `disabledMcpjsonServers`
    # of `settings.json` (the official Claude Code mechanism to keep an
    # `.mcp.json` entry from connecting — code.claude.com/docs/en/mcp).
    # add-telegram-notifications (tasks 3.2/3.3): the same merge also seeds
    # the milestone hooks (Stop / Notification / PostToolUse:TodoWrite →
    # telegram-hook.py, spool-only, no network, no-op without Telegram env).
    # Merge is ADDITIVE and idempotent: operator hook entries are never
    # touched, pre-existing keys are never lost, and our entries are
    # recognized by the 'hooks/telegram-hook.py <kind>' command marker.
    # add-telegram-interaction (task 3.3): PreToolUse (decisional remote
    # approval) and PostToolUse:native (dual-control) are seeded too — both
    # immediate no-ops unless the inbound channel is configured.
    # add-task-liveness-safety (task 4.1, design D4): PostToolUse with a broad
    # matcher emits in-turn `tool` digests — without it a headless claude turn
    # is indistinguishable from a dead microVM for tens of minutes.
    # Idempotent merge: preserves all other fields, no-ops if everything is
    # already in place. Skipped entirely if `settings.json` doesn't exist (the
    # user has explicitly chosen not to have one; respect that and don't
    # create the file just to disable a single MCP server).
    if [ -f "${CLAUDE_SETTINGS_FILE}" ]; then
        merge_output_file="$(mktemp)"
        python3 - "${CLAUDE_SETTINGS_FILE}" "${CLAUDE_TEMPLATE_DIR}" "${CLAUDE_BEDROCK_SETTING}" >"${merge_output_file}" <<'PYEOF'
import json, sys, tempfile, os
path = sys.argv[1]
template_dir = sys.argv[2]
bedrock_setting = sys.argv[3]
try:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
except Exception:
    print("unparseable")
    sys.exit(3)
if not isinstance(data, dict):
    print("not-object")
    sys.exit(3)
changed = False
if "agent" not in data:
    data["agent"] = "remote-interactive"
    changed = True
disabled = data.get("disabledMcpjsonServers")
if not (isinstance(disabled, list) and "context7" in disabled):
    if not isinstance(disabled, list):
        disabled = []
    else:
        disabled = [x for x in disabled if isinstance(x, str) and x]
    if "context7" not in disabled:
        disabled.append("context7")
    data["disabledMcpjsonServers"] = disabled
    changed = True
# --- add-provider-api-keys (task 3.3): keep env.CLAUDE_CODE_USE_BEDROCK aligned
# --- with the presence of the Anthropic key -----------------------------------
# Claude Code applies settings.json `env` ON TOP of the process environment, so
# a stale "1" here would override the dispatcher's CLAUDE_CODE_USE_BEDROCK=0 and
# keep sending traffic to Bedrock even with SCH_ANTHROPIC_API_KEY set (and,
# symmetrically, a stale "0" would leave a rolled-back deployment with no
# provider at all). Ownership is by VALUE: only the two values this script ever
# writes ("1"/"0") are managed; anything else is an operator choice and is left
# untouched. An `env` of an unexpected shape is left alone rather than "fixed".
env_block = data.get("env")
if isinstance(env_block, dict):
    current = env_block.get("CLAUDE_CODE_USE_BEDROCK")
    if current in ("1", "0") and current != bedrock_setting:
        env_block["CLAUDE_CODE_USE_BEDROCK"] = bedrock_setting
        changed = True
# --- Telegram milestone + approval hooks (add-telegram-notifications,
# --- add-telegram-interaction) ------------------------------------------------
# Additive merge keyed on the command marker: an event list already containing
# the SAME telegram-hook.py kind is left untouched (idempotence), any other
# operator entry is preserved as-is (never-overwrite), and a `hooks` key of an
# unexpected shape is left alone rather than "fixed".
# add-telegram-interaction (task 3.3): PreToolUse becomes DECISIONAL — the
# hook waits for the remote decision with a bounded timeout (default 600s,
# SCH_APPROVAL_TIMEOUT_S) so the hook timeout below (660s) is never hit on
# the fail-safe path. The hook itself is an immediate no-op when the inbound
# channel is not configured (no commands table in the environment).
# PostToolUse/native closes the dual-control loop (task 3.5).
# add-task-liveness-safety (task 4.1, design D4): the PostToolUse/tool entry
# has a deliberately broad matcher (".*") — it is the in-turn liveness signal,
# so it must fire for EVERY tool call. The hook itself drops the TodoWrite
# calls (already covered by the `todo` kind) so the two entries never produce
# two events for the same tool call.
HOOK_MARKER = "hooks/telegram-hook.py"
hook_script = os.path.join(template_dir, "hooks", "telegram-hook.py")
def _hook_entry(kind, matcher=None, timeout=10):
    entry = {"hooks": [{
        "type": "command",
        "command": f"python3 {hook_script} {kind}",
        "timeout": timeout,
    }]}
    if matcher is not None:
        entry["matcher"] = matcher
    return entry
def _has_kind(groups, kind):
    suffix = f"{HOOK_MARKER} {kind}"
    for group in groups:
        if not isinstance(group, dict):
            continue
        for hook in group.get("hooks") or []:
            if isinstance(hook, dict) and str(hook.get("command", "")).endswith(suffix):
                return True
    return False
hooks = data.get("hooks")
if hooks is None:
    hooks = {}
    data["hooks"] = hooks
# Matcher for the decisional/native pair: the tools claude prompts a human
# for by default — read-only tools must never gain a remote wait.
APPROVAL_MATCHER = "Bash|Write|Edit|MultiEdit|NotebookEdit|WebFetch"
if isinstance(hooks, dict):
    for event, kind, matcher, timeout in (
        ("Stop", "stop", None, 10),
        ("Notification", "notification", None, 10),
        ("PostToolUse", "todo", "TodoWrite", 10),
        ("PreToolUse", "pretooluse", APPROVAL_MATCHER, 660),
        ("PostToolUse", "native", APPROVAL_MATCHER, 10),
        ("PostToolUse", "tool", ".*", 10),
    ):
        groups = hooks.get(event)
        if groups is None:
            groups = []
            hooks[event] = groups
        if not isinstance(groups, list) or _has_kind(groups, kind):
            continue
        groups.append(_hook_entry(kind, matcher, timeout))
        changed = True
if not changed:
    sys.exit(0)
# Atomic write: tmp in the same dir, then rename (mount-safe).
dirpath = os.path.dirname(path) or "."
fd, tmp = tempfile.mkstemp(prefix=".settings-", suffix=".json", dir=dirpath)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)
except Exception:
    try: os.unlink(tmp)
    except Exception: pass
    raise
PYEOF
        merge_rc=$?
        merge_output="$(<"${merge_output_file}")"
        rm -f "${merge_output_file}"
        case "${merge_rc}" in
            0) log "ensured agent default, context7 and telegram hook settings in ${CLAUDE_SETTINGS_FILE}" ;;
            3) log "WARNING: ${CLAUDE_SETTINGS_FILE} is not parseable as a JSON object; leaving it unchanged" ;;
            *) log "WARNING: could not merge Claude defaults into ${CLAUDE_SETTINGS_FILE} (rc=${merge_rc}${merge_output:+, ${merge_output}})" ;;
        esac
    fi
    # --- 2e. Reconcile the Anthropic API key approval (add-provider-api-keys D4,
    # --- add-user-provider-keys D6) -------------------------------------------
    # With ANTHROPIC_API_KEY coming from the environment, Claude Code asks the
    # user once, interactively, to confirm the key it detected, and persists the
    # answer in its global config file as the LAST 20 CHARACTERS of the key
    # (`customApiKeyResponses.approved`). Pre-seeding that answer keeps the TUI
    # from opening a modal on first launch — and keeps any non-interactive path
    # from ever parking on it.
    #
    # Only the 20-char suffix is written, never the key.
    #
    # add-user-provider-keys (design D6): the key is now a per-USER, per-session
    # property, so this is a RECONCILIATION in both directions, recomputed at
    # every bootstrap — not a one-way deploy-time seed. With no key in the
    # session, a registration made by a PREVIOUS session (the workspace is
    # checkpointed, the config file survives) must be withdrawn, or the TUI would
    # keep a stale approval for a credential this session does not have. Which
    # entry is ours is remembered in a sidecar marker: an approval the OPERATOR
    # added by hand is never touched. No file is created and nothing is written
    # when there is neither a key nor a marker — a Bedrock-only session leaves no
    # trace at all.
    #
    # Same resolution as the pinned binary: `.config.json` wins when it already
    # exists, otherwise `.claude.json`, both inside CLAUDE_CONFIG_DIR.
    if [ -f "${CLAUDE_CONFIG_DIR}/.config.json" ]; then
        CLAUDE_GLOBAL_CONFIG_FILE="${CLAUDE_CONFIG_DIR}/.config.json"
    else
        CLAUDE_GLOBAL_CONFIG_FILE="${CLAUDE_CONFIG_DIR}/.claude.json"
    fi
    # Sidecar marker holding ONLY the 20-char suffix this script registered
    # (same secrecy class as the approval itself, mode 0600).
    CLAUDE_APPROVED_KEY_MARKER="${CLAUDE_CONFIG_DIR}/.sch-approved-api-key"
    if [ -n "${SCH_ANTHROPIC_API_KEY:-}" ] || [ -f "${CLAUDE_APPROVED_KEY_MARKER}" ]; then
        approve_output_file="$(mktemp)"
        python3 - "${CLAUDE_GLOBAL_CONFIG_FILE}" "${CLAUDE_APPROVED_KEY_MARKER}" >"${approve_output_file}" <<'PYEOF'
import json, os, sys, tempfile
path = sys.argv[1]
marker = sys.argv[2]
key = os.environ.get("SCH_ANTHROPIC_API_KEY", "")
suffix = key[-20:] if key else ""
# The suffix a previous session of this workspace registered, if any. Only that
# value is ever withdrawn: an approval added by the operator (or by Claude Code
# itself, answering the modal) is not ours to remove.
try:
    with open(marker, "r", encoding="utf-8") as f:
        previous = f.read().strip()
except OSError:
    previous = ""
if not key and not previous:
    # Nothing to register and nothing of ours to withdraw: never create the
    # global config file for a Bedrock-only session.
    sys.exit(0)
data = {}
if os.path.exists(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        print("unparseable")
        sys.exit(3)
    if not isinstance(data, dict):
        print("not-object")
        sys.exit(3)
elif not key:
    # Our marker outlived the config file (operator deleted it): just forget.
    try:
        os.unlink(marker)
    except OSError:
        pass
    print("withdrawn")
    sys.exit(0)
responses = data.get("customApiKeyResponses")
have_responses = isinstance(responses, dict)
if not have_responses:
    responses = {}
approved_raw = responses.get("approved")
rejected_raw = responses.get("rejected")
approved = [x for x in (approved_raw or []) if isinstance(x, str)]
rejected = [x for x in (rejected_raw or []) if isinstance(x, str)]
if previous and previous != suffix:
    # Key removed or rotated (design D6): withdraw the entry we own.
    approved = [x for x in approved if x != previous]
    rejected = [x for x in rejected if x != previous]
if key:
    if suffix not in approved:
        approved.append(suffix)
    # Approving means the key is no longer rejected (this is what Claude Code's
    # own approve path does): a suffix left in `rejected` is stale bookkeeping.
    rejected = [x for x in rejected if x != suffix]
# Record (or forget) what we own BEFORE touching the config: a crash between the
# two then leaves a stale marker (withdrawn on the next boot, harmless) rather
# than an approval nobody claims.
try:
    if key:
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(suffix + "\n")
    else:
        os.unlink(marker)
except OSError:
    pass
if have_responses and approved == approved_raw and rejected == rejected_raw:
    sys.exit(0)
if not have_responses and not approved and not rejected:
    # Nothing of ours was registered after all: leave the file untouched.
    sys.exit(0)
responses["approved"] = approved
responses["rejected"] = rejected
data["customApiKeyResponses"] = responses
# Atomic write, 0600: the file holds the key SUFFIX plus Claude Code's own
# per-project bookkeeping. Preserve an existing file's mode.
dirpath = os.path.dirname(path) or "."
mode = 0o600
try:
    mode = os.stat(path).st_mode & 0o777
except OSError:
    pass
fd, tmp = tempfile.mkstemp(prefix=".claude-config-", suffix=".json", dir=dirpath)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.chmod(tmp, mode)
    os.replace(tmp, path)
except Exception:
    try: os.unlink(tmp)
    except Exception: pass
    raise
print("written" if key else "withdrawn")
PYEOF
        approve_rc=$?
        approve_output="$(<"${approve_output_file}")"
        rm -f "${approve_output_file}"
        case "${approve_rc}" in
            0)
                case "${approve_output}" in
                    written) log "pre-approved the Anthropic API key (suffix only) in ${CLAUDE_GLOBAL_CONFIG_FILE}" ;;
                    withdrawn) log "withdrew this workspace's pre-approved Anthropic API key (no key in this session) from ${CLAUDE_GLOBAL_CONFIG_FILE}" ;;
                    *) log "Anthropic API key approval already reconciled in ${CLAUDE_GLOBAL_CONFIG_FILE}" ;;
                esac
                ;;
            3) log "WARNING: ${CLAUDE_GLOBAL_CONFIG_FILE} is not parseable as a JSON object; leaving it unchanged" ;;
            *) log "WARNING: could not reconcile the Anthropic API key approval (rc=${approve_rc}${approve_output:+, ${approve_output}})" ;;
        esac
    fi
fi

# --- 2c. Seed Claude agents + global CLAUDE.md (per-file) --------------------
# User-scoped policy is persisted by the existing CLAUDE_CONFIG_DIR mirror.
# Never replace operator customizations; older workspaces receive only files
# that are missing. This runs before the shim can write Claude's ready marker.
if [ "${HARNESS}" = "claude" ]; then
    if [ -d "${CLAUDE_TEMPLATE_DIR}" ]; then
        mkdir -p "${CLAUDE_AGENTS_DIR}"
        if [ -f "${CLAUDE_TEMPLATE_DIR}/CLAUDE.md" ] && [ ! -f "${CLAUDE_GLOBAL_MD}" ]; then
            cp "${CLAUDE_TEMPLATE_DIR}/CLAUDE.md" "${CLAUDE_GLOBAL_MD}"
            log "seeded global Claude CLAUDE.md at ${CLAUDE_GLOBAL_MD}"
        fi
        for tmpl in "${CLAUDE_TEMPLATE_DIR}"/agents/*.md; do
            [ -f "${tmpl}" ] || continue
            dest="${CLAUDE_AGENTS_DIR}/$(basename "${tmpl}")"
            if [ ! -f "${dest}" ]; then
                cp "${tmpl}" "${dest}"
                log "seeded Claude agent $(basename "${tmpl}" .md) at ${dest}"
            fi
        done
    else
        log "WARNING: Claude template dir ${CLAUDE_TEMPLATE_DIR} missing, skipping agent seeding"
    fi
fi

# --- 2b. Seed OpenCode custom agents + global AGENTS.md (per-file) -----------
# sch-remote-agents: two custom primary agents tuned for the microVM harness
# (remote-interactive for interactive TUI sessions — the seeded config's
# default_agent — and remote-auto for the headless task path, selected by the
# shim via `opencode run --agent remote-auto`, see app/main.py) plus a global
# AGENTS.md carrying the microVM environment brief, which OpenCode appends to
# EVERY agent's system prompt (built-ins included).
#
# Idempotence is PER FILE and NOT gated on opencode.json existing: workspaces
# created before this feature pick the agents up on their next boot, while
# any file the operator has edited or removed-and-recreated is never
# overwritten (same never-overwrite property as the config seed above).
if [ "${HARNESS}" = "opencode" ]; then
    if [ -d "${OPENCODE_TEMPLATE_DIR}" ]; then
        mkdir -p "${OPENCODE_AGENTS_DIR}"
        if [ -f "${OPENCODE_TEMPLATE_DIR}/AGENTS.md" ] && [ ! -f "${OPENCODE_GLOBAL_AGENTS_MD}" ]; then
            cp "${OPENCODE_TEMPLATE_DIR}/AGENTS.md" "${OPENCODE_GLOBAL_AGENTS_MD}"
            log "seeded global AGENTS.md (microVM environment brief) at ${OPENCODE_GLOBAL_AGENTS_MD}"
        fi
        for tmpl in "${OPENCODE_TEMPLATE_DIR}"/agents/*.md; do
            [ -f "${tmpl}" ] || continue
            dest="${OPENCODE_AGENTS_DIR}/$(basename "${tmpl}")"
            if [ ! -f "${dest}" ]; then
                cp "${tmpl}" "${dest}"
                log "seeded OpenCode agent $(basename "${tmpl}" .md) at ${dest}"
            fi
        done
        # add-telegram-notifications (task 4.2): milestone plugin. Unlike the
        # agents above, sch-MANAGED plugins are refreshed on image upgrades
        # (add-interactive-busy-keepalive): the plugin dir lives in the
        # checkpointed state/config, so a never-overwrite policy would pin
        # existing workspaces to the plugin version that first seeded them
        # forever. Ownership tracking: seeding records the seeded content's
        # sha256 in a `<name>.js.sch-seeded` sidecar; a destination that
        # still matches its sidecar is sch-managed and safe to refresh. A
        # destination WITHOUT a sidecar (workspace seeded by a pre-sidecar
        # image) is adopted as sch-managed only when it carries the template
        # header marker ("seeded by init-workspace.sh"); anything else is an
        # operator file and is never touched.
        for tmpl in "${OPENCODE_TEMPLATE_DIR}"/plugin/*.js; do
            [ -f "${tmpl}" ] || continue
            dest="${OPENCODE_CONFIG_DIR}/plugin/$(basename "${tmpl}")"
            sidecar="${dest}.sch-seeded"
            tmpl_sha="$(sha256_of "${tmpl}")"
            if [ ! -f "${dest}" ]; then
                mkdir -p "${OPENCODE_CONFIG_DIR}/plugin"
                cp "${tmpl}" "${dest}"
                printf '%s\n' "${tmpl_sha}" > "${sidecar}"
                log "seeded OpenCode plugin $(basename "${tmpl}") at ${dest}"
                continue
            fi
            dest_sha="$(sha256_of "${dest}")"
            if [ "${dest_sha}" = "${tmpl_sha}" ]; then
                # Already current: (re)write the sidecar so a legacy workspace
                # graduates to explicit ownership tracking.
                printf '%s\n' "${tmpl_sha}" > "${sidecar}"
                continue
            fi
            managed="no"
            if [ -f "${sidecar}" ] && [ "$(head -n1 "${sidecar}" 2>/dev/null)" = "${dest_sha}" ]; then
                managed="yes"
            elif [ ! -f "${sidecar}" ] && grep -q "seeded by init-workspace.sh" "${dest}" 2>/dev/null; then
                managed="yes"
            fi
            if [ "${managed}" = "yes" ]; then
                cp "${tmpl}" "${dest}"
                printf '%s\n' "${tmpl_sha}" > "${sidecar}"
                log "refreshed sch-managed OpenCode plugin $(basename "${tmpl}") at ${dest}"
            else
                log "left operator OpenCode plugin $(basename "${tmpl}") untouched at ${dest}"
            fi
        done
        # seed-opencode-openspec-workflows: global user-scope defaults, never
        # project-scoped files. This runs every bootstrap independently of
        # opencode.json so older workspaces receive missing workflows without
        # an `openspec init` invocation or any write under REPO_DIR.
        if [ -d "${OPENCODE_TEMPLATE_DIR}/commands" ] && [ -d "${OPENCODE_TEMPLATE_DIR}/skills" ]; then
            seeded_opsx=0
            for tmpl in "${OPENCODE_TEMPLATE_DIR}"/commands/opsx-*.md; do
                [ -f "${tmpl}" ] || continue
                dest="${OPENCODE_CONFIG_DIR}/commands/$(basename "${tmpl}")"
                if [ ! -f "${dest}" ]; then
                    mkdir -p "${OPENCODE_CONFIG_DIR}/commands"
                    cp "${tmpl}" "${dest}"
                    seeded_opsx=$((seeded_opsx + 1))
                fi
            done
            seeded_skills=0
            for tmpl in "${OPENCODE_TEMPLATE_DIR}"/skills/openspec-*/SKILL.md; do
                [ -f "${tmpl}" ] || continue
                skill_name="$(basename "$(dirname "${tmpl}")")"
                dest="${OPENCODE_CONFIG_DIR}/skills/${skill_name}/SKILL.md"
                if [ ! -f "${dest}" ]; then
                    mkdir -p "${OPENCODE_CONFIG_DIR}/skills/${skill_name}"
                    cp "${tmpl}" "${dest}"
                    seeded_skills=$((seeded_skills + 1))
                fi
            done
            if [ "${seeded_opsx}" -gt 0 ] || [ "${seeded_skills}" -gt 0 ]; then
                log "seeded OpenSpec OpenCode artifacts (${seeded_opsx} commands in ${OPENCODE_CONFIG_DIR}/commands, ${seeded_skills} skills in ${OPENCODE_CONFIG_DIR}/skills)"
            else
                log "OpenSpec OpenCode artifacts already present, leaving untouched"
            fi
        else
            log "WARNING: OpenCode OpenSpec templates missing under ${OPENCODE_TEMPLATE_DIR}, skipping workflow seeding"
        fi
    else
        # Out-of-image runs only (the Dockerfile asserts the templates exist):
        # warn and continue — the workspace still works on built-in agents.
        log "WARNING: OpenCode template dir ${OPENCODE_TEMPLATE_DIR} missing, skipping global template seeding"
    fi
fi

# --- 2c. Seed Claude Code OpenSpec commands + skills (per-file) ---------------
# add-claude-openspec-commands: user-scope artifacts so the /opsx:* workflow
# commands (incl. /opsx:ff) and openspec-* skills are available in every
# project the operator opens with `claude` in this microVM. Destination is
# CLAUDE_CONFIG_DIR (NOT the repo worktree: two directory trees would pollute
# the operator's `git status`, and a cloned repo shipping its own .claude/
# must never be touched) — it already rides the L2 claude-state mirror, so
# the seeded files persist with zero new checkpoint machinery.
#
# Ordering is safe by construction: the shim restores the L2 replica into
# $HOME/.claude (_restore_claude_state) BEFORE running this script, so the
# per-file never-overwrite check below always sees the operator's restored
# files first. Same idempotence contract as the OpenCode agents (2b):
# operator edits/deletions of individual files survive every boot, and
# workspaces created before this feature pick the files up on their next
# boot (not gated on first-boot).
if [ "${HARNESS}" = "claude" ]; then
    if [ -d "${CLAUDE_TEMPLATE_DIR}" ]; then
        seeded_opsx=0
        for tmpl in "${CLAUDE_TEMPLATE_DIR}"/commands/opsx/*.md; do
            [ -f "${tmpl}" ] || continue
            dest="${CLAUDE_CONFIG_DIR}/commands/opsx/$(basename "${tmpl}")"
            if [ ! -f "${dest}" ]; then
                mkdir -p "${CLAUDE_CONFIG_DIR}/commands/opsx"
                cp "${tmpl}" "${dest}"
                seeded_opsx=$((seeded_opsx + 1))
            fi
        done
        seeded_skills=0
        for tmpl in "${CLAUDE_TEMPLATE_DIR}"/skills/*/SKILL.md; do
            [ -f "${tmpl}" ] || continue
            skill_name="$(basename "$(dirname "${tmpl}")")"
            dest="${CLAUDE_CONFIG_DIR}/skills/${skill_name}/SKILL.md"
            if [ ! -f "${dest}" ]; then
                mkdir -p "${CLAUDE_CONFIG_DIR}/skills/${skill_name}"
                cp "${tmpl}" "${dest}"
                seeded_skills=$((seeded_skills + 1))
            fi
        done
        if [ "${seeded_opsx}" -gt 0 ] || [ "${seeded_skills}" -gt 0 ]; then
            log "seeded OpenSpec Claude artifacts (${seeded_opsx} commands in ${CLAUDE_CONFIG_DIR}/commands/opsx, ${seeded_skills} skills in ${CLAUDE_CONFIG_DIR}/skills)"
        else
            log "OpenSpec Claude artifacts already present, leaving untouched"
        fi
    else
        # Out-of-image runs only (the Dockerfile asserts the templates exist):
        # warn and continue — the harness still works without the commands.
        log "WARNING: Claude template dir ${CLAUDE_TEMPLATE_DIR} missing, skipping OpenSpec command seeding"
    fi
fi

# --- 2d. Seed OpenSpec global config ONLY if absent (both harnesses) ----------
# add-claude-openspec-commands: the CLI's default "core" profile generates
# only 6 of the 12 workflows — seeding the full custom profile makes an
# operator's own `openspec init`/`openspec update` inside the microVM emit
# the same complete artifact set as the image-baked templates (incl. ff/new/
# continue/verify/onboard/bulk-archive). telemetry.noticeSeen suppresses the
# first-run banner in scripted flows; telemetry itself stays at the CLI
# default (opt-out is the operator's call). Never overwrite an existing
# config: a deliberately chosen workflow subset is respected as-is.
if [ ! -f "${OPENSPEC_CONFIG_FILE}" ]; then
    mkdir -p "${OPENSPEC_CONFIG_DIR}"
    cat > "${OPENSPEC_CONFIG_FILE}" <<'EOF'
{
  "profile": "custom",
  "delivery": "both",
  "telemetry": { "noticeSeen": true },
  "workflows": ["propose", "explore", "new", "continue", "apply", "update", "ff", "sync", "archive", "bulk-archive", "verify", "onboard"]
}
EOF
    log "seeded OpenSpec global config (custom profile, 12 workflows) at ${OPENSPEC_CONFIG_FILE}"
else
    log "OpenSpec global config already present, leaving untouched"
fi

# --- 3. Repo bootstrap ---------------------------------------------------------
if [ -e "${REPO_DIR}/.git" ]; then
    log "repo already a git worktree, no-op"
elif [ -n "$(ls -A "${REPO_DIR}" 2>/dev/null)" ]; then
    # Non-empty but not a worktree: don't touch user data.
    log "repo dir non-empty but not a worktree, leaving as-is"
elif [ -n "${SCH_REPO_URL:-}" ]; then
    clone_url="${SCH_REPO_URL}"
    if [ -n "${SCH_REPO_TOKEN:-}" ]; then
        # Minimal token injection for HTTPS clones (design D9:
        # robust private-repo credentials are out of scope).
        clone_url="$(echo "${clone_url}" | sed -E "s#^https://#https://x-access-token:${SCH_REPO_TOKEN}@#")"
    fi
    log "cloning ${SCH_REPO_URL} into ${REPO_DIR}"
    if git clone "${clone_url}" "${REPO_DIR}"; then
        log "clone completed"
    else
        log "WARNING: clone failed (exit $?), falling back to empty git repo"
        git init "${REPO_DIR}" >/dev/null && log "initialized empty git repo"
    fi
else
    git init "${REPO_DIR}" >/dev/null
    log "initialized empty git repo in ${REPO_DIR}"
fi

# --- 4. Claude .mcp.json (project-scoped, repo root) ONLY if absent ----------
# sch-context7-builtin follow-up fix: MUST run after section 3 (repo is now
# guaranteed to exist as a worktree, either cloned/init'd above or already
# present from an L2 restore that ran before this script) — writing the file
# before section 3 would make the "is REPO_DIR empty" clone-vs-init check
# above see a non-empty dir and skip cloning SCH_REPO_URL entirely.
#
# Claude Code only reads project-scoped `.mcp.json` from the project root
# (cwd where `claude` is launched), never from `$CLAUDE_CONFIG_DIR/.mcp.json`
# (code.claude.com/docs/en/mcp: "Claude Code doesn't read paths such as
# ~/.claude/.mcp.json" — confirmed empirically: with the file at the old
# CLAUDE_CONFIG_DIR location, `claude mcp list` reported no servers
# configured at all, not even as pending approval). aws-docs (self-hosted)
# and aws-mcp (managed AWS MCP Server via the mcp-proxy-for-aws-cli SigV4
# proxy, TASK-25) enabled, aws-mcp read-only with no static credentials
# (inherits the credential_process -> IMDS chain via the env). context7
# (sch-context7-builtin) is a built-in entry but DISABLED by default,
# enforced via the official `disabledMcpjsonServers` list in `settings.json`
# (merged above, section 2) — NOT a per-server `"disabled": true` in
# `.mcp.json`, which is not part of Claude Code's schema.
#
# Idempotent: never overwrites an existing REPO_DIR/.mcp.json — this also
# means a cloned SCH_REPO_URL repo that already ships its own `.mcp.json`
# (committed, team-shared) is respected as-is and never touched.
if [ "${HARNESS}" = "claude" ]; then
    if [ ! -f "${CLAUDE_MCP_FILE}" ]; then
        cat > "${CLAUDE_MCP_FILE}" <<EOF
{
  "mcpServers": {
    "aws-docs": {
      "command": "awslabs.aws-documentation-mcp-server",
      "env": {
        "FASTMCP_LOG_LEVEL": "ERROR",
        "AWS_DOCUMENTATION_PARTITION": "aws"
      }
    },
    "aws-mcp": {
      "command": "mcp-proxy-for-aws-cli",
      "args": ["${AWS_MCP_ENDPOINT}", "--metadata", "AWS_REGION=${SEED_REGION}", "--read-only", "--log-level", "ERROR"],
      "env": {}
    },
    "context7": {
      "command": "context7-mcp",
      "env": {}
    }
  }
}
EOF
        log "seeded default Claude .mcp.json (aws-docs, aws-mcp read-only, context7 disabled-via-settings.json) at ${CLAUDE_MCP_FILE}"
    else
        log "Claude .mcp.json already present, leaving untouched"
    fi
    # Keep the machine-generated file out of the operator's `git status`
    # (it is not part of their actual project source and should not be
    # accidentally committed). Local-only exclude (.git/info/exclude),
    # never touches a tracked .gitignore. Idempotent append-if-missing.
    if [ -d "${REPO_DIR}/.git" ] || [ -f "${REPO_DIR}/.git" ]; then
        git_dir="$(git -C "${REPO_DIR}" rev-parse --git-dir 2>/dev/null)"
        if [ -n "${git_dir}" ]; then
            case "${git_dir}" in
                /*) : ;;
                *) git_dir="${REPO_DIR}/${git_dir}" ;;
            esac
            exclude_file="${git_dir}/info/exclude"
            mkdir -p "${git_dir}/info"
            if [ ! -f "${exclude_file}" ] || ! grep -qxF '/.mcp.json' "${exclude_file}" 2>/dev/null; then
                printf '%s\n' '/.mcp.json' >> "${exclude_file}"
                log "added /.mcp.json to ${exclude_file} (local-only, keeps git status clean)"
            fi
        fi
    fi
fi

log "workspace ready (root=${WORKSPACE_ROOT}, harness=${HARNESS})"
