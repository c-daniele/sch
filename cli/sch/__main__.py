"""Top-level entry point: `python3 -m sch <command> [args...]`.

Also runnable by direct path (``python3 .../cli/sch/__main__.py``), which
is what the platform shims (``bin/sch``, ``bin/sch.ps1``) do — see the
``sys.path`` fixup below.
"""

import sys

MIN_PYTHON = (3, 8)

if sys.version_info < MIN_PYTHON:
    # Kept dependency-free and syntax-minimal: this must not itself raise a
    # SyntaxError on interpreters older than MIN_PYTHON.
    sys.stderr.write(
        "sch: requires python3 >= {}.{} (found {})\n".format(
            MIN_PYTHON[0], MIN_PYTHON[1], sys.version.split()[0]
        )
    )
    sys.exit(1)

import os  # noqa: E402  (after the version guard, by design)

if __package__ in (None, ""):
    # Executed directly as a script (python3 .../cli/sch/__main__.py)
    # rather than via `-m sch`: add the parent of this package directory
    # to sys.path so `sch.*` absolute imports resolve, then continue as if
    # this were run via `-m sch`. Avoids requiring the shim to set
    # PYTHONPATH (design D1 open question).
    _pkg_dir = os.path.dirname(os.path.abspath(__file__))
    _cli_dir = os.path.dirname(_pkg_dir)
    if _cli_dir not in sys.path:
        sys.path.insert(0, _cli_dir)

    from sch import cli as cli_mod
    from sch.config import Config, die
    from sch.commands import (
        acp,
        attach,
        dashboard,
        delete,
        deploy,
        destroy,
        fetch,
        handoff,
        info,
        list as list_cmd,
        reset_session,
        run as run_cmd,
        shell,
        status,
        stop,
        task,
        uninstall,
        web,
        setup,
        zed_config,
    )
else:
    from . import cli as cli_mod
    from .config import Config, die
    from .commands import (
        acp,
        attach,
        dashboard,
        delete,
        deploy,
        destroy,
        fetch,
        handoff,
        info,
        list as list_cmd,
        reset_session,
        run as run_cmd,
        shell,
        status,
        stop,
        task,
        uninstall,
        web,
        setup,
        zed_config,
    )

COMMANDS = {
    "shell": shell.cmd_shell,
    "open": shell.cmd_open,
    "run": run_cmd.cmd_run,
    "list": list_cmd.cmd_list,
    "task": task.cmd_task,
    "fetch": fetch.cmd_fetch,
    "handoff": handoff.cmd_handoff,
    "status": status.cmd_status,
    "stop": stop.cmd_stop,
    "reset-session": reset_session.cmd_reset_session,
    "info": info.cmd_info,
    "acp": acp.cmd_acp,
    "zed-config": zed_config.cmd_zed_config,
    "setup": setup.cmd_setup,
    "deploy": deploy.cmd_deploy,
    "destroy": destroy.cmd_destroy,
    "uninstall": uninstall.cmd_uninstall,
    "attach": attach.cmd_attach,
    "dashboard": dashboard.cmd_dashboard,
    "web": web.cmd_web,
    "delete": delete.cmd_delete,
}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        cli_mod.usage(0)

    command, rest = argv[0], argv[1:]
    handler = COMMANDS.get(command)
    if handler is None:
        die("unknown command '{}' (try: sch --help)".format(command))

    cfg = Config()
    rc = handler(cfg, rest)
    return rc if isinstance(rc, int) else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except KeyboardInterrupt:
        sys.exit(130)
