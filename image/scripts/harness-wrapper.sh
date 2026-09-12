#!/bin/bash
# harness-wrapper.sh — single dispatcher for the opencode, claude and pi binaries.
#
# Generalized from opencode-wrapper.sh (sch-multi-harness, design D3): every
# launch path that would exec the real `opencode`, `claude` or `pi` binary
# instead execs this wrapper, which (1) applies the ENV bridge that `agentcore
# exec --it` login shells do NOT inherit, (2) gates on the per-harness readiness
# marker written by the shim once the workspace is seeded/restored, and (3)
# branches on the workspace's persisted harness so the right binary + harness-
# specific signals are applied. Installed at /usr/local/bin/opencode,
# /usr/local/bin/claude AND /usr/local/bin/pi at build time (see Dockerfile),
# each with its own SCH_HARNESS_REAL placeholder sed-substituted to the real
# binary path — so there is no path that exec's the real binary directly
# (design D3, "no path exec's the real binary directly").
#
# Harness selection precedence (first match wins):
#   1. $SCH_HARNESS env var — set by the shim's headless task subprocess
#      (_run_task builds the argv and sets SCH_HARNESS in the subprocess env).
#   2. mount marker's `harness` field — written by the shim from the payload
#      propagated by `bin/sch`. Covers interactive `agentcore exec --it`
#      shells (which do not inherit container ENV) on a known workspace.
#   3. invocation name ($0 basename) — `claude*` → claude, `pi` → pi, else
#      opencode. Covers raw shells without `sch` (no marker, no env): the
#      operator's typed binary name is honored (spec: harness-selection,
#      "Assenza di harness nel payload non seleziona il default" — the
#      multi-harness contract is opt-in of the `sch` wrapper; raw shells fall
#      back to the binary name).
#
# SCH_HARNESS_REAL is baked in at image build time (one wrapper per binary).
#
# Belt-and-braces (carried over from opencode-wrapper.sh v8): `agentcore exec
# --it` spawns a LOGIN shell (`bash --login`) that does NOT inherit the
# container's Docker ENV — only a minimal default set (HOME, PATH, SHELL,
# TERM, USER). Without XDG_CONFIG_HOME/XDG_DATA_HOME, OpenCode falls back to
# $HOME/.config and $HOME/.opencode — never seeing the seeded provider/mcp
# config, and OPENCODE_DB drifts from the path the shim actually backs
# up/restores. Claude Code without CLAUDE_CONFIG_DIR drifts from the local-
# disk root the L2 loop mirrors to /mnt/workspace/state/claude. /etc/profile.d/
# sch-env.sh covers AWS_PROFILE/AWS_REGION/CLAUDE_* for the shell itself;
# exporting the canonical paths here as well guarantees both harnesses always
# get them right, regardless of which shell (or lack thereof) invoked this
# wrapper.
set -u

export HOME="${HOME:-/home/sch}"
ACTIVE_WORKSPACE_FILE="/home/sch/.sch-workspace.json"
if [ -f "${ACTIVE_WORKSPACE_FILE}" ]; then
    ACTIVE_WORKSPACE_ROOT="$(python3 -c 'import json,sys
try:
    print(json.load(open(sys.argv[1])).get("root") or "")
except Exception:
    print("")' "${ACTIVE_WORKSPACE_FILE}" 2>/dev/null || echo "")"
fi
export SCH_WORKSPACE_ROOT="${ACTIVE_WORKSPACE_ROOT:-${SCH_WORKSPACE_ROOT:-/mnt/workspace}}"
export XDG_DATA_HOME="${SCH_WORKSPACE_ROOT}/state/data"
export XDG_CONFIG_HOME="${SCH_WORKSPACE_ROOT}/state/config"
export AWS_PROFILE="${AWS_PROFILE:-default}"
export AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-eu-west-1}}"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-${AWS_REGION}}"
# Login-shell launches are interactive unless the shim explicitly classifies a
# task subprocess as headless. Keep hook IPC on fixed ephemeral paths; none of
# these values grants access to Telegram.
SCH_EXECUTION_MODE="${SCH_EXECUTION_MODE:-interactive}"
case "${SCH_EXECUTION_MODE}" in
    interactive|headless) ;;
    *) SCH_EXECUTION_MODE="interactive" ;;
esac
export SCH_EXECUTION_MODE
export SCH_TELEGRAM_SPOOL_DIR="/tmp/sch-telegram-spool"
export SCH_TELEGRAM_ENABLED_MARKER="/tmp/sch-telegram-enabled"
export SCH_COMMAND_SHELL_PRESENCE_FILE="/tmp/sch-command-shell-presence.json"
# add-dev-env-autonomy: the harness is told to bootstrap project environments
# itself, so the uv Python policy must hold for every launch path — including
# the login shells that do not inherit the container ENV. Never a managed
# CPython (it would land on the checkpointed mount) and never AL2023's default
# python3 (3.9); project venvs use the system python3.11.
export UV_PYTHON="${UV_PYTHON:-python3.11}"
export UV_PYTHON_DOWNLOADS="${UV_PYTHON_DOWNLOADS:-never}"

# --- Resolve harness -----------------------------------------------------------
# Precedence: $SCH_HARNESS (shim headless subprocess) > mount marker > $0.
HARNESS="${SCH_HARNESS:-}"
if [ -z "${HARNESS}" ]; then
    MARKER_PATH="${SCH_WORKSPACE_ROOT}/state/.sch-initialized"
    if [ -f "${MARKER_PATH}" ]; then
        # Best-effort JSON field extraction (python3 is in the image). Swallow
        # any failure → fall through to $0-based detection.
        HARNESS="$(python3 -c 'import json,sys
try:
    print((json.load(open(sys.argv[1])).get("harness") or ""))
except Exception:
    print("")' "${MARKER_PATH}" 2>/dev/null || echo "")"
    fi
fi
if [ -z "${HARNESS}" ]; then
    case "$(basename "$0")" in
        claude*) HARNESS="claude" ;;
        pi)      HARNESS="pi" ;;
        *)       HARNESS="opencode" ;;
    esac
fi
case "${HARNESS}" in
    opencode|claude|pi) ;;
    *)
        echo "sch: unknown harness '${HARNESS}' (expected 'opencode', 'claude' or 'pi')" >&2
        exit 2
        ;;
esac

# --- Harness/binary binding enforcement ----------------------------------------
# The workspace's harness binding restricts which harness BINARY is available:
# invoking the binary of the OTHER harness on a bound workspace is refused with
# an explicit error (previously it soft-gated: waited 220s on a readiness
# marker that never appears, then launched anyway). The invoked identity comes
# from $0 (each wrapper copy shadows exactly one binary name). Raw shells
# without `sch` (no marker, no env) resolve HARNESS from $0 too, so they always
# match. Escape hatch: SCH_HARNESS explicitly exported by the operator (or by
# the shim's headless task subprocess) takes precedence rule 1 above, and a
# matching SCH_HARNESS+binary pair is honored as-is.
INVOKED_BIN="$(basename "$0")"
case "${INVOKED_BIN}" in
    claude*) INVOKED_HARNESS="claude" ;;
    pi)      INVOKED_HARNESS="pi" ;;
    *)       INVOKED_HARNESS="opencode" ;;
esac
if [ "${INVOKED_HARNESS}" != "${HARNESS}" ]; then
    echo "sch: '${INVOKED_BIN}' is not available in this workspace (bound to harness='${HARNESS}')." >&2
    echo "sch: run '${HARNESS}' instead, or create a new workspace with: sch shell <new-ws> --harness ${INVOKED_HARNESS}" >&2
    exit 127
fi

REAL="${SCH_HARNESS_REAL:?SCH_HARNESS_REAL not set}"

# --- Provider API keys: SCH_* -> canonical names (add-provider-api-keys) -------
# The keys are a per-USER, per-session property (add-user-provider-keys): `sch`
# reads them from the caller's ~/.sch/env and the shim stages them in a 0600
# file under /run/sch;
# they reach the microVM under the inert SCH_ prefix (design D1) and this
# dispatcher is the single decision point that maps them onto the canonical
# names each harness auto-detects, per harness. Never logged: only variable
# NAMES may ever appear in output (spec: provider-api-keys, "Key secrecy").
#
#   export_provider_key <CANONICAL_NAME> <SCH_NAME>
#
# Exports only when the source is present AND non-empty — an absent key must
# leave the canonical variable absent too (not empty), so a harness never sees
# an empty credential and fails in a confusing way. An explicit canonical value
# already in the environment (operator override) wins.
export_provider_key() {
    local canonical="$1" source_var="$2" source_val
    source_val="${!source_var:-}"
    [ -n "${source_val}" ] || return 0
    if [ -n "${!canonical:-}" ]; then
        return 0
    fi
    export "${canonical}=${source_val}"
}

# add-user-provider-keys (design D3, task 3.1): the session's key set comes from
# the staging file the shim writes (mode 0600, /run/sch is not on the checkpointed
# mount) — NOT from the container environment, which no longer carries any
# provider secret, and no longer from /proc/1/environ (the previous recovery
# path: it exposed the deployment's shared keys to every session, including raw
# shells, and is exactly the blast radius this capability removes).
#
# This is also what makes the keys reachable from `agentcore exec --it` login
# shells, which inherit only a minimal environment. Reading is deliberately NOT
# `source`/`eval`: only the allowlisted names are exported, and the value is
# taken verbatim after the first `=` — so no file content can ever be executed.
# A shell that never went through `sch` finds no file and stays Bedrock-only.
PROVIDER_KEYS_FILE="${SCH_PROVIDER_KEYS_FILE:-/run/sch/provider-keys.env}"
load_provider_keys_from_staging() {
    local line name value
    [ -r "${PROVIDER_KEYS_FILE}" ] || return 0
    while IFS= read -r line || [ -n "${line}" ]; do
        case "${line}" in
            SCH_ANTHROPIC_API_KEY=*|SCH_OPENCODE_API_KEY=*|SCH_OPENROUTER_API_KEY=*|SCH_KILO_API_KEY=*|SCH_BEDROCK_API_KEY=*|SCH_GITHUB_TOKEN=*) ;;
            *) continue ;;
        esac
        name="${line%%=*}"
        value="${line#*=}"
        [ -n "${value}" ] || continue
        # An inherited value wins: processes spawned BY the shim already carry
        # the set they were started with, and keeping it is the documented
        # "a running process keeps its keys until restart" contract (design D4).
        [ -n "${!name:-}" ] && continue
        export "${name}=${value}"
    done < "${PROVIDER_KEYS_FILE}"
}

load_provider_keys_from_staging

# Is the current value of $1 a Bedrock inference profile / model ARN? Used by
# the claude branch to neutralize ONLY the image-baked Bedrock model defaults
# (invalid on the Anthropic API) while preserving a deliberate operator
# override with a name the Anthropic API accepts (design D3).
is_bedrock_model_ref() {
    case "${1:-}" in
        arn:*) return 0 ;;
        eu.anthropic.*|us.anthropic.*|global.anthropic.*|apac.anthropic.*) return 0 ;;
        *) return 1 ;;
    esac
}

# --- Per-harness ENV bridge + readiness marker --------------------------------
# Shared wait-loop parameter (design D3, task 2.3): SCH_OPENCODE_WAIT stays
# the OpenCode-specific knob for backward compat; SCH_HARNESS_WAIT is the
# generalized knob reused for the claude and pi branches. Both default to 220s, which
# comfortably exceeds the shim's worst-case RESUME_WAIT (180s) plus
# db-restore / init-workspace / seed-verify overhead (carried over from
# opencode-wrapper.sh v11: the prior 90s default caused the wrapper to give
# up and launch OpenCode against a not-yet-seeded workspace, the final root
# cause of "0 MCP tools" reports that survived v8/v10).
if [ "${HARNESS}" = "pi" ]; then
    # --- add-pi-harness (design D2/D9/D10) ------------------------------------
    # PI_CODING_AGENT_DIR points at the LOCAL-disk root: the hot live path the
    # `pi` binary writes JSONL sessions, settings, trust and extensions to is
    # $HOME/.pi/agent, mirrored to /mnt/workspace/state/pi by the L2 checkpoint
    # loop (same pattern as CLAUDE_CONFIG_DIR — the mount has no fcntl support
    # and asynchronous restore hazards rule out pointing Pi at it directly).
    export PI_CODING_AGENT_DIR="${PI_CODING_AGENT_DIR:-/home/sch/.pi/agent}"
    # Telemetry off and no pi.dev latest-version request: this microVM must never
    # make an unsolicited outbound call at harness startup. `--offline` is NOT
    # used — it would also disable package/model-catalog refreshes the operator
    # may legitimately want.
    export PI_TELEMETRY="${PI_TELEMETRY:-0}"
    export PI_SKIP_VERSION_CHECK="${PI_SKIP_VERSION_CHECK:-1}"
    # No provider block, no API key: Pi's native `amazon-bedrock` provider rides
    # the execution role through the shared AWS env exported at the top of this
    # script (AWS_PROFILE=default -> ~/.aws/config credential_process -> IMDSv2).
    # Verified empirically in a live SCH microVM (design.md OQ-PI-BEDROCK).
    #
    # add-pi-harness (design D9): provider keys are ADDITIVE on pi. Pi keeps
    # several providers side by side (like opencode, unlike claude), so a key
    # merely makes its provider selectable — the seeded Bedrock default provider
    # and model are untouched.
    #
    # extend-pi-gateway-keys (TASK-18): the gateway keys are wired too. Pi has
    # no built-in provider consuming them, so init-workspace.sh merges the
    # SCH-owned gateway blocks (generated from models.dev at image build) into
    # models.json when the matching key is staged this session; the
    # interpolation happens inside pi ("$ENV_VAR" references), the value itself
    # never touches the models.json file.
    #
    # TASK-19 (cross-account Bedrock): the Bedrock API key re-auths the SAME
    # seeded amazon-bedrock provider (bearer token over the execution role;
    # pi resolves AWS_BEARER_TOKEN_BEDROCK before the container credentials).
    # Additive like every other key: no switchover, model defaults untouched.
    export_provider_key ANTHROPIC_API_KEY SCH_ANTHROPIC_API_KEY
    export_provider_key OPENROUTER_API_KEY SCH_OPENROUTER_API_KEY
    export_provider_key OPENCODE_API_KEY SCH_OPENCODE_API_KEY
    export_provider_key KILO_API_KEY SCH_KILO_API_KEY
    export_provider_key AWS_BEARER_TOKEN_BEDROCK SCH_BEDROCK_API_KEY
    # TASK-26 (opt-in GitHub access): not a model provider — `gh` and git
    # need it on every harness. Additive, no switchover: `gh` reads GH_TOKEN
    # first, then GITHUB_TOKEN, so both names are exported with the same
    # value (never `gh auth login`: its hosts file would land on the
    # checkpointed mount and persist the secret).
    export_provider_key GH_TOKEN SCH_GITHUB_TOKEN
    export_provider_key GITHUB_TOKEN SCH_GITHUB_TOKEN
    READY_MARKER="${PI_CODING_AGENT_DIR}/.ready"
    WAIT_SECS="${SCH_HARNESS_WAIT:-${SCH_OPENCODE_WAIT:-220}}"
elif [ "${HARNESS}" = "opencode" ]; then
    export OPENCODE_DB="${OPENCODE_DB:-/home/sch/.opencode/opencode.db}"
    # Enables OpenCode's built-in `websearch`/`codesearch` tools (Exa-backed,
    # no separate API key needed — requests go through OpenCode's own backend).
    # Off by default unless using the hosted "opencode" provider; needed
    # explicitly here since this image uses amazon-bedrock as the default.
    export OPENCODE_ENABLE_EXA="${OPENCODE_ENABLE_EXA:-1}"
    # add-provider-api-keys (task 2.1): ALL staged keys are offered to opencode.
    # It auto-detects them through the models.dev provider catalog, so the
    # matching providers (`anthropic`, `opencode` + `opencode-go` — one shared
    # key —, `openrouter`, `kilo`) become selectable in /models with no
    # `opencode auth login` and no auth.json on the checkpointed mount
    # (design D2). The seeded default model (amazon-bedrock/...) is untouched:
    # keys ADD providers, they do not change the current selection.
    #
    # TASK-19: AWS_BEARER_TOKEN_BEDROCK is the one canonical name that does not
    # ADD a provider — it re-auths the amazon-bedrock provider itself (bearer
    # token has the highest precedence in opencode's Bedrock plugin, above the
    # container credentials of the execution role).
    export_provider_key ANTHROPIC_API_KEY SCH_ANTHROPIC_API_KEY
    export_provider_key OPENCODE_API_KEY SCH_OPENCODE_API_KEY
    export_provider_key OPENROUTER_API_KEY SCH_OPENROUTER_API_KEY
    export_provider_key KILO_API_KEY SCH_KILO_API_KEY
    export_provider_key AWS_BEARER_TOKEN_BEDROCK SCH_BEDROCK_API_KEY
    # TASK-26 (opt-in GitHub access): same additive export as the pi branch —
    # `gh`/git need the token on every harness, no switchover involved.
    export_provider_key GH_TOKEN SCH_GITHUB_TOKEN
    export_provider_key GITHUB_TOKEN SCH_GITHUB_TOKEN
    READY_MARKER="/home/sch/.opencode/.ready"
    WAIT_SECS="${SCH_HARNESS_WAIT:-${SCH_OPENCODE_WAIT:-220}}"
else  # claude
    # CLAUDE_CONFIG_DIR points at the LOCAL-disk root (design D2): the hot
    # live path the `claude` binary writes JSONL transcripts to is
    # $HOME/.claude, on local disk, mirrored to /mnt/workspace/state/claude by
    # the L2 checkpoint loop. CLAUDE_CODE_USE_BEDROCK=1 + the inference
    # profile ARN defaults ride the execution role via IMDSv2 (no OAuth token,
    # sidesteps anthropics/claude-code#28827).
    export CLAUDE_CONFIG_DIR="${CLAUDE_CONFIG_DIR:-/home/sch/.claude}"
    export CLAUDE_CODE_USE_BEDROCK="${CLAUDE_CODE_USE_BEDROCK:-1}"
    # Model aliases (fable/opus/sonnet/haiku) all pinned to Bedrock inference
    # profiles so every Anthropic model slot in Claude Code's /model picker
    # resolves through an inference profile available in-region; _NAME vars
    # give the picker friendly labels. Opus 5 (Global) rides the single
    # ANTHROPIC_CUSTOM_MODEL_OPTION slot (one alias slot per family). Any
    # other profile ID can still be selected with `/model <profile-id>` — on
    # Bedrock, Claude Code passes the string through unchecked.
    export ANTHROPIC_DEFAULT_FABLE_MODEL="${ANTHROPIC_DEFAULT_FABLE_MODEL:-global.anthropic.claude-fable-5}"
    export ANTHROPIC_DEFAULT_FABLE_MODEL_NAME="${ANTHROPIC_DEFAULT_FABLE_MODEL_NAME:-Fable 5 (Global)}"
    export ANTHROPIC_DEFAULT_OPUS_MODEL="${ANTHROPIC_DEFAULT_OPUS_MODEL:-eu.anthropic.claude-opus-5}"
    export ANTHROPIC_DEFAULT_OPUS_MODEL_NAME="${ANTHROPIC_DEFAULT_OPUS_MODEL_NAME:-Opus 5 (EU)}"
    export ANTHROPIC_DEFAULT_SONNET_MODEL="${ANTHROPIC_DEFAULT_SONNET_MODEL:-global.anthropic.claude-sonnet-5}"
    export ANTHROPIC_DEFAULT_SONNET_MODEL_NAME="${ANTHROPIC_DEFAULT_SONNET_MODEL_NAME:-Sonnet 5 (Global)}"
    export ANTHROPIC_DEFAULT_HAIKU_MODEL="${ANTHROPIC_DEFAULT_HAIKU_MODEL:-eu.anthropic.claude-haiku-4-5-20251001-v1:0}"
    export ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME="${ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME:-Haiku 4.5 (EU)}"
    export ANTHROPIC_CUSTOM_MODEL_OPTION="${ANTHROPIC_CUSTOM_MODEL_OPTION:-global.anthropic.claude-opus-5}"
    export ANTHROPIC_CUSTOM_MODEL_OPTION_NAME="${ANTHROPIC_CUSTOM_MODEL_OPTION_NAME:-Opus 5 (Global)}"
    export ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION="${ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION:-Opus 5 via the global cross-region inference profile}"
    # add-provider-api-keys (tasks 2.2/2.3, design D3): the PRESENCE of an
    # Anthropic key expresses the intent to use the provider's own API, so this
    # branch switches Claude Code off Bedrock for the whole deployment. The
    # image ENV / profile.d already set CLAUDE_CODE_USE_BEDROCK=1 and the
    # Bedrock model defaults above, so the switch must OVERRIDE existing values
    # actively — a `:-` fallback would be a no-op here.
    #
    # Only the Anthropic key is wired in as a provider switch: OPENCODE_API_KEY
    # / OPENROUTER_API_KEY / KILO_API_KEY are deliberately NOT exported to
    # claude and ANTHROPIC_BASE_URL is never touched (spec: provider-api-keys,
    # "Chiavi gateway non cablate in Claude Code"). Without the key this whole
    # block is skipped and the branch behaves exactly as before this capability.
    #
    # TASK-19 (cross-account Bedrock): a staged Bedrock API key (bearer token)
    # does NOT switch claude off Bedrock — it re-auths Bedrock itself against
    # the key's account (AWS_BEARER_TOKEN_BEDROCK; Claude Code consumes it with
    # CLAUDE_CODE_USE_BEDROCK=1 untouched, and the Bedrock model defaults stay
    # valid because they resolve in the key's account). Precedence when both
    # keys are staged: the Anthropic key wins and the bearer token is NOT
    # exported — claude rides the Anthropic API and no unused secret is handed
    # to the process.
    if [ -n "${SCH_ANTHROPIC_API_KEY:-}" ]; then
        export ANTHROPIC_API_KEY="${SCH_ANTHROPIC_API_KEY}"
        # Claude Code reads this with a truthy check (1/true/yes/on), so "0"
        # disables Bedrock — verified against the pinned CLAUDE_CODE_VERSION.
        export CLAUDE_CODE_USE_BEDROCK=0
        # The alias/custom-option defaults point at Bedrock inference profiles,
        # which the Anthropic API rejects. Drop ONLY those; a deliberate
        # operator override naming an Anthropic API model survives untouched.
        # The _NAME/_DESCRIPTION labels are meaningless once their model slot is
        # gone, so they are dropped together with the slot they label (and only
        # then — hence the iteration is over SLOTS, not over variables).
        for _model_slot in \
            ANTHROPIC_DEFAULT_FABLE_MODEL \
            ANTHROPIC_DEFAULT_OPUS_MODEL \
            ANTHROPIC_DEFAULT_SONNET_MODEL \
            ANTHROPIC_DEFAULT_HAIKU_MODEL \
            ANTHROPIC_CUSTOM_MODEL_OPTION; do
            if is_bedrock_model_ref "${!_model_slot:-}"; then
                unset "${_model_slot}" "${_model_slot}_NAME" "${_model_slot}_DESCRIPTION"
            fi
        done
        unset _model_slot
    elif [ -n "${SCH_BEDROCK_API_KEY:-}" ]; then
        export_provider_key AWS_BEARER_TOKEN_BEDROCK SCH_BEDROCK_API_KEY
    fi
    # TASK-26 (opt-in GitHub access): outside the Bedrock/Anthropic switch —
    # the token is harness-independent and must reach claude sessions too.
    export_provider_key GH_TOKEN SCH_GITHUB_TOKEN
    export_provider_key GITHUB_TOKEN SCH_GITHUB_TOKEN
    READY_MARKER="/home/sch/.claude/.ready"
    WAIT_SECS="${SCH_HARNESS_WAIT:-${SCH_OPENCODE_WAIT:-220}}"
fi

if [ ! -f "${READY_MARKER}" ]; then
    echo "sch: workspace initializing (harness=${HARNESS}, waiting up to ${WAIT_SECS}s)..." >&2
    i=0
    while [ "${i}" -lt "${WAIT_SECS}" ] && [ ! -f "${READY_MARKER}" ]; do
        sleep 1
        i=$((i + 1))
    done
    if [ ! -f "${READY_MARKER}" ]; then
        echo "sch: WARNING — workspace not ready after ${WAIT_SECS}s (harness=${HARNESS}; check shim logs);" >&2
        echo "sch: starting ${HARNESS} anyway. Session history may be incomplete." >&2
    fi
fi

# The real harness and all descendants receive only local IPC paths. This also
# strips secrets inherited by a non-login/headless launch.
unset SCH_TELEGRAM_BOT_TOKEN SCH_TELEGRAM_CHAT_ID SCH_TELEGRAM_COMMANDS_TABLE
unset SCH_TELEGRAM_ROUTING_TABLE
unset SCH_TELEGRAM_NOTIFICATIONS_CONFIGURED

if [ "${HARNESS}" = "pi" ] && [ "$#" -eq 0 ]; then
    PI_INTERACTIVE_ROLE="${PI_CODING_AGENT_DIR}/roles/remote-interactive.md"
    if [ -f "${PI_INTERACTIVE_ROLE}" ]; then
        exec "${REAL}" --append-system-prompt "${PI_INTERACTIVE_ROLE}"
    fi
fi
exec "${REAL}" "$@"
