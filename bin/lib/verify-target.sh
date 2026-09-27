#!/bin/bash
# bin/lib/verify-target.sh — shared target resolution for bin/verify-*.sh
# (spec: per-principal-isolation R40; TASK-20.5). Sourced, never executed.
#
# The live verification scripts address a workspace the way `sch` does on
# every deployment mode:
#   - registry off  : nothing changes; the scripts keep their local-index path.
#   - registry on   : session ID, storage, epoch and the workspace identity
#                     (the `workspace` field of invoke payloads) come from the
#                     registry.
#   - isolation on  : the runtime is the caller's plane runtime (never the
#                     shared one), and owner checkpoint objects are read
#                     through the plane access role.
#
# Functions (all prefixed sch_target_):
#   sch_target_init                 probe once; dies with a clear message when
#                                   the stack is isolated but the registry is
#                                   not configured, or the registry refuses
#                                   the caller (unlisted principal, HTTP 403)
#   sch_target_registry / _isolated exit status 0 when on
#   sch_target_runtime_arn          plane runtime ARN (isolation), else ""
#   sch_target_workspace <ws> [harness] [storage]
#                                   registry on: resolve (create when new) and
#                                   set SCH_TARGET_SID, SCH_TARGET_STORAGE,
#                                   SCH_TARGET_EPOCH, SCH_TARGET_HARNESS,
#                                   SCH_TARGET_WS (payload workspace),
#                                   SCH_TARGET_CKPT_PREFIX, SCH_TARGET_ARN;
#                                   registry off: returns 1, sets nothing
#   sch_target_ckpt_prefix <runtime-ws>
#                                   checkpoints/[<ownerPrefix>/]<ws>/
#   sch_target_owner_aws <aws args...>
#                                   `aws` for owner checkpoint reads: through
#                                   the access role with isolation on
#   sch_target_skip_if_isolated <reason>
#                                   prints SKIP and exits 0 when the runtime
#                                   stack reports IsolationStatus=true
#
# Call sch_target_init once at the top level of the script (not inside a
# command substitution): the probe result lives in shell variables, and a
# subshell would probe the registry again.

SCH_TARGET_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCH_TARGET_SUPPORT="${SCH_TARGET_LIB_DIR}/../../cli/sch/verify_support.py"
SCH_TARGET_PROBED="${SCH_TARGET_PROBED:-}"
SCH_TARGET_REGISTRY_ON="${SCH_TARGET_REGISTRY_ON:-false}"
SCH_TARGET_ISOLATION_ON="${SCH_TARGET_ISOLATION_ON:-false}"
SCH_TARGET_PLANE_ARN="${SCH_TARGET_PLANE_ARN:-}"
SCH_TARGET_OWNER_PREFIX="${SCH_TARGET_OWNER_PREFIX:-}"
SCH_TARGET_ACCESS_ROLE="${SCH_TARGET_ACCESS_ROLE:-}"

sch_target_support() { # <subcommand> [args...]
    python3 "${SCH_TARGET_SUPPORT}" "$@"
}

sch_target_json_field() { # <json> <field> -> value ("" for null/missing)
    printf '%s' "$1" | python3 -c '
import json, sys
value = json.load(sys.stdin)
for part in sys.argv[1].split("."):
    value = value.get(part) if isinstance(value, dict) else None
if value is None:
    value = ""
elif isinstance(value, bool):
    value = "true" if value else "false"
print(value)
' "$2"
}

sch_target_init() {
    [ -n "${SCH_TARGET_PROBED}" ] && return 0
    local out rc=0
    out="$(sch_target_support probe)" || rc=$?
    if [ "${rc}" -ne 0 ]; then
        echo "verify: cannot resolve the deployment target (verify_support probe exit ${rc}); see the message above" >&2
        exit 1
    fi
    SCH_TARGET_REGISTRY_ON="$(sch_target_json_field "${out}" registry)"
    SCH_TARGET_ISOLATION_ON="$(sch_target_json_field "${out}" isolation)"
    SCH_TARGET_PLANE_ARN="$(sch_target_json_field "${out}" plane.runtimeArn)"
    SCH_TARGET_OWNER_PREFIX="$(sch_target_json_field "${out}" plane.ownerPrefix)"
    SCH_TARGET_ACCESS_ROLE="$(sch_target_json_field "${out}" plane.accessRoleArn)"
    SCH_TARGET_PROBED=1
    if [ "${SCH_TARGET_ISOLATION_ON}" = "true" ]; then
        echo "note: isolation on — using this caller's plane runtime (owner prefix ${SCH_TARGET_OWNER_PREFIX})" >&2
    fi
}

sch_target_registry() { sch_target_init; [ "${SCH_TARGET_REGISTRY_ON}" = "true" ]; }
sch_target_isolated() { sch_target_init; [ "${SCH_TARGET_ISOLATION_ON}" = "true" ]; }

sch_target_runtime_arn() {
    sch_target_init
    if [ "${SCH_TARGET_ISOLATION_ON}" = "true" ]; then
        printf '%s\n' "${SCH_TARGET_PLANE_ARN}"
    fi
}

sch_target_workspace() { # <ws> [harness] [storage]
    sch_target_registry || return 1
    local out
    out="$(sch_target_support workspace "$1" ${2:+--harness "$2"} ${3:+--storage "$3"})" || {
        echo "verify: cannot resolve workspace '$1' through the registry" >&2
        exit 1
    }
    SCH_TARGET_SID="$(sch_target_json_field "${out}" sessionId)"
    SCH_TARGET_STORAGE="$(sch_target_json_field "${out}" storage)"
    SCH_TARGET_EPOCH="$(sch_target_json_field "${out}" sessionEpoch)"
    SCH_TARGET_HARNESS="$(sch_target_json_field "${out}" harness)"
    SCH_TARGET_WS="$(sch_target_json_field "${out}" runtimeWorkspace)"
    SCH_TARGET_CKPT_PREFIX="$(sch_target_json_field "${out}" checkpointPrefix)"
    SCH_TARGET_ARN="$(sch_target_json_field "${out}" runtimeArn)"
    return 0
}

sch_target_ckpt_prefix() { # <runtime-ws>
    sch_target_init
    if [ "${SCH_TARGET_ISOLATION_ON}" = "true" ]; then
        printf 'checkpoints/%s/%s/\n' "${SCH_TARGET_OWNER_PREFIX}" "$1"
    else
        printf 'checkpoints/%s/\n' "$1"
    fi
}

sch_target_owner_aws() { # <aws args...>
    sch_target_init
    if [ "${SCH_TARGET_ISOLATION_ON}" = "true" ]; then
        sch_target_support owner-exec -- aws "$@"
    else
        aws "$@"
    fi
}

sch_target_stack_isolation() { # -> the runtime stack's IsolationStatus output ("" when unreadable)
    local value
    value="$(aws cloudformation describe-stacks \
        --stack-name "${SCH_PROJECT:-sch}-${SCH_ENV:-dev}-runtime" \
        --region "${SCH_REGION:-eu-west-1}" \
        --query "Stacks[0].Outputs[?OutputKey=='IsolationStatus'].OutputValue" \
        --output text 2>/dev/null)" || value=""
    [ "${value}" = "None" ] && value=""
    printf '%s\n' "${value}"
}

sch_target_skip_if_isolated() { # <reason>
    # A stack-level property: read from the runtime stack, so the check needs
    # neither the registry nor a listed caller.
    if [ "$(sch_target_stack_isolation)" = "true" ]; then
        echo "SKIP: $1"
        exit 0
    fi
}
