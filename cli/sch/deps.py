"""Resolution of the ``node``/``aws``/``opencode`` binaries.

``sch acp`` is spawned by editors (Zed) OUTSIDE a login shell: PATH is
minimal (often ``/usr/bin:/bin`` only) and no shell rc has run. On POSIX we
resolve ``node``/``aws`` explicitly from well-known install locations
instead of assuming an enriched PATH, honoring the ``SCH_NODE_BIN`` /
``SCH_AWS_BIN`` absolute-path overrides. On failure: explicit error on
stderr, non-zero exit, nothing on stdout (spec: acp-editor-integration,
"Robustezza allo spawn da editor").

On Windows, resolution goes through :func:`shutil.which`, which already
knows how to find ``.exe``/``.cmd`` shims on ``PATH``.

``sch attach`` (invoked directly by a human in a terminal, not spawned by
an editor) only ever needs a plain PATH lookup for ``node``/``opencode`` —
it does not use the well-known-directories search below.
"""

import glob
import os
import re
import shutil

from .config import die

_WELL_KNOWN_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin")


def _home():
    return os.environ.get("HOME") or "/nonexistent"


def _extra_posix_dirs():
    home = _home()
    return list(_WELL_KNOWN_BIN_DIRS) + [
        os.path.join(home, ".local", "bin"),
        os.path.join(home, ".volta", "bin"),
        os.path.join(home, ".asdf", "shims"),
    ]


def _resolve_dep_bin_posix(name):
    found = shutil.which(name)
    if found:
        return found
    for directory in _extra_posix_dirs():
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _version_sort_key(path):
    """Best-effort natural sort key so that .../v10/... sorts after
    .../v9/... (mirrors ``sort -V`` used by the bash reference for the nvm
    fallback).
    """
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", path)]


def _nvm_fallback_node():
    home = _home()
    pattern = os.path.join(home, ".nvm", "versions", "node", "*", "bin", "node")
    candidates = [c for c in glob.glob(pattern) if os.access(c, os.X_OK)]
    if not candidates:
        return None
    candidates.sort(key=_version_sort_key)
    return candidates[-1]


def resolve_node_bin(cfg):
    """Resolve the ``node`` binary for spawning ``tunnel/acp.js``."""
    if cfg.node_bin_override:
        if not (
            os.path.isfile(cfg.node_bin_override)
            and os.access(cfg.node_bin_override, os.X_OK)
        ):
            die(
                "SCH_NODE_BIN='{}' is not an executable file — fix or unset it".format(
                    cfg.node_bin_override
                )
            )
        return cfg.node_bin_override

    if os.name != "posix":
        found = shutil.which("node")
        if found:
            return found
        die(
            "'node' not found on PATH. Install Node >= 18 or set "
            "SCH_NODE_BIN=/abs/path/to/node.exe"
        )

    found = _resolve_dep_bin_posix("node")
    if found:
        return found
    nvm_bin = _nvm_fallback_node()
    if nvm_bin:
        return nvm_bin
    die(
        "'node' not found (editors spawn 'sch acp' with a minimal PATH — "
        "searched PATH, {}, ~/.local/bin, ~/.volta/bin, ~/.asdf/shims, "
        "~/.nvm). Install Node >= 18 or set SCH_NODE_BIN=/abs/path/to/node".format(
            " ".join(_WELL_KNOWN_BIN_DIRS)
        )
    )


def resolve_aws_bin(cfg):
    """Resolve the ``aws`` CLI binary for the same editor-minimal-PATH
    scenario as :func:`resolve_node_bin`.
    """
    if cfg.aws_bin_override:
        if not (
            os.path.isfile(cfg.aws_bin_override)
            and os.access(cfg.aws_bin_override, os.X_OK)
        ):
            die(
                "SCH_AWS_BIN='{}' is not an executable file — fix or unset it".format(
                    cfg.aws_bin_override
                )
            )
        return cfg.aws_bin_override

    if os.name != "posix":
        found = shutil.which("aws")
        if found:
            return found
        die(
            "'aws' (AWS CLI) not found on PATH. Install the AWS CLI or set "
            "SCH_AWS_BIN=/abs/path/to/aws.exe"
        )

    found = _resolve_dep_bin_posix("aws")
    if found:
        return found
    die(
        "'aws' (AWS CLI) not found (editors spawn 'sch acp' with a minimal "
        "PATH — searched PATH, {}, ~/.local/bin, ~/.volta/bin, "
        "~/.asdf/shims). Install the AWS CLI or set "
        "SCH_AWS_BIN=/abs/path/to/aws".format(" ".join(_WELL_KNOWN_BIN_DIRS))
    )


def prepend_path(*dirs):
    """Prepend ``dirs`` to ``PATH`` in the current process environment, so
    every downstream ``aws``/``node`` subprocess call in this run resolves
    consistently (mirrors bash's ``export PATH=...``).
    """
    current = os.environ.get("PATH", "")
    prefix = os.pathsep.join(d for d in dirs if d)
    os.environ["PATH"] = prefix + (os.pathsep + current if current else "")


def which_node():
    """Plain-PATH lookup for ``node`` (used by ``sch attach``, which is run
    interactively and does not need the editor-minimal-PATH fallbacks).
    """
    return shutil.which("node")


def which_opencode():
    """Plain-PATH lookup for the local ``opencode`` binary (used by
    ``sch attach``).
    """
    return shutil.which("opencode")
