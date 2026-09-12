"""`sch zed-config`: print the `agent_servers` snippet for Zed's
settings.json.

Contract: stdout carries EXCLUSIVELY valid JSON (parsable with
`python3 -m json.tool`); every instruction/diagnostic goes to stderr. The
snippet does NOT encode the harness — `sch acp` resolves the persisted
harness at runtime, so the entry stays valid for legacy or
not-yet-created workspaces.
"""

import json
import os
import shutil
import sys

from .. import repo, workspace
from ..config import die


def _agent_server_command(ws):
    """Return (command, args) for Zed to spawn `sch acp <ws>` on this
    platform. Zed executes the command directly (no shell). A pip-installed
    `sch` is preferred when found on PATH (its console script is directly
    executable on every platform); otherwise fall back to the support
    repo's shim (`bin/sch`, or `bin/sch.ps1` via PowerShell on Windows —
    where `.ps1` scripts are not directly executable).
    """
    installed = shutil.which("sch")
    if installed:
        return installed, ["acp", ws]
    root = repo.repo_root()
    if root is None:
        die(repo.missing_message("zed-config"))
    if os.name == "posix":
        return str(root / "bin" / "sch"), ["acp", ws]
    return "pwsh", ["-NoLogo", "-NoProfile", "-File", str(root / "bin" / "sch.ps1"), "acp", ws]


def cmd_zed_config(cfg, args):
    if not args or not args[0]:
        die("usage: sch zed-config <workspace>")
    ws = args[0]
    if len(args) > 1:
        die("unexpected argument '{}' (usage: sch zed-config <workspace>)".format(args[1]))
    workspace.validate_workspace_name(ws)

    mirror_dir = cfg.acp_mirror_root / ws / "repo"
    # Create the mirror upfront so the instructions below work immediately:
    # Zed cannot open a folder that does not exist yet.
    try:
        mirror_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        die("cannot create mirror directory '{}'".format(mirror_dir))

    lines = [
        "sch: Zed setup for workspace '{}' — three steps:".format(ws),
        "sch:",
        "sch: 1) Add the JSON printed below to Zed's settings.json:",
        "sch:      in Zed press cmd-shift-p -> 'zed: open settings file'.",
        'sch:      If a top-level "agent_servers" object is already there, copy',
        'sch:      only the inner "SCH {}" entry into it; otherwise paste'.format(ws),
        "sch:      the whole object as printed.",
        "sch:",
        "sch: 2) In Zed, open this folder as your project (File > Open...):",
        "sch:      {}".format(mirror_dir),
        "sch:    It exists now but is EMPTY: it is the local mirror of the remote",
        "sch:    repo, and it fills up automatically the first time the agent",
        "sch:    connects (first hydration can take a while on big repos).",
        "sch:    (custom location: SCH_ACP_MIRROR_ROOT or 'sch acp {} --mirror <dir>')".format(ws),
        "sch:",
        "sch: 3) Open Zed's Agent Panel, start a new thread with the external",
        "sch:    agent 'SCH {}', and send a prompt. sch starts the remote".format(ws),
        "sch:    workspace, syncs the mirror, and from then on jump-to-file,",
        "sch:    inline diffs and @file mentions work on local paths.",
        "sch:",
        "sch: Note: the workspace harness (opencode|claude|pi) is resolved at runtime",
        "sch: by 'sch acp' — this snippet never needs regenerating for that.",
    ]
    for line in lines:
        print(line, file=sys.stderr)

    command, cmd_args = _agent_server_command(ws)
    snippet = {
        "agent_servers": {
            "SCH {}".format(ws): {
                "type": "custom",
                "command": command,
                "args": cmd_args,
                "env": {},
            }
        }
    }
    print(json.dumps(snippet, indent=2))
    return 0
