"""
Root conftest.py — AVA Verification Platform
=============================================
Ensures all agent modules are importable when running pytest from the
project root.  With __init__.py now present in every agent package,
only the project root needs to be on sys.path.

Usage:
    pytest tests/                 # new agent test suite (46 tests)
    pytest AGENT_C/ AGENT_D/      # legacy per-agent tests
    pytest -k mismatch            # filtered
"""

import sys
from pathlib import Path
import importlib.util as _ilu

_ROOT = str(Path(__file__).resolve().parent)

# Project root — all AGENT_* packages resolve from here
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# AGENT_B internals still need explicit paths (sibling imports inside the package)
for _sub in ("AGENT_B", "AGENT_B/ava", "AGENT_B/backends"):
    _p = str(Path(_ROOT) / _sub)
    if _p not in sys.path:
        sys.path.append(_p)

# Register AGENT_G also as the legacy 'rv32im_testgen' alias used by some
# older tests that do `import rv32im_testgen` directly.
if "rv32im_testgen" not in sys.modules:
    _spec = _ilu.spec_from_file_location(
        "rv32im_testgen",
        str(Path(_ROOT) / "AGENT_G" / "__init__.py"),
        submodule_search_locations=[str(Path(_ROOT) / "AGENT_G")],
    )
    _pkg = _ilu.module_from_spec(_spec)
    sys.modules["rv32im_testgen"] = _pkg
    _spec.loader.exec_module(_pkg)
