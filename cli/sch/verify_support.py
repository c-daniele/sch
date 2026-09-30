"""Target resolution for the live ``bin/verify-*.sh`` scripts.

Not a ``sch`` command. The verification scripts run it by path
(``python3 cli/sch/verify_support.py <subcommand>``) through
``bin/lib/verify-target.sh``, so they address a workspace exactly as the
client does on every deployment mode:

- registry off: nothing here is used; the scripts keep their local-index
  path unchanged;
- registry on: the session ID, storage, epoch and workspace identity come
  from the registry (``sch`` mirrors them into the local index);
- isolation on (per-principal-isolation R40): the runtime is the caller's
  plane runtime, payloads name the workspace identity, and owner checkpoint
  objects are read through the plane access role with in-memory
  credentials.

Subcommands (JSON on stdout, diagnostics on stderr):

``probe``
    ``{"registry", "isolation", "plane"}``. With the registry off, a stack
    whose ``IsolationStatus`` output is ``true`` is an error (exit 5):
    every user of an isolated stack must be listed and use the registry.
    A registry refusal (for example the HTTP 403 of an unlisted caller)
    exits 4 with the registry's message.
``workspace <name> [--harness H] [--storage S]``
    Resolves (creating when new) the registry record, like any ``sch``
    command, and prints the addressing of the workspace.
``owner-exec -- <command...>``
    Runs the command with the access-role session in its environment when
    isolation is on, else unchanged; exits with the command's code.
``dashboard``
    The rows ``sch dashboard`` would render (its data path, no UI).
"""

import json
import os
import subprocess
import sys

if __package__ in (None, ""):
    _cli_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _cli_dir not in sys.path:
        sys.path.insert(0, _cli_dir)
    from sch import config as config_mod
    from sch import dashboard as dashboard_mod
    from sch import harness as harness_mod
    from sch import plane as plane_mod
    from sch import workspace_registry
else:
    from . import config as config_mod
    from . import dashboard as dashboard_mod
    from . import harness as harness_mod
    from . import plane as plane_mod
    from . import workspace_registry

EXIT_USAGE = 2
EXIT_REGISTRY_REFUSED = 4
EXIT_REGISTRY_REQUIRED = 5


class SupportError(Exception):
    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


def _plane_json(plane):
    if plane is None:
        return None
    return {
        "runtimeArn": plane.runtime_arn,
        "accessRoleArn": plane.access_role_arn,
        "ownerPrefix": plane.owner_prefix,
        "ownerKey": plane.owner_key,
    }


def stack_isolation_status(cfg, run=subprocess.run):
    """The runtime stack's ``IsolationStatus`` output, ``""`` when unreadable."""
    try:
        result = run(
            [
                "aws", "cloudformation", "describe-stacks",
                "--stack-name", cfg.stack_name(),
                "--region", cfg.region,
                "--query", "Stacks[0].Outputs[?OutputKey=='IsolationStatus'].OutputValue",
                "--output", "text",
            ],
            capture_output=True,
            text=True,
        )
    except OSError:
        return ""
    if result.returncode != 0:
        return ""
    value = (result.stdout or "").strip()
    return "" if value == "None" else value


def probe(cfg, run=subprocess.run):
    if not workspace_registry.enabled(cfg):
        if stack_isolation_status(cfg, run) == "true":
            raise SupportError(
                "stack {} has per-principal isolation on: set SCH_WORKSPACE_REGISTRY_URL "
                "and run as a principal listed in ISOLATED_PRINCIPALS (the shared "
                "runtime refuses every caller)".format(cfg.stack_name()),
                EXIT_REGISTRY_REQUIRED,
            )
        return {"registry": False, "isolation": False, "plane": None}
    try:
        workspace_registry.list_workspaces(cfg)
    except (RuntimeError, ValueError) as exc:
        raise SupportError(str(exc), EXIT_REGISTRY_REFUSED)
    return {
        "registry": True,
        "isolation": bool(cfg.isolation),
        "plane": _plane_json(cfg.plane),
    }


def describe_workspace(cfg, name, harness_flag="", storage_flag=""):
    if not workspace_registry.enabled(cfg):
        raise SupportError(
            "the workspace subcommand needs SCH_WORKSPACE_REGISTRY_URL (registry-off "
            "scripts read the local index themselves)",
            EXIT_USAGE,
        )
    resolved = harness_mod.resolve_harness(cfg, name, harness_flag, storage_flag)
    plane = plane_mod.active_plane(cfg)
    runtime_workspace = resolved.identity or name
    return {
        "workspace": name,
        "sessionId": resolved.sid,
        "harness": resolved.harness,
        "storage": resolved.storage,
        "sessionEpoch": resolved.epoch,
        "runtimeWorkspace": runtime_workspace,
        "created": bool(resolved.was_created),
        "isolation": plane is not None,
        "runtimeArn": config_mod.runtime_arn(cfg),
        "ownerPrefix": plane.owner_prefix if plane is not None else "",
        "accessRoleArn": plane.access_role_arn if plane is not None else "",
        "checkpointPrefix": plane_mod.checkpoint_key(cfg, runtime_workspace, ""),
        "writerKey": "{}{}.json".format(
            plane_mod.tree_prefix(cfg, "workspace-writers"), runtime_workspace
        ),
    }


def owner_exec(cfg, command, run=subprocess.run):
    if not command:
        raise SupportError("owner-exec needs a command after --", EXIT_USAGE)
    try:
        options = plane_mod.s3_run_options(cfg)
    except plane_mod.PlaneError as exc:
        raise SupportError(str(exc), 1)
    try:
        return run(command, **options).returncode
    except OSError as exc:
        raise SupportError("cannot run {}: {}".format(command[0], exc), 127)


def dashboard_rows(cfg):
    snapshot = dashboard_mod.aggregate_snapshot(cfg)
    return [
        {
            "name": row.name,
            "harness": row.harness,
            "storage": row.storage,
            "taskState": row.task_state,
            "manifestAgeS": row.manifest_age_s,
            "error": row.error,
        }
        for row in snapshot.workspaces
    ]


def _parse_workspace_args(args):
    if not args or args[0].startswith("-"):
        raise SupportError("usage: workspace <name> [--harness H] [--storage S]", EXIT_USAGE)
    name, rest = args[0], args[1:]
    flags = {"--harness": "", "--storage": ""}
    while rest:
        if rest[0] not in flags or len(rest) < 2:
            raise SupportError("usage: workspace <name> [--harness H] [--storage S]", EXIT_USAGE)
        flags[rest[0]] = rest[1]
        rest = rest[2:]
    return name, flags["--harness"], flags["--storage"]


def main(argv=None, cfg_factory=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__.strip(), file=sys.stderr)
        return EXIT_USAGE
    command, args = argv[0], argv[1:]
    try:
        cfg = (cfg_factory or config_mod.Config)()
        if command == "probe":
            print(json.dumps(probe(cfg), sort_keys=True))
            return 0
        if command == "workspace":
            name, harness_flag, storage_flag = _parse_workspace_args(args)
            print(json.dumps(describe_workspace(cfg, name, harness_flag, storage_flag),
                             sort_keys=True))
            return 0
        if command == "owner-exec":
            if args[:1] == ["--"]:
                args = args[1:]
            return owner_exec(cfg, args)
        if command == "dashboard":
            print(json.dumps(dashboard_rows(cfg), sort_keys=True))
            return 0
        raise SupportError("unknown subcommand '{}'".format(command), EXIT_USAGE)
    except SupportError as exc:
        print("verify-support: {}".format(exc), file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
