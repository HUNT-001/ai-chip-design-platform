"""
Root conftest.py — AVA Verification Platform
=============================================
Ensures all agent modules are importable when running pytest from the project
root, without each test file needing its own sys.path manipulation.

Usage:
    pytest                        # runs all test suites
    pytest AGENT_C/               # only Agent C tests
    pytest AGENT_D/ -k mismatch   # filtered
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent

# Add each agent directory so tests can import sibling modules directly.
_AGENT_PATHS = [
    _ROOT,
    _ROOT / "AGENT_B",
    _ROOT / "AGENT_B" / "ava",
    _ROOT / "AGENT_B" / "backends",
    _ROOT / "AGENT_C",
    _ROOT / "AGENT_D",
    _ROOT / "AGENT_E",
    _ROOT / "AGENT_F",
    _ROOT / "AGENT_G",
]

for _p in _AGENT_PATHS:
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

# Register AGENT_G as the rv32im_testgen package so generate_tests.py
# and any test that does `import rv32im_testgen` works from the root.
import importlib.util as _ilu
if "rv32im_testgen" not in sys.modules:
    _spec = _ilu.spec_from_file_location(
        "rv32im_testgen",
        str(_ROOT / "AGENT_G" / "__init__.py"),
        submodule_search_locations=[str(_ROOT / "AGENT_G")],
    )
    _pkg = _ilu.module_from_spec(_spec)
    sys.modules["rv32im_testgen"] = _pkg
    _spec.loader.exec_module(_pkg)
