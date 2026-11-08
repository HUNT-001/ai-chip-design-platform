"""#142: a store whose size differs is a mismatch, even when addr and data agree.

Schema (AGENT_D/compare_commitlogs.py docstring): a store is (addr, size, data),
size in bytes 1|2|4|8.  Exit-code contract: 0 clean, 1 mismatch.  A byte store
and a word store of the same value are different architectural effects.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = str(REPO_ROOT / "AGENT_D" / "compare_commitlogs.py")
N = 6


def _log(size_at_3, other=4):
    out = []
    for i in range(N):
        sz = size_at_3 if i == 3 else other
        out.append(dict(schema_version="2.1.0", seq=i, hart=0, priv="M", trap=None,
                        pc=f"0x{0x80000000 + 4 * i:08x}", instr="0x00a12023",
                        regs={}, csrs={},
                        mem_writes=[{"addr": "0x80001000", "size": sz, "data": "0x0000002a"}]))
    return out


def _run(tmp_path, rtl, iss):
    paths = []
    for tag, log in (("rtl", rtl), ("iss", iss)):
        p = tmp_path / f"{tag}.jsonl"
        p.write_bytes(("\n".join(json.dumps(e) for e in log) + "\n").encode("utf-8"))
        paths.append(str(p))
    return subprocess.run([sys.executable, CLI, *paths], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=120)


def test_equal_sizes_are_clean(tmp_path):
    r = _run(tmp_path, _log(4), _log(4))
    assert r.returncode == 0, (r.stdout + r.stderr)[-500:]


@pytest.mark.parametrize("rtl_size,iss_size", [(2, 4), (4, 2), (1, 8), (8, 4)])
def test_a_store_whose_size_differs_is_a_mismatch(tmp_path, rtl_size, iss_size):
    r = _run(tmp_path, _log(rtl_size), _log(iss_size))
    out = r.stdout + r.stderr
    assert r.returncode == 1, f"size {rtl_size} vs {iss_size} exited {r.returncode}"
    assert "MEMMISMATCH" in out, out[-500:]


def test_the_controls_disagree():
    """Anti-vacuity: a clean pair and a differing pair must not look alike."""
    assert _log(4) != _log(2)
