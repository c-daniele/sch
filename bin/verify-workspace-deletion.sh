#!/usr/bin/env bash
set -eu

# Destructive verification. This is opt-in and requires a deployed runtime,
# checkpoint bucket, registry (for registry mode), and operator permissions.
# It intentionally refuses to guess deployment identifiers.
if [ "${SCH_DELETE_VERIFY:-}" != "1" ]; then
  printf '%s\n' 'Set SCH_DELETE_VERIFY=1 to run destructive workspace deletion verification.' >&2
  exit 2
fi

: "${SCH_VERIFY_WORKSPACE:?set SCH_VERIFY_WORKSPACE to a disposable workspace name}"
: "${SCH_CHECKPOINT_BUCKET:?set SCH_CHECKPOINT_BUCKET to the checkpoint bucket}"

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
BIN="$ROOT/bin/sch"
REGISTRY_URL=${SCH_WORKSPACE_REGISTRY_URL:-}

# Registry and isolation stacks (TASK-20.5).
# shellcheck source=lib/verify-target.sh
. "$ROOT/bin/lib/verify-target.sh"
sch_target_init

if sch_target_isolated; then
  # Isolation on (per-principal-isolation R17, R24, R38): local index mode is
  # not available (the shared runtime refuses every caller), and nobody but
  # the plane roles and SCH service roles can write owner trees, so the
  # operator cannot seed objects. The check deletes a real workspace through
  # the registry and reads what is left through the access role, which sees
  # current objects of checkpoints/ and workspace-writers/ only: remaining
  # object versions and checkpoint-generations/ are not visible to an owner.
  printf '%s\n' 'Isolation on: local deletion checks skipped (local index mode is unavailable).' >&2
  printf 'Verifying registry deletion for %s on the caller plane\n' "$SCH_VERIFY_WORKSPACE" >&2
  task_id=$("$BIN" task "$SCH_VERIFY_WORKSPACE" "workspace deletion probe: reply ok" | tail -n 1)
  [ -n "$task_id" ] || { printf '%s\n' 'task submission failed' >&2; exit 1; }
  state=""
  for _ in $(seq 1 120); do
    raw=$("$BIN" status "$SCH_VERIFY_WORKSPACE" --json) || test $? -eq 3
    state=$(printf '%s' "$raw" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("state", ""))')
    case "$state" in succeeded|failed|timed-out|interrupted) break ;; esac
    sleep 5
  done
  sch_target_workspace "$SCH_VERIFY_WORKSPACE"
  ckpt_prefix=$SCH_TARGET_CKPT_PREFIX
  writer_key="workspace-writers/$SCH_TARGET_OWNER_PREFIX/$SCH_TARGET_WS.json"
  "$BIN" delete "$SCH_VERIFY_WORKSPACE" --yes
  for prefix in "$ckpt_prefix" "$writer_key"; do
    count=$(sch_target_owner_aws s3api list-objects-v2 --bucket "$SCH_CHECKPOINT_BUCKET" \
      --prefix "$prefix" --region "${SCH_REGION:-eu-west-1}" --query 'KeyCount' --output text)
    if [ "$count" != "0" ]; then
      printf 'remaining objects under %s after deletion (%s)\n' "$prefix" "$count" >&2
      exit 1
    fi
  done
  printf '%s\n' 'Workspace deletion verification completed (isolation on).' >&2
  exit 0
fi

: "${SCH_RUNTIME_ARN:?set SCH_RUNTIME_ARN to the test runtime ARN}"
: "${SCH_VERIFY_SESSION_ID:?set SCH_VERIFY_SESSION_ID to the active test runtime session ID}"

seed_scope() {
  identity=$1
  key_prefixes="checkpoints/$identity/seed checkpoint-generations/$identity/seed"
  for key in $key_prefixes "workspace-writers/$identity.json"; do
    aws s3api put-object --bucket "$SCH_CHECKPOINT_BUCKET" --key "$key" \
      --body /dev/null --region "${SCH_REGION:-eu-west-1}" >/dev/null
    aws s3api put-object --bucket "$SCH_CHECKPOINT_BUCKET" --key "$key" \
      --body /dev/null --region "${SCH_REGION:-eu-west-1}" >/dev/null
    aws s3api delete-object --bucket "$SCH_CHECKPOINT_BUCKET" --key "$key" \
      --region "${SCH_REGION:-eu-west-1}" >/dev/null
  done
}

assert_empty() {
  identity=$1
  python3 - "$SCH_CHECKPOINT_BUCKET" "$identity" "${SCH_REGION:-eu-west-1}" <<'PY'
import json, subprocess, sys
bucket, identity, region = sys.argv[1:]
prefixes = [f"checkpoints/{identity}/", f"checkpoint-generations/{identity}/"]
for prefix in prefixes:
    raw = subprocess.check_output(["aws", "s3api", "list-object-versions", "--bucket", bucket,
                                   "--prefix", prefix, "--region", region, "--output", "json"], text=True)
    data = json.loads(raw or "{}")
    if data.get("Versions") or data.get("DeleteMarkers"):
        raise SystemExit(f"remaining versioned objects under {prefix}")
key = f"workspace-writers/{identity}.json"
raw = subprocess.check_output(["aws", "s3api", "list-object-versions", "--bucket", bucket,
                               "--prefix", key, "--region", region, "--output", "json"], text=True)
data = json.loads(raw or "{}")
for field in ("Versions", "DeleteMarkers"):
    if any(item.get("Key") == key for item in data.get(field, [])):
        raise SystemExit(f"remaining writer claim {key}")
PY
}

seed_scope "$SCH_VERIFY_WORKSPACE"

# Local mode uses the supplied active session ID and immutable index metadata;
# the command itself performs quiescence before touching the seeded objects.
mkdir -p "${XDG_CONFIG_HOME:-$HOME/.config}/sch/workspaces"
python3 - "$SCH_VERIFY_WORKSPACE" "$SCH_VERIFY_SESSION_ID" <<'PY'
import json, os, sys
ws, sid = sys.argv[1:]
root = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "sch", "workspaces")
with open(os.path.join(root, ws), "w") as fh:
    json.dump({"runtimeSessionId": sid, "harness": "claude", "storage": "s3", "sessionEpoch": 1}, fh)
    fh.write("\n")
PY

printf 'Verifying local deletion for %s (s3 backend)\n' "$SCH_VERIFY_WORKSPACE" >&2
SCH_DEFAULT_STORAGE=s3 "$BIN" delete "$SCH_VERIFY_WORKSPACE" --yes
assert_empty "$SCH_VERIFY_WORKSPACE"

# Recreate the same logical name through the normal path, proving that the
# deleted writer claim and checkpoint history do not block a fresh identity.
SCH_DEFAULT_STORAGE=s3 "$BIN" task "$SCH_VERIFY_WORKSPACE" "workspace deletion recreation probe" >/dev/null

seed_scope "$SCH_VERIFY_WORKSPACE"
python3 - "$SCH_VERIFY_WORKSPACE" "$SCH_VERIFY_SESSION_ID" <<'PY'
import json, os, sys
ws, sid = sys.argv[1:]
root = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "sch", "workspaces")
with open(os.path.join(root, ws), "w") as fh:
    json.dump({"runtimeSessionId": sid, "harness": "claude", "storage": "session", "sessionEpoch": 1}, fh)
    fh.write("\n")
PY

printf 'Verifying local deletion for %s (session backend caveat)\n' "$SCH_VERIFY_WORKSPACE" >&2
session_output=$(SCH_DEFAULT_STORAGE=session "$BIN" delete "$SCH_VERIFY_WORKSPACE" --yes 2>&1)
printf '%s\n' "$session_output" >&2
case "$session_output" in
  *"managed session storage may remain"*) ;;
  *) printf '%s\n' 'session backend caveat was not reported' >&2; exit 1 ;;
esac
assert_empty "$SCH_VERIFY_WORKSPACE"

if [ -n "$REGISTRY_URL" ]; then
  printf 'Verifying owner-scoped registry deletion\n' >&2
  seed_scope "$SCH_VERIFY_WORKSPACE"
  "$BIN" delete "$SCH_VERIFY_WORKSPACE" --yes
  assert_empty "$SCH_VERIFY_WORKSPACE"
  "$BIN" delete --all --yes
else
  printf '%s\n' 'Registry verification skipped: SCH_WORKSPACE_REGISTRY_URL is unset.' >&2
fi

printf '%s\n' 'Workspace deletion verification completed.' >&2
