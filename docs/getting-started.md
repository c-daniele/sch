# Getting started: prerequisites and first deployment

Everything a new AWS account needs before the first `sch run`, in order. The short version is in the [README](../README.md#installation); this guide carries the full prerequisite list and the optional-feature table. Deploy mechanics live in [Deploying and operating SCH](deploy.md).

## Installation

The `sch` client installs directly from the git repository — no PyPI
publication needed. It is a stdlib-only package (no dependencies, no
compilation) with full macOS / Linux / Windows parity:

```sh
pipx install git+https://github.com/c-daniele/sch.git
# pinned to a release tag (recommended once tags exist):
pipx install git+https://github.com/c-daniele/sch.git@v0.1.0
sch setup                 # check prerequisites, obtain the support repo,
                          # report runtime-stack status
sch shell my-workspace    # done — `sch` is on your PATH
```

`uv tool install git+...` and `pip install git+...` work the same way.

`sch` is self-sufficient for remote operations, but two feature families live
in a **support repo** checkout rather than in the wheel: the tunnel helpers
(`tunnel/*.js` — needed by `--sync`, `attach`, `web`, `acp`, git-native
transfer) and the deployment tooling (`infra/deploy.sh`, `image/`). `sch
setup` resolves (and, if missing, clones) that checkout for you; you can
point at any checkout with `SCH_REPO_ROOT`. To deploy the AWS stack as part
of setup, run `sch setup --deploy` (POSIX) or follow the printed steps on
Windows. See the normative spec:
[`docs/specs/platform/installation.md`](specs/platform/installation.md).

### Installing from source (git checkout)

```sh
git clone <repo-url> && cd sch
pipx install .            # same console script; or just run ./bin/sch
./bin/sch shell my-workspace
```

In a checkout, `bin/sch` / `bin/sch.ps1` work unchanged and resolve the
support repo automatically (they *are* in it). Prerequisites for the client
are **Python 3.8+** and **AWS CLI v2**; `npm install` inside `tunnel/` is
needed once for tunnel-based commands (`sch setup` checks all of this).

## From scratch on a new AWS account

The whole sequence, in order. Everything is required except **step 6**, which
is the single point where optional features are switched on: skipping it
deploys the documented defaults, and enabling one later is the same
`sch deploy` with the switch set.

```sh
# 1. local prerequisites (full list under Prerequisites; macOS shown)
brew install awscli node git pipx
npm install -g @aws/agentcore

# 2. credentials for the target account
aws configure
aws sts get-caller-identity        # confirm the account you are deploying into

# 3. Bedrock: submit Anthropic's one-time use-case form for this account
#    (Bedrock console -> Model catalog -> any Claude model), otherwise the
#    first agent turn fails with AccessDeniedException. See Prerequisites.

# 4. install the client
pipx install git+https://github.com/c-daniele/sch.git

# 5. prerequisite checks, support repo, tunnel dependencies
sch setup

# 6. OPTIONAL — enable the features you want on this deployment (table below);
#    nothing to do here for a default deployment

# 7. deploy: bootstrap stack -> image build -> runtime stack
sch deploy

# 8. first workspace
sch run my-workspace --sync .
```

Steps 5 and 7 collapse into `sch setup --deploy` when you want the defaults.
They are separate above because step 6 configures the support repo that step 5
obtains, and it must happen **before** the deploy — the deploy is what carries
the switches into AWS. `sch setup --deploy` passes no flags to the deploy, so a
non-default region or environment still needs `sch deploy -r <region>`
/ `-e <env>`.

### Step 6 — what is optional, and what you get by skipping it

| Optional feature | Default when skipped | Enable it |
| --- | --- | --- |
| Telegram notifications, and remote interaction | Off: no notifications, no inbound channel, no webhook | [docs/telegram-setup.md](telegram-setup.md) |
| Runtime IAM posture (capability tuning) | Every Bedrock model invokable, `ReadOnlyAccess` attached, no other AI service reachable | [docs/runtime-capability-tuning.md](runtime-capability-tuning.md) |
| IAM workspace registry (owner-scoped names) | Off: workspace-to-session mappings stay local to each client | [IAM Workspace Registry](workspaces.md#iam-workspace-registry) |
| Image rebuild from inside a session | Off: the runtime execution role gets no CodeBuild permission | [Image Rebuild from a Session](image-rebuild.md#image-rebuild-from-a-session-codebuild) |

Turning something on later is never a dead end, but two of them are not free:
Telegram and the in-session image rebuild add runtime environment variables, so
enabling or disabling them **bumps the AgentCore runtime version and restarts
every microVM from its last checkpoint** (no data loss — see
[L2 Durability](workspaces.md#l2-durability-s3-checkpoint)), exactly like any deploy that
rebuilds the image does ([What creates a new runtime
version](deploy.md#what-creates-a-new-runtime-version)). Capability tuning and the
workspace registry change no runtime environment and deploy in place.

Read [Deploy-time
switches](deploy.md#deploy-time-switches-optional-features) before step 6: they are
ordinary environment variables of one deploy, they are re-read on every deploy,
and an omitted switch is deployed as *off*.

## Prerequisites

- **Docker** (only for local builds, `./infra/deploy.sh -l`; the default
  CodeBuild build needs no Docker at all. A local arm64 build is native on
  Apple Silicon, `buildx` + QEMU elsewhere)
- **AWS CLI v2** with credentials that include `BedrockAgentCoreFullAccess`,
  CloudFormation, ECR, S3, CloudWatch Logs, and IAM (`iam:PassRole` limited to
  `*BedrockAgentCore*`: the role name in the template matches that pattern).
  The default (CodeBuild) build path also needs CodeBuild permissions —
  `codebuild:CreateProject`/`UpdateProject`/`DeleteProject` (CloudFormation
  creates the project) plus `codebuild:StartBuild`/`BatchGetBuilds`, which can
  be scoped to `arn:aws:codebuild:*:<account-id>:project/<project>-*`. A
  local build (`sch deploy -l`) needs none of them. When deploying the
  optional workspace registry, the deploy principal also needs
  `iam:PassRole` for `arn:aws:iam::<account-id>:role/<project>-<env>-workspace-registry-role`
  with `iam:PassedToService` set to `lambda.amazonaws.com`.
- **`agentcore` CLI** (npm): `npm install -g @aws/agentcore` (≥ 0.24)
- **Python 3.8+** (`python3` on macOS/Linux; `python3`, `python`, or the `py`
  launcher on Windows) — required to run the `sch` client CLI (`bin/sch` /
  `bin/sch.ps1` are thin shims around `cli/sch/`, stdlib-only, no `pip
  install` needed)
- **Bedrock model access** for the Claude inference profiles in the target
  region. In commercial regions, foundation-model access is enabled by default
  given AWS Marketplace permissions and a valid payment method — but
  **Anthropic models additionally require a one-time use-case form**, submitted
  once per account (or once at the AWS Organizations management account) from
  the Bedrock console's model catalog or with `PutUseCaseForModelAccess`.
  Without it, the first agent turn fails with `AccessDeniedException`, which is
  the most common failure of an otherwise correct first deploy. See
  [Request access to models](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html).
- Building locally behind a TLS-intercepting proxy (e.g. Netskope): drop the
  proxy CA certificates into `image/certs/*.pem` (gitignored) and build with
  `-l` — `infra/deploy.sh` automatically selects the local `corporate-ca`
  image base there. Remote (CodeBuild) builds never receive or process these
  certificates and ignore them.
