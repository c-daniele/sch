# TASK-20 live check (AC #2), single-operator PoC

Everything runs from this checkout (`dev`), not from the installed `sch` (uv tool from
`origin/dev`) nor from the managed repo `~/.local/share/sch/repo` (on `main`): neither has
the TASK-20 code until `dev` is merged. `bin/verify-isolation.sh` already uses `bin/sch`
and `cli/sch/verify_support.py` of this checkout.

Principals: A = your own IAM user (profile `personal_dev_mfa`, MFA session, `aws:userid`
stays the user's `AIDA…`), B = `sch-iso-b` (listed), C = `sch-iso-c` (unlisted). No
Identity Center user (optional in the check).

## 1. Test users (once)

```bash
.backlog/brainstorming/2026-09-28.IsolationLiveCheck/setup-test-principals.sh
aws sts get-caller-identity --query Arn --output text   # your user name is the last path segment
```

## 2. `infra/setenv.sh`

Comment out the three `TELEGRAM_*` / `ENABLE_TELEGRAM_INTERACTION` exports (the deploy
refuses them with isolation on), keep `ENABLE_WORKSPACE_REGISTRY=true`, add:

```bash
export ISOLATED_PRINCIPALS="user:<your-iam-user-name>, user:sch-iso-b"
```

## 3. Deploy in place (no destroy needed, reversible)

```bash
bin/sch list                 # stop any running session first: bin/sch stop <ws>
source infra/setenv.sh
infra/deploy.sh              # full deploy: the image changed (new shim), so no -s
```

What to watch: the preflight resolves both entries and prints the quota check; the
runtime stack update creates `CheckpointBucketPolicy`, `SharedRuntimeLock`,
`SharedRuntimeEndpointLock`; then two `sch-dev-plane-<owner key>` stacks. The first
live acceptance of `AWS::BedrockAgentCore::ResourcePolicy` happens here. The summary
prints one line per entry with the runtime ARN and stack status.

Existing workspaces are not migrated: they stay in DynamoDB and S3, invisible while
isolation is on, and reappear when it is turned off.

## 4. The check

```bash
bin/verify-isolation.sh --profile-a personal_dev_mfa --profile-b sch-iso-b --profile-c sch-iso-c
```

Expected: `isolation result: N passed, 0 failed`. It runs one headless task per listed
user (a few minutes for the first boot), deletes its test workspaces at the end, and
masks account IDs. Keep the output (redacted) for the task notes. Optional, as A:

```bash
bin/verify-headless-tasks.sh
bin/verify-persistence.sh
bin/verify-workspace-deletion.sh
```

## 5. Afterwards

Keep isolation on, drop the test users (chosen 2026-09-29):

```bash
.backlog/brainstorming/2026-09-28.IsolationLiveCheck/apply-caller-policy.sh dev_generic   # minimal policy on, test policy off
bin/sch info                                                                             # must still print the isolation line
sed -i '' 's/, user:sch-iso-b//' "$(readlink -f infra/setenv.sh)"                        # only your own user stays listed
source infra/setenv.sh && infra/deploy.sh -s                                             # deletes the plane of sch-iso-b
.backlog/brainstorming/2026-09-28.IsolationLiveCheck/teardown-test-principals.sh         # users, keys, test policy
```

`apply-caller-policy.sh` attaches the minimal identity policy of
`docs/getting-started.md` (built from the live stack) before detaching the
broad test policy, so `sch` never loses access; a working `bin/sch info` and
`bin/sch task` afterwards is the live confirmation of that minimal shape. To
use the installed `sch` against this stack, push `dev`, `uv tool upgrade sch`,
and point the tunnel helpers at this checkout with `SCH_REPO_ROOT=$PWD` until
`main` carries the work.

Back to the previous setup: remove `ISOLATED_PRINCIPALS`, restore the Telegram exports,
`source infra/setenv.sh && infra/deploy.sh -s` (deletes the two planes, drops the bucket
policy and the locks; the new image is fine with isolation off). Then:

```bash
.backlog/brainstorming/2026-09-28.IsolationLiveCheck/teardown-test-principals.sh
```

## Points the live run verifies for the first time

CloudFormation acceptance of the resource-policy locks and of the string escape-hatch
document; the joint runtime+endpoint deny; `requestContext.identity.user` as the principal
ID for a real IAM user; the 403 text; the bucket policy against real callers; the
`Decimal` epoch fix of the registry.
