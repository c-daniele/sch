"""Ensures `cli/` is importable as `sch.*` regardless of how the test
suite is invoked (``python3 -m unittest discover cli/tests``, pytest, an
IDE runner, etc.) — mirrors the sys.path fixup in ``sch/__main__.py``.
"""

import os
import sys

_CLI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _CLI_DIR not in sys.path:
    sys.path.insert(0, _CLI_DIR)
