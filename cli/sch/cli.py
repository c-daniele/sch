"""Shared CLI plumbing: help/usage text and small argument-parsing helpers
used across the ``commands/*`` modules.
"""

import re
import sys

from .config import die

# Syntactic allowlist for `--model` values. Single implementation shared by
# `sch run` and `sch task` so the two flags can never drift (spec:
# run-model-selection, "Rule shared between run and task"; design D2 of
# add-task-model-flag).
_MODEL_RE = re.compile(r"[A-Za-z0-9._:/-]+")

USAGE_TEXT = """\
sch — Serverless Coding Harness client.
Manage remote coding-agent workspaces (OpenCode | Claude Code | Pi) running in
Amazon Bedrock AgentCore microVMs. See the README and docs/ for concepts and workflows.

Usage:
  sch setup [--repo <url>] [--deploy]      check prerequisites, obtain/update the support repo,
                                            install tunnel dependencies, report runtime-stack
                                            status; --deploy also deploys AWS (guided first run)
  sch deploy [-r <region>] [-e <env>] [-v <tag>] [-s] [-l]
                                            deploy/update the AWS side (bootstrap stack ->
                                            image build -> runtime stack); -l builds locally
                                            with Docker instead of CodeBuild, -s skips the
                                            image build; -h shows every option
  sch destroy [-r <region>] [-p <project>] [-e <env>] [--keep-checkpoints] [--dry-run] [--yes]
                                            delete every AWS resource of one deployment
                                            (runtime + bootstrap stacks, ECR images, buckets);
                                            prints the resolved account before touching anything
  sch uninstall [--keep-keys] [--keep-client] [--dry-run] [--yes] [--force]
                                            remove local state, the managed support checkout
                                            and the installed client (run `sch destroy` first)
  sch shell <workspace> [--harness <opencode|claude|pi>] [--storage <s3|session>] [--shell-id <id>] [sync options]
                                             open (or reconnect to) a shell
  sch open  <workspace> [--harness <opencode|claude|pi>] [--storage <s3|session>] [sync options]
                                             alias for `shell`
  sch run   <workspace> [--harness <opencode|claude|pi>] [--model <id>] [--branch <name>] [--continue] [--storage <s3|session>] [sync options]
                                              open a shell that jumps straight into
                                              the harness TUI in the active worktree; quitting the harness
                                              closes the session (no lingering remote shell)
                                              (--continue resumes the harness's latest session)
  sch list                                  list known workspaces + status
  sch dashboard [--interval <seconds>]      full-screen offline workspace dashboard
  sch task <workspace> [--harness <opencode|claude|pi>] [--model <id>] [--branch <name>] [--storage <s3|session>] [--continue] [--handoff [--handoff-session <id>] [--sanitize]] "<prompt>" [--timeout <s>] [sync options]
                                             submit a headless detached task
                                             (--handoff exports the local OpenCode session,
                                             imports it remotely, then submits with --continue;
                                             opencode harness only; seed-then-handoff with --branch)
  sch fetch <workspace> [--push] [--force]  collect a git-native session's work as a local branch
                                            (snapshot -> incremental bundle -> fast-forward import;
                                             --push forwards to origin with LOCAL credentials only)
  sch handoff <workspace> [--harness <opencode|claude|pi>] [--session <id>] [--sanitize] [--storage <s3|session>]
                                              push a local OpenCode session into the remote workspace
                                              (opencode harness only; new workspaces are created and bound to opencode)
  workspace storage: --storage s3|session (creation only; default s3; immutable)
  git-native mode (autonomous sessions): --branch <name> seeds the remote workspace
                from the local HEAD via git bundle and provisions the branch before the
                harness starts; mutually exclusive with the sync options; deliver with `sch fetch`.
  sync options: --sync <dir> | --no-sync [--bootstrap abort|local-wins|remote-wins|union]
                [--conflict abort|local-wins|remote-wins|keep-both]
                `--sync` requires Node.js and the AWS tunnel dependencies; progress is written to stderr.
  sch status <workspace> [--json] [--live]  show task status (offline-first; reads S3)
  sch stop <workspace>                      stop the runtime session (L2 checkpoint first)
  sch reset-session <workspace>             regenerate the local sessionId (confirms; tests/exercises L2 restore)
  sch delete <workspace> [--yes]            permanently delete one workspace
  sch delete --all [--yes]                  permanently delete all workspaces in the active scope
  sch info                                  show resolved runtime/region
  sch acp <workspace> [--harness <opencode|claude|pi>] [--storage <s3|session>] [--mirror <dir>]
                                            expose the workspace as an ACP (Agent Client Protocol) agent
                                            over stdio, for editors (Zed, JetBrains) to spawn directly.
                                            opencode and claude only (pi has no ACP agent). Keeps a local
                                            mirror of the remote worktree so editor path-based features work.
  sch zed-config <workspace>                print the `agent_servers` snippet for Zed's settings.json
                                            (JSON only on stdout, instructions on stderr)
  sch attach <workspace> [--harness opencode] [--storage <s3|session>] [--force]
                                            local OpenCode TUI connected to a remote `opencode serve`
                                            (rendering/clipboard/keybinding stay local, only API traffic
                                            crosses the network). opencode harness only.
  sch web <workspace> [--harness opencode] [--storage <s3|session>] [--no-browser]
                                            open the remote OpenCode web UI through a local bridge.
                                            No local opencode installation is required.

Env overrides:
  SCH_REGION       (default: eu-west-1)
  SCH_REPO_ROOT    (optional path to the support repo checkout — tunnel helpers,
                    infra tooling; auto-resolved from the managed checkout or a
                    git clone of this repository when unset)
  SCH_RUNTIME_ARN  (default: resolved from CloudFormation stack sch-dev-runtime)
  SCH_PROJECT/SCH_ENV (default: sch/dev — used for stack name resolution)
  SCH_CHECKPOINT_BUCKET (default: resolved from the same CloudFormation stack)
  SCH_DEFAULT_HARNESS (default: opencode — default harness for new workspaces)
  SCH_DEFAULT_STORAGE (default: s3 — default storage for new workspaces)
  SCH_WORKSPACE_REGISTRY_URL (optional HTTPS IAM-authenticated workspace registry)
  SCH_TUNNEL_MAX_CHANNELS (default: 32 — soft cap on concurrent `sch attach`
                    tunnel channels; a safety net, not a hard limit, since the
                    WebSocketStream transport shares no budget with `sch shell`)
  SCH_ACP_MIRROR_ROOT (default: ~/.config/sch/mirrors — root under which the
                    per-workspace ACP mirrors live, as <root>/<ws>/repo;
                    per-session override: sch acp --mirror <dir>)
  SCH_NODE_BIN / SCH_AWS_BIN (absolute-path overrides for the node / AWS CLI
                     binaries when `sch acp` is spawned by an editor with a
                     minimal PATH and the auto-resolution cannot find them)

Workspace/session mapping:
  SCH generates runtimeSessionId; AWS does not assign it. Without
  SCH_WORKSPACE_REGISTRY_URL, mappings are stored only on this client under
  ~/.config/sch/workspaces (or $XDG_CONFIG_HOME/sch/workspaces), are not shared
  across machines, and are lost if that local index is deleted. With the
  optional registry configured, its DynamoDB-backed API is authoritative.
"""


def usage(exit_code=0):
    print(USAGE_TEXT, end="")
    raise SystemExit(exit_code)


def confirm_phrase(expected, yes=False):
    """Ask the operator to type ``expected`` verbatim. True only when they did.

    Single implementation for every destructive command (`delete`, `destroy`,
    `uninstall`) so the wording cannot drift between them.

    Two details learned from an operator getting it wrong: the phrase is
    **quoted** in the prompt, because an unquoted `DELETE sch-dev` reads as the
    single word `DELETE` followed by prose; and the answer is normalized for
    surrounding quotes and whitespace, because a prompt that shows quotes
    invites typing them. Normalizing that does not weaken the guard — the whole
    phrase, environment name included, still has to be typed.

    EOF (no terminal on stdin) is reported explicitly instead of looking like a
    silent refusal: a redirected or backgrounded run cancels immediately, which
    is otherwise indistinguishable from a typo.
    """
    if yes:
        return True
    try:
        answer = input("Type '{}' to continue: ".format(expected))
    except (EOFError, KeyboardInterrupt):
        print(
            "\nsch: no confirmation received (stdin is not a terminal) —"
            " use --yes for unattended runs",
            file=sys.stderr,
        )
        return False
    return answer.strip().strip("\"'").strip() == expected


def validate_model_or_die(model):
    """Validate a ``--model`` value against the shared syntactic allowlist,
    dying with a usage error (without invoking the runtime) on an empty or
    out-of-allowlist value — spec: run-model-selection, "Syntactic
    validation of the model value".
    """
    if not _MODEL_RE.fullmatch(model):
        die(
            "invalid --model value '{}' (must match [A-Za-z0-9._:/-]+)".format(
                model
            )
        )
    return model


def parse_int_or_die(value, flag_name):
    """Validate ``value`` as a base-10 integer, dying (without invoking the
    runtime) on anything else — spec: cli-cross-platform, "Non-numeric
    timeout".
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        die(
            "invalid {} value '{}' (must be integer seconds)".format(
                flag_name, value
            )
        )
