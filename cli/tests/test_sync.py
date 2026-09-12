import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from sch.config import Config
from sch.sync import baseline_path, load_binding, parse_options, resolve_binding, save_binding


def config(root):
    old = os.environ.get("XDG_CONFIG_HOME")
    os.environ["XDG_CONFIG_HOME"] = root
    try:
        return Config()
    finally:
        if old is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = old


with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    cfg = config(td)
    project = root / "project"
    project.mkdir()
    canonical = save_binding(cfg, "owner/ws", project / ".")
    assert load_binding(cfg, "owner/ws") == canonical
    assert baseline_path(cfg, "owner/ws") != baseline_path(cfg, "other/ws")
    _, opts = parse_options(["--sync", str(project), "--bootstrap", "union", "--conflict", "keep-both"], "usage")
    assert resolve_binding(cfg, "owner/ws", opts) == canonical
    _, disabled = parse_options(["--no-sync"], "usage")
    assert resolve_binding(cfg, "owner/ws", disabled) is None
    _, passthrough = parse_options(["--sync", str(project), "--", "--sync", "prompt"], "usage")
    assert passthrough["sync"] == str(project)
    _, storage = parse_options(["--storage", "s3"], "usage")
    assert storage["storage"] == "s3"

    with patch("sch.sync.die", side_effect=SystemExit) as die:
        (root / "project").rename(root / "moved")
        try:
            load_binding(cfg, "owner/ws")
        except SystemExit:
            pass
        else:
            assert False, "unavailable binding must fail early"
        die.assert_called_once()
print("test_sync.py: ALL PASS")
