#!/bin/bash
# Verify owner-scoped registry behavior using two configured AWS CLI profiles.
set -euo pipefail

if [ "$#" -ne 4 ]; then
    echo "usage: $0 <registry-url> <profile-a> <profile-b> <checkpoint-bucket>" >&2
    exit 2
fi

REGISTRY_URL="$1"
PROFILE_A="$2"
PROFILE_B="$3"
CHECKPOINT_BUCKET="$4"
WORKSPACE="registry-verify-$(date +%s)"
SCH="$(dirname "$0")/sch"
SUPPORT="$(dirname "$0")/../cli/sch/verify_support.py"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

run_as() {
    local profile="$1"
    local config_home="$2"
    shift 2
    AWS_PROFILE="$profile" XDG_CONFIG_HOME="$config_home" SCH_WORKSPACE_REGISTRY_URL="$REGISTRY_URL" "$SCH" "$@"
}

# Registry and isolation stacks (TASK-20.5): with isolation on, each owner's
# objects live under its owner segment and are read through its access role.
support_as() {
    local profile="$1"
    local config_home="$2"
    shift 2
    AWS_PROFILE="$profile" XDG_CONFIG_HOME="$config_home" SCH_WORKSPACE_REGISTRY_URL="$REGISTRY_URL" python3 "$SUPPORT" "$@"
}

status_key() { # <profile> <config-home> <identity>
    local prefix
    prefix="$(support_as "$1" "$2" probe | python3 -c 'import json,sys; p=json.load(sys.stdin).get("plane") or {}; print(p.get("ownerPrefix", ""))')"
    printf 'checkpoints/%s%s/task-status.json\n' "${prefix:+$prefix/}" "$3"
}

wait_for_task() {
    local profile="$1"
    local config_home="$2"
    local state="" raw=""
    for _ in $(seq 1 60); do
        # Split from the pipeline on purpose: `sch status` exits 3 on a stale
        # `running` state (add-task-liveness-safety task 1.2), which `set -e`
        # plus `pipefail` would otherwise turn into an unexplained abort.
        raw="$(run_as "$profile" "$config_home" status "$WORKSPACE" --json)" || test $? -eq 3
        state="$(printf '%s' "$raw" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("state", "none"))')"
        case "$state" in
            succeeded|failed|timed-out|interrupted) return 0 ;;
        esac
        sleep 5
    done
    echo "task for ${profile} did not finish (last state: ${state})" >&2
    return 1
}

echo "==> resolving ${WORKSPACE} as both IAM principals and writing checkpoint status"
run_as "$PROFILE_A" "$TMP_DIR/a" task "$WORKSPACE" --harness opencode "Reply exactly: registry verification A" >/dev/null
run_as "$PROFILE_B" "$TMP_DIR/b" task "$WORKSPACE" --harness opencode "Reply exactly: registry verification B" >/dev/null
wait_for_task "$PROFILE_A" "$TMP_DIR/a"
wait_for_task "$PROFILE_B" "$TMP_DIR/b"

LIST_A="$(run_as "$PROFILE_A" "$TMP_DIR/a" list)"
LIST_B="$(run_as "$PROFILE_B" "$TMP_DIR/b" list)"
SID_A="$(printf '%s\n' "$LIST_A" | awk -v ws="$WORKSPACE" '$1 == ws { print $3 }')"
SID_B="$(printf '%s\n' "$LIST_B" | awk -v ws="$WORKSPACE" '$1 == ws { print $3 }')"
IDENTITY_A="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["workspaceIdentity"])' "$TMP_DIR/a/sch/workspaces/$WORKSPACE")"
IDENTITY_B="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["workspaceIdentity"])' "$TMP_DIR/b/sch/workspaces/$WORKSPACE")"

test -n "$SID_A"
test -n "$SID_B"
test "$SID_A" != "$SID_B"
test "$IDENTITY_A" != "$IDENTITY_B"
test "$(printf '%s\n' "$LIST_A" | awk -v ws="$WORKSPACE" '$1 == ws { count++ } END { print count+0 }')" = 1
test "$(printf '%s\n' "$LIST_B" | awk -v ws="$WORKSPACE" '$1 == ws { count++ } END { print count+0 }')" = 1
KEY_A="$(status_key "$PROFILE_A" "$TMP_DIR/a" "$IDENTITY_A")"
KEY_B="$(status_key "$PROFILE_B" "$TMP_DIR/b" "$IDENTITY_B")"
support_as "$PROFILE_A" "$TMP_DIR/a" owner-exec -- aws s3api head-object --bucket "$CHECKPOINT_BUCKET" --key "$KEY_A" >/dev/null
support_as "$PROFILE_B" "$TMP_DIR/b" owner-exec -- aws s3api head-object --bucket "$CHECKPOINT_BUCKET" --key "$KEY_B" >/dev/null

echo "verified: same logical workspace maps to isolated owner-scoped sessions and checkpoint prefixes"
