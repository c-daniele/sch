#!/bin/bash
# sch-run-profile.sh — run-once harness autostart for `sch run` (installed as
# /etc/profile.d/sch-run.sh, sourced AFTER sch-env.sh: 'e' < 'r').
#
# `agentcore exec --it` opens a PTY login shell and cannot pass a startup
# command (the CLI's [command...] argument is one-shot/non-interactive only).
# `sch run <ws>` therefore asks the shim (action=prepare-run) to write a
# RUN-ONCE marker on local disk; the first interactive login shell that finds
# it consumes it (delete-before-use), creates+enters the active worktree and
# exec's the workspace's harness binary. Because it is an `exec`, quitting the
# harness terminates the login shell, which closes the remote shell session
# and returns control to the operator's terminal (no lingering shell).
#
# `sch run --continue` arms the same marker with a `session_id` resolved by
# the shim (the harness's latest session, same resolvers as `task --continue`);
# the autostart then resumes that session explicitly
# (`opencode --session`, `claude --resume`, `pi --session`). A marker without
# a session (none found, or an older shim that never resolved one) starts a
# fresh TUI — resume degrades, never fails.
#
# Safety properties:
#   - consume-once: the marker is deleted BEFORE the exec, so a concurrent or
#     subsequent shell (e.g. a plain `sch shell`) starts normally;
#   - TTL (SCH_RUN_TTL, default 300s): a stale marker from an aborted
#     `sch run` (e.g. the operator's connection never arrived) is discarded;
#   - interactive-only: [ -t 0 ] skips one-shot `agentcore exec <cmd>`
#     invocations and any shell without a PTY;
#   - the exec goes through the harness dispatcher (/usr/local/bin/<harness>
#     -> harness-wrapper.sh), so the ENV bridge, readiness gating and
#     harness/binary binding enforcement all apply unchanged.
#
# NOTE: this file is SOURCED by /etc/profile — no set -e/-u, no exit; every
# step is best-effort and falls back to a normal shell on any failure.

_SCH_RUN_MARKER="/home/sch/.sch-run-once.json"
if [ -f "${_SCH_RUN_MARKER}" ] && [ -t 0 ] && [ -z "${SCH_RUN_AUTOSTARTED:-}" ]; then
    _SCH_RUN_PARSED="$(python3 -c 'import json, re, sys, time
try:
    d = json.load(open(sys.argv[1]))
    h = (d.get("harness") or "").strip().lower()
    m = d.get("model") or ""
    s = d.get("session_id") or ""
    ts = float(d.get("epoch") or 0)
    ttl = float(sys.argv[2])
    # Model/session travel as discrete argv elements: refuse anything with
    # whitespace or control characters (the shim already validates the model;
    # the session may be an id or, for pi, an absolute session-file path).
    safe = re.compile(r"\A[\x21-\x7e]+\Z")
    if not isinstance(m, str) or not safe.match(m):
        m = ""
    if not isinstance(s, str) or not safe.match(s):
        s = ""
    print(h + "\t" + m + "\t" + s if h in ("opencode", "claude", "pi") and (time.time() - ts) <= ttl else "\t\t")
except Exception:
    print("\t\t")' "${_SCH_RUN_MARKER}" "${SCH_RUN_TTL:-300}" 2>/dev/null || echo $'\t\t')"
    rm -f "${_SCH_RUN_MARKER}" 2>/dev/null
    _SCH_RUN_HARNESS="${_SCH_RUN_PARSED%%$'\t'*}"
    _SCH_RUN_REST="${_SCH_RUN_PARSED#*$'\t'}"
    _SCH_RUN_MODEL="${_SCH_RUN_REST%%$'\t'*}"
    _SCH_RUN_SESSION="${_SCH_RUN_REST#*$'\t'}"
    if [ "${_SCH_RUN_HARNESS}" = "opencode" ] || [ "${_SCH_RUN_HARNESS}" = "claude" ] \
        || [ "${_SCH_RUN_HARNESS}" = "pi" ]; then
        export SCH_RUN_AUTOSTARTED=1
        _SCH_RUN_ROOT="$(python3 -c 'import json
try:
    print(json.load(open("/home/sch/.sch-workspace.json")).get("root") or "/mnt/workspace")
except Exception:
    print("/mnt/workspace")' 2>/dev/null || echo "/mnt/workspace")"
        mkdir -p "${_SCH_RUN_ROOT}/repo" 2>/dev/null
        if cd "${_SCH_RUN_ROOT}/repo" 2>/dev/null; then
            echo "sch: autostarting ${_SCH_RUN_HARNESS} in ${_SCH_RUN_ROOT}/repo (quitting it closes this session)" >&2
            if [ "${_SCH_RUN_HARNESS}" = "pi" ]; then
                # add-pi-harness: Pi selects a model as a provider/model PAIR
                # (spec: run-model-selection), and has no agent files — the
                # remote-interactive role contract is a system prompt appended
                # from the seeded role file (design D4). Every value is a
                # discrete argv element; nothing is shell-interpolated. A
                # missing role file degrades to Pi's default prompt rather than
                # failing the autostart.
                # A bash array, NOT `set --`: this file is SOURCED by the login
                # shell, and `set --` would clobber its positional parameters
                # (visible if the exec below ever fails and we fall through).
                _SCH_RUN_PI_ROLE="${PI_CODING_AGENT_DIR:-/home/sch/.pi/agent}/roles/remote-interactive.md"
                _SCH_RUN_PI_ARGS=()
                if [ -n "${_SCH_RUN_SESSION}" ]; then
                    _SCH_RUN_PI_ARGS+=(--session "${_SCH_RUN_SESSION}")
                fi
                if [ -n "${_SCH_RUN_MODEL}" ]; then
                    _SCH_RUN_PI_ARGS+=(--provider "${SCH_PI_PROVIDER:-amazon-bedrock}" --model "${_SCH_RUN_MODEL}")
                fi
                if [ -f "${_SCH_RUN_PI_ROLE}" ]; then
                    _SCH_RUN_PI_ARGS+=(--append-system-prompt "${_SCH_RUN_PI_ROLE}")
                fi
                unset _SCH_RUN_PI_ROLE
                exec pi "${_SCH_RUN_PI_ARGS[@]}"
            elif [ "${_SCH_RUN_HARNESS}" = "opencode" ] && [ -n "${_SCH_RUN_SESSION}" ]; then
                if [ -n "${_SCH_RUN_MODEL}" ]; then
                    exec opencode --session "${_SCH_RUN_SESSION}" --model "${_SCH_RUN_MODEL}"
                else
                    exec opencode --session "${_SCH_RUN_SESSION}"
                fi
            elif [ "${_SCH_RUN_HARNESS}" = "claude" ] && [ -n "${_SCH_RUN_SESSION}" ]; then
                if [ -n "${_SCH_RUN_MODEL}" ]; then
                    exec claude --resume "${_SCH_RUN_SESSION}" --model "${_SCH_RUN_MODEL}"
                else
                    exec claude --resume "${_SCH_RUN_SESSION}"
                fi
            elif [ -n "${_SCH_RUN_MODEL}" ]; then
                exec "${_SCH_RUN_HARNESS}" --model "${_SCH_RUN_MODEL}"
            else
                exec "${_SCH_RUN_HARNESS}"
            fi
        else
            echo "sch: WARNING — cannot cd into ${_SCH_RUN_ROOT}/repo; dropping to a plain shell" >&2
        fi
        unset _SCH_RUN_ROOT
    fi
    unset _SCH_RUN_PARSED
    unset _SCH_RUN_HARNESS
    unset _SCH_RUN_MODEL
    unset _SCH_RUN_SESSION
    unset _SCH_RUN_REST
fi
unset _SCH_RUN_MARKER
