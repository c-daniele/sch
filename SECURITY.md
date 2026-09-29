# Security Policy

SCH provisions real AWS resources and runs autonomous coding agents inside them.
We take its security posture seriously.

## Reporting a vulnerability

**Do not open a public issue for a security vulnerability.**

Instead, use GitHub's *Report a vulnerability* button on the Security tab of this
repository (private disclosure to the maintainer). Include:

- Affected component (CLI, runtime image, tunnel, Lambda handlers, infra template)
- Reproduction steps or PoC
- Impact assessment, if you can

You will get an acknowledgment within 7 days and a status update at least every
14 days until resolution. We will credit reporters in release notes unless they
prefer anonymity.

## Scope

In scope:

- The `sch` CLI (`cli/sch`), `bin/` shims, and `tunnel/` local client
- The runtime image (`image/`) and what runs inside microVM sessions
- The Lambda handlers and infrastructure template (`infra/`)
- Checkpoint/restore, sync, and the git-native workflow (data exposure, credential leakage)
- The workspace registry control plane (authorization, tenant isolation)

Out of scope:

- Vulnerabilities in AWS-managed services themselves (report to AWS)
- Cost exposure caused by *deliberate* user configuration (e.g. intentionally
  raised quotas) — still tell us if defaults are unsafe
- Social engineering of AWS support or the maintainer

## Design posture (what we promise to maintain)

- The IAM execution role is the real permission boundary and is reviewed like
  security code. On a default deployment one execution role is shared by every
  workspace of the stack; with per-principal isolation each listed user has
  its own.
- Secrets must never be baked into the image or synced into workspaces; sync
  excludes secrets by design.
- Workspaces are separated per user by owner-scoped storage prefixes and a
  per-user registry identity. This separates names, not access: on a default
  deployment one user's agent can read another user's checkpoints, and a user
  who learns another user's session ID can join that session.
- Opt-in per-principal isolation (`ISOLATED_PRINCIPALS`) enforces that
  boundary for the users listed at deploy time: one runtime locked to its
  owner, an execution role confined to the owner's storage, and a checkpoint
  bucket policy. Principals that can change IAM, AgentCore runtimes or their
  resource policies, the bucket policy, SCH Lambdas, CloudFormation stacks or
  the plane parameters administer that boundary and can remove it. The
  boundary is checked by tests, IAM Access Analyzer and the IAM policy
  simulator, and by the live two-principal check (`bin/verify-isolation.sh`,
  passed 2026-09-29 with IAM users). A way around it for a listed user without those rights is in scope
  for reports. Details and residual risks:
  [security posture](docs/security.md#isolation-between-users),
  [spec](docs/specs/security/per-principal-isolation.md).
- Security fixes land on `main` and are released as patch versions.

## Supported versions

Only the latest tagged release receives security fixes. SCH is pre-1.0; upgrade
to stay patched.
