"""The real coverage backend must import from the repo root (#41).

`ava_patched.py:152` does a top-level `from coverage_pipeline import ...`, but
`coverage_pipeline.py` lives in `AGENT_F/`. From the repo root -- which is how
`ava_patched.py` is actually run -- that import always fails, `except ImportError`
swallows it, and `COVERAGE_PIPELINE_AVAILABLE` is permanently False. Every
coverage number in a real run then comes from the fallback synthesiser, which is
#42's subject.

**Why this file uses subprocesses.** The bug is invisible to an ordinary test.
`pyproject.toml`'s `[tool.pytest.ini_options] pythonpath` lists `AGENT_F`
(`:53`), so under pytest the bare name resolves and the flag is already True --
the defect is dead in the suite and live only in a real run. Asserting the flag
in-process would pass today and prove nothing. So each test here launches a fresh
interpreter with the repo root as cwd and no AGENT_F on the path, which is the
environment the orchestrator actually runs in.

That is also why the fix must be a package-path import rather than appending
`AGENT_F/` to `sys.path`: `ava_patched.py` exists at the root AND in `AGENT_F/`,
and the AGENT_F copy is older, so putting that directory on the path invites #7's
shadowing. `tests/test_coverage_pipeline.py:39` made the same call for the same
reason.
"""

import ast
import json
import os
import pathlib
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

_PROBE = r"""
import json, sys
import ava_patched as av
cp = sys.modules.get("AGENT_F.coverage_pipeline") or sys.modules.get("coverage_pipeline")
print("PROBE" + json.dumps({
    "available": bool(av.COVERAGE_PIPELINE_AVAILABLE),
    "module_file": getattr(cp, "__file__", None),
    "agent_f_on_path": [p for p in sys.path if p.endswith("AGENT_F")],
}))
"""


def _probe_from_repo_root():
    """Import ava_patched the way a real run does: cwd = repo root, nothing
    injected onto sys.path, no inherited PYTHONPATH."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, (
        f"importing ava_patched from the repo root failed outright:\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    line = [l for l in proc.stdout.splitlines() if l.startswith("PROBE")]
    assert line, f"probe printed nothing usable:\n{proc.stdout}\n{proc.stderr}"
    return json.loads(line[-1][len("PROBE"):])


def test_the_coverage_backend_is_available_from_the_repo_root():
    """The defect itself. Not reproducible in-process -- see the module docstring."""
    info = _probe_from_repo_root()
    assert info["agent_f_on_path"] == [], (
        "this probe is meant to run without AGENT_F on sys.path; it leaked in "
        f"as {info['agent_f_on_path']} and the test would pass vacuously"
    )
    assert info["available"] is True, (
        "COVERAGE_PIPELINE_AVAILABLE is False in a run from the repo root, so "
        "every coverage number comes from the fallback synthesiser and the real "
        "Verilator backend is dead code outside pytest (#41)"
    )


def test_the_backend_resolves_to_the_agent_f_copy():
    """And it must resolve to AGENT_F's module, not some root-level shadow."""
    info = _probe_from_repo_root()
    assert info["module_file"], (
        "no coverage_pipeline module in sys.modules after importing ava_patched"
    )
    resolved = pathlib.Path(info["module_file"]).resolve()
    assert resolved == (REPO_ROOT / "AGENT_F" / "coverage_pipeline.py").resolve(), (
        f"the backend resolved to {resolved}, not AGENT_F's copy"
    )


def test_no_import_of_coverage_pipeline_by_bare_name():
    """Both import sites, pinned by AST so a new one is caught the day it lands.

    A bare `from coverage_pipeline import ...` only resolves when something has
    already put AGENT_F/ on sys.path -- true under pytest, false in a real run.
    There are two sites: the top-level one and the lazy CoverageDatabase import
    in AVA.__init__.
    """
    tree = ast.parse((REPO_ROOT / "ava_patched.py").read_text())
    bare = [
        n.lineno for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom)
        and n.module == "coverage_pipeline"
        and n.level == 0
    ]
    assert bare == [], (
        f"ava_patched.py imports coverage_pipeline by bare name at line(s) "
        f"{bare}; that only resolves when AGENT_F/ is on sys.path, which is "
        f"true under pytest and false in a run from the repo root (#41)"
    )
