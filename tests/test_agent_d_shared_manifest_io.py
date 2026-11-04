"""AGENT_D must write the manifest through the shared implementation (#34).

AGENT_D keeps ``ConfigError`` as its public failure type, because its callers
catch it.  Today it also silently replaces a scalar on a dotted path, which
the shared contract forbids.

The last test is the comparison #34 asked for: the same three bad inputs go
through AGENT_C's and AGENT_D's entry points and both must refuse and leave
the file byte-identical.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def test_dotted_key_through_scalar_raises_config_error_and_leaves_file(tmp_path):
    import compare_commitlogs as cc

    p = tmp_path / "manifest.json"
    original = b'{"phases": "legacy"}'
    p.write_bytes(original)
    with pytest.raises(cc.ConfigError):
        cc.atomic_update_manifest(p, {"phases.iss.status": "done"})
    assert p.read_bytes() == original


def test_missing_manifest_raises_config_error(tmp_path):
    import compare_commitlogs as cc

    with pytest.raises(cc.ConfigError):
        cc.atomic_update_manifest(tmp_path / "manifest.json", {"a": 1})


def test_corrupt_manifest_raises_config_error_and_leaves_file(tmp_path):
    import compare_commitlogs as cc

    p = tmp_path / "manifest.json"
    p.write_bytes(b"{not json")
    with pytest.raises(cc.ConfigError):
        cc.atomic_update_manifest(p, {"a": 1})
    assert p.read_bytes() == b"{not json"


def test_compare_commitlogs_help_runs_as_a_script_from_outside_the_repo(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run(
        [sys.executable, str(REPO / "AGENT_D" / "compare_commitlogs.py"), "--help"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "usage: compare_commitlogs.py" in proc.stdout


# name -> (initial bytes, or None for no file; updates)
BAD_INPUTS = {
    "missing": (None, {"a": 1}),
    "corrupt": (b"{not json", {"a": 1}),
    "scalar": (b'{"phases": "legacy"}', {"phases.iss.status": "done"}),
}


@pytest.mark.parametrize("agent", ["AGENT_C", "AGENT_D"])
@pytest.mark.parametrize("case", sorted(BAD_INPUTS))
def test_both_agents_refuse_bad_input_and_leave_the_file_alone(agent, case, tmp_path):
    if agent == "AGENT_C":
        import run_iss as mod
    else:
        import compare_commitlogs as mod

    initial, updates = BAD_INPUTS[case]
    p = tmp_path / "manifest.json"
    if initial is not None:
        p.write_bytes(initial)
    with pytest.raises(Exception):
        mod.atomic_update_manifest(p, updates)
    if initial is None:
        assert not p.exists()
    else:
        assert p.read_bytes() == initial
