"""`sch uninstall`: remove the local side of an SCH installation.

Counterpart of `sch destroy` (AWS side). It removes what the client wrote on
this machine — workspace index and caches, the managed support checkout, the
per-user provider keys, temporary artifacts — and finally the client package
itself, whichever tool installed it.

It deliberately refuses to run while a runtime stack is still deployed: the
managed support checkout carries the deploy tooling, and deleting it first
leaves an operator with live AWS resources and a longer road to tearing them
down. `--force` is there for the case where the AWS side is already gone (or
lives in an account these credentials cannot see).
"""

import argparse
import glob
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .. import cli as cli_mod
from .. import repo
from .setup import stack_status


def _parser():
    parser = argparse.ArgumentParser(
        prog="sch uninstall",
        description="Remove local SCH state, the support checkout and the client.",
    )
    parser.add_argument(
        "--keep-keys", action="store_true",
        help="keep the per-user provider keys (~/.sch/env)",
    )
    parser.add_argument(
        "--keep-client", action="store_true",
        help="remove local state but leave the installed 'sch' command in place",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="print what would be removed and exit without touching anything",
    )
    parser.add_argument(
        "--yes", action="store_true", help="skip the typed confirmation",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="uninstall even while a runtime stack is still deployed",
    )
    return parser


def provider_env_path():
    """The per-user provider key file (`~/.sch/env`)."""
    return Path.home() / ".sch" / "env"


def local_targets(cfg, keep_keys):
    """(label, path) pairs that exist on this machine, in removal order."""
    targets = []
    if cfg.config_dir.is_dir():
        targets.append(("local state (index + caches)", cfg.config_dir))
    managed = repo.managed_root()
    if managed.is_dir():
        targets.append(("managed support checkout", managed))
    if not keep_keys:
        env_path = provider_env_path()
        if env_path.is_file():
            targets.append(("provider keys", env_path))
    for path in sorted(glob.glob(str(Path(tempfile.gettempdir()) / "sch-*"))):
        targets.append(("temporary artifact", Path(path)))
    return targets


def detect_install(argv0=None):
    """How the running client was installed: (method, uninstall command).

    Detection is by asking the tools, not by guessing from paths: a pipx
    install, a uv tool install and a plain pip install all put `sch` on PATH
    the same way, and telling the operator the wrong uninstall command is
    worse than telling them none.
    """
    pipx = shutil.which("pipx")
    if pipx:
        rc, out = _run([pipx, "list", "--short"])
        if rc == 0 and any(
            line.split()[:1] == ["sch"] for line in out.splitlines() if line.strip()
        ):
            return "pipx", [pipx, "uninstall", "sch"]
    uv = shutil.which("uv")
    if uv:
        rc, out = _run([uv, "tool", "list"])
        if rc == 0 and any(
            line.split()[:1] == ["sch"] for line in out.splitlines() if line.strip()
        ):
            return "uv tool", [uv, "tool", "uninstall", "sch"]
    rc, _ = _run([sys.executable, "-m", "pip", "show", "sch"])
    if rc == 0:
        return "pip", [sys.executable, "-m", "pip", "uninstall", "-y", "sch"]
    return "checkout", []


def _run(argv):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return result.returncode, "{}{}".format(result.stdout, result.stderr)


def _confirm(yes):
    if cli_mod.confirm_phrase("UNINSTALL SCH", yes):
        return True
    print("sch: uninstall cancelled", file=sys.stderr)
    return False


def cmd_uninstall(cfg, args):
    opts = _parser().parse_args(args)

    if not opts.force:
        status, detail = stack_status(cfg)
        if status == "deployed":
            print(
                "sch: the runtime stack {} is still deployed in {} — destroy the"
                " AWS side first:\n"
                "  sch destroy\n"
                "Then re-run 'sch uninstall' (or pass --force to remove the local"
                " side anyway; the AWS resources would keep running and"
                " costing).".format(cfg.stack_name(), cfg.region),
                file=sys.stderr,
            )
            return 1
        if status == "error":
            print(
                "sch: cannot tell whether AWS resources are still deployed ({});"
                " re-run with --force if you know they are gone".format(detail),
                file=sys.stderr,
            )
            return 1

    targets = local_targets(cfg, opts.keep_keys)
    method, command = detect_install()

    print("sch: local state to remove")
    if targets:
        for label, path in targets:
            print("sch:   {:<32} {}".format(label, path))
    else:
        print("sch:   (nothing found)")
    if opts.keep_keys:
        print("sch:   provider keys kept (--keep-keys): {}".format(provider_env_path()))
    print("sch: installed client: {}".format(method))
    if opts.keep_client:
        print("sch:   left in place (--keep-client)")
    elif command:
        print("sch:   removal command: {}".format(" ".join(command)))
    else:
        print(
            "sch:   running from a git checkout — delete the clone yourself"
            " when done"
        )

    if opts.dry_run:
        print("sch: dry run — nothing was removed")
        return 0
    if not targets and (opts.keep_client or not command):
        print("sch: nothing to do")
        return 0
    if not _confirm(opts.yes):
        return 1

    failures = 0
    for label, path in targets:
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            print("sch: removed {} ({})".format(path, label))
        except OSError as exc:
            failures += 1
            print("sch: could not remove {}: {}".format(path, exc), file=sys.stderr)

    # Tidy the now-empty parents these paths lived in (~/.local/share/sch,
    # ~/.sch): leaving empty directories behind is the kind of residue that
    # makes an "uninstall" feel unfinished.
    for parent in {repo.managed_root().parent, provider_env_path().parent}:
        try:
            if parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
                print("sch: removed empty {}".format(parent))
        except OSError:
            pass

    if opts.keep_client or not command:
        return 1 if failures else 0

    if sys.platform == "win32":
        # Windows holds the running executable open; removing it now would
        # fail halfway. Printing is the honest outcome, not a fallback.
        print(
            "sch: local state removed. Finish by removing the client:\n"
            "  {}".format(" ".join(command))
        )
        return 1 if failures else 0

    print("sch: removing the client ({})".format(method))
    result = subprocess.run(command)
    if result.returncode != 0:
        print(
            "sch: the client is still installed — remove it with:\n"
            "  {}".format(" ".join(command)),
            file=sys.stderr,
        )
        return 1
    print("sch: uninstalled")
    return 1 if failures else 0
