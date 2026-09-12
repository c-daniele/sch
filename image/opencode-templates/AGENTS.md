# Execution environment: AWS microVM

You are running inside a Bedrock AgentCore microVM (Amazon Linux 2023,
arm64), not on the user's machine.

## Facts

- Non-root user `sch` (uid 1000). No sudo, no docker daemon, no systemd.
  Container images CANNOT be built here: delegate to CodeBuild via
  `sch-build-image`.
- Lifecycle: max 8h per session, 15-min idle timeout. Nothing you leave
  in memory or outside the workspace survives.
- `/mnt/workspace` is the ONLY persistent storage (checkpointed to S3).
  Everything else on the filesystem is ephemeral.
- AWS credentials come from the runtime IAM role via IMDSv2. There are
  no static keys and you cannot switch identity. IAM is the real
  permission boundary — roughly: broad read-only access, Bedrock model
  invocation, and narrowly scoped checkpoint writes.
- The `aws-mcp` MCP server (managed AWS MCP Server via a local SigV4 proxy)
  is configured read-only; this is a UX guard,
  not a security boundary. The AWS CLI uses the same IAM role.

## Consequences

- Advantage: you are network-close to AWS. Reading AWS state (describe,
  list, get, CloudWatch logs/metrics) is fast and pre-authenticated —
  prefer verifying real AWS state over guessing.
- Limitation: assume AWS mutations will fail unless proven otherwise.
  When a task seems to require one, check first with
  `aws sts get-caller-identity` and a dry-run; report an IAM gap
  instead of retrying.
- Persist anything worth keeping (code, notes, reports) inside the
  repository under /mnt/workspace, and commit at meaningful points.

## Development environments

Bootstrapping a project environment is YOUR job, with the tools already
baked into this image. A dependency that is not installed yet is work to
do, not a blocker to report.

- Python: `uv` is installed (pinned version). With a `pyproject.toml`
  or `uv.lock`, run `uv sync`. Otherwise create a project-local venv:
  `uv venv .venv` then `uv pip install ...`. The interpreter is the
  system `python3.11` (`UV_PYTHON=python3.11`,
  `UV_PYTHON_DOWNLOADS=never` — uv never downloads a managed CPython).
  NEVER install packages into the system interpreter (no
  `pip3.11 install` outside a venv).
- Node / TypeScript: Node 22 and npm are installed. Lockfile first —
  `npm ci` when a lockfile exists, `npm install` only when it does not;
  respect the package manager the project declares.
- Native builds work: `make`, `gcc`, `gcc-c++` and the `python3.11`
  headers are present, so Python C extensions without an arm64 wheel and
  `node-gyp` native modules compile. `jq` is available for JSON in shell.
- Reproducibility: commit lockfiles, and if a project has no one-command
  bootstrap, add one. The microVM can be recycled at any time — an
  environment MUST be recreatable from the repository alone.
- Keep environments project-local (`.venv/`, `node_modules/` inside the
  worktree) and gitignored. Never depend on state living outside the
  repository.
