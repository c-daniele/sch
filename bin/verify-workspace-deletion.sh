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
: "${SCH_RUNTIME_ARN:?set SCH_RUNTIME_ARN to the test runtime ARN}"
: "${SCH_VERIFY_SESSION_ID:?set SCH_VERIFY_SESSION_ID to the active test runtime session ID}"

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
BIN="$ROOT/bin/sch"
REGISTRY_URL=${SCH_WORKSPACE_REGISTRY_URL:-}

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
