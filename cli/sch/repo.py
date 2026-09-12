"""Support-repo resolution for the installed (pip) and checkout (bin/sch) CLI.

The `sch` client is stdlib-only and pip-installable, but several features
delegated to helper scripts that live in the repository: the Node tunnel
helpers (``tunnel/*.js`` used by ``--sync``, ``acp``, ``attach``, ``web``,
git-native bundle transfer) and the deployment tooling (``infra/deploy.sh``).
Neither belongs in the wheel (the tunnel is explicitly not published, and the
image build context is far too large), so the CLI locates a *support repo
checkout* at runtime.

Resolution order (first match wins; every candidate must contain the
``tunnel/sync.js`` marker to be accepted):

1. ``SCH_REPO_ROOT`` environment variable (explicit user override);
2. the managed checkout under the user data dir
   (``~/.local/share/sch/repo`` on POSIX, ``%LOCALAPPDATA%\\sch\\repo`` on
   Windows) — where `sch setup` clones it;
3. the legacy git-checkout-relative location: the grandparent of this
   package directory (``cli/sch/`` inside a repo clone), which keeps
   ``bin/sch`` / ``bin/sch.ps1`` working unchanged.

All functions return ``None`` when nothing valid is found; callers decide
whether that is fatal (most tunnel features die with a hint to run
``sch setup``).
"""

import os
import sys
from pathlib import Path

MARKER = Path("tunnel") / "sync.js"

# Fallback only: the URL is defined once, in pyproject.toml ([project.urls]
# Homepage), and read from the installed package metadata below so that moving
# the repository is a single edit. Keep the two in sync anyway -- a checkout
# run through bin/sch without an installed package sees only this constant
# (see docs/release/checklist.md).
_FALLBACK_REPO_URL = "https://github.com/c-daniele/sch.git"


def default_repo_url():
    """The support-repo URL ``sch setup`` clones when nothing else is configured."""
    try:
        from importlib import metadata

        for entry in metadata.metadata("sch").get_all("Project-URL") or []:
            label, _, url = entry.partition(",")
            url = url.strip().rstrip("/")
            if label.strip().lower() == "homepage" and url.startswith("https://"):
                return url if url.endswith(".git") else url + ".git"
    except Exception:  # not installed as a distribution: checkout via bin/sch
        pass
    return _FALLBACK_REPO_URL


DEFAULT_REPO_URL = default_repo_url()


def _has_marker(root):
    return (root / MARKER).is_file()


def _validated(root):
    root = Path(root).expanduser()
    if _has_marker(root):
        return root
    return None


def managed_root():
    """Well-known location of the managed support checkout (may not exist)."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return Path(base) / "sch" / "repo"
    xdg_data = os.environ.get("XDG_DATA_HOME") or ""
    if xdg_data:
        base = Path(xdg_data)
    else:
        base = Path.home() / ".local" / "share"
    return base / "sch" / "repo"


def legacy_root():
    """Grandparent of this package dir: ``cli/``'s parent in a repo clone."""
    return Path(__file__).resolve().parents[2]


def repo_root():
    """Resolve the support repo checkout, or ``None`` when absent.

    See the module docstring for the resolution order.
    """
    override = os.environ.get("SCH_REPO_ROOT") or ""
    if override:
        found = _validated(override)
        if found is None:
            return None
        return found
    for candidate in (managed_root(), legacy_root()):
        found = _validated(candidate)
        if found is not None:
            return found
    return None


def tunnel_dir():
    """The support repo's ``tunnel/`` directory, or ``None``."""
    root = repo_root()
    if root is None:
        return None
    return root / "tunnel"


def missing_message(feature):
    """Standard error text for a command that needs the support repo."""
    return (
        "{} requires the SCH support repo (tunnel helpers), but no checkout was"
        " found; set SCH_REPO_ROOT or run: sch setup".format(feature)
    )
