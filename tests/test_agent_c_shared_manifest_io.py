"""AGENT_C must use the shared manifest writer, not its own copy (#34).

(a) and (b) fail while AGENT_C carries a private ``atomic_update_manifest``.
(c) is a control that must stay green: AGENT_C runs as a script, so adding a
repo-level import must not break ``python AGENT_C/run_iss.py`` from outside
the repo with no PYTHONPATH.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def test_run_iss_atomic_update_manifest_is_the_shared_one():
    import run_iss
    from ava_shared import manifest_io

    assert run_iss.atomic_update_manifest is manifest_io.atomic_update_manifest


def test_run_iss_dotted_key_through_scalar_raises_conflict_and_leaves_file(tmp_path):
    import run_iss
    from ava_shared.manifest_io import ManifestPathConflict

    p = tmp_path / "manifest.json"
    original = b'{"phases": "legacy"}'
    p.write_bytes(original)
    with pytest.raises(ManifestPathConflict):
        run_iss.atomic_update_manifest(p, {"phases.iss.status": "done"})
    assert p.read_bytes() == original


def test_run_iss_help_runs_as_a_script_from_outside_the_repo(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run(
        [sys.executable, str(REPO / "AGENT_C" / "run_iss.py"), "--help"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "usage: run_iss.py" in proc.stdout
