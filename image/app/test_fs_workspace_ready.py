"""Offline regression test for the fs tunnel bootstrap barrier.

The worker must not start until the asynchronous workspace bootstrap has made
the remote repo directory available.
"""

from __future__ import annotations

import asyncio
import ast
from pathlib import Path


source_path = Path(__file__).parent / "main.py"
tree = ast.parse(source_path.read_text(encoding="utf-8"))
open_target = next(
    node for node in tree.body
    if isinstance(node, ast.AsyncFunctionDef) and node.name == "_open_tunnel_target"
)
source = ast.get_source_segment(source_path.read_text(encoding="utf-8"), open_target)

assert "_WORKSPACE_READY.wait" in source
assert "FS_WORKSPACE_READY_TIMEOUT_S" in source
assert "REPO_DIR.is_dir()" in source
assert "workspace bootstrap did not complete before tunnel target" in source
assert asyncio.run(asyncio.to_thread(lambda: True)) is True
print("test_fs_workspace_ready.py: ALL PASS")
