# Execution environment: AWS microVM

You are running inside a Bedrock AgentCore microVM (Amazon Linux 2023,
arm64), not on the user's machine.

## Facts

- Non-root user `sch` (uid 1000). No sudo, no Docker daemon, no systemd.
  Container images cannot be built here; delegate to CodeBuild with
  `sch-build-image`.
- Lifecycle: maximum 8 hours per session, with a 15-minute idle timeout.
  Memory and files outside the workspace are ephemeral.
- `/mnt/workspace` is the only persistent storage and is checkpointed to S3.
- AWS credentials come from the runtime execution role through IMDSv2. There
  are no static keys and the process cannot switch identity. IAM is the real
  authorization boundary.
- The `aws-mcp` MCP server (managed AWS MCP Server via a local SigV4 proxy)
  is configured read-only. This is a UX guard, not a
  security boundary; the AWS CLI uses the same IAM role.

## Consequences

- Prefer inspecting real AWS state over guessing; the microVM is
  pre-authenticated and network-close to AWS.
- Assume AWS mutations are denied unless proven otherwise. Check identity and
  use dry-run support where available rather than repeatedly retrying writes.
- Keep durable work in the repository under `/mnt/workspace` and commit at
  meaningful points.

## Development environments

Bootstrapping project environments is your responsibility, using the tooling
already baked into this image. A dependency that is not installed yet is work
to do, not a blocker to report.

- Python: `uv` is installed at a pinned version. Run `uv sync` when the project
  has a `pyproject.toml` or `uv.lock`; otherwise create a project-local
  environment with `uv venv .venv` followed by `uv pip install`. The
  interpreter is the system `python3.11` (`UV_PYTHON=python3.11` and
  `UV_PYTHON_DOWNLOADS=never`, so uv never downloads a managed CPython). Never
  install packages into the system interpreter.
- Node and TypeScript: Node 22 and npm are installed. Prefer the lockfile —
  `npm ci` when one exists and `npm install` only when it does not — and
  respect the package manager the project declares.
- Native builds are supported: `make`, `gcc`, `gcc-c++` and the `python3.11`
  headers are present, so Python C extensions without an arm64 wheel and
  `node-gyp` native modules compile. `jq` is available for JSON handling in
  shell scripts.
- Keep environments reproducible: commit lockfiles, and add a one-command
  bootstrap when a project lacks one. The microVM can be recycled at any time,
  so an environment must be recreatable from the repository alone.
- Keep environments project-local (`.venv/` and `node_modules/` inside the
  worktree) and gitignored; never depend on state outside the repository.
