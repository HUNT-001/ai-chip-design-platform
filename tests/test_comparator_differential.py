"""#137: the orchestrator's comparator and AGENT_D must agree on pass/fail.

Fabricated commit-log pairs that differ only in stores are run through
ava_patched.CommitLogComparator and through AGENT_D's compare_commitlogs CLI
(exit 0 clean, 1 mismatch).  The expectation for each pair is written from the
schema (a store differs, or it does not), then both tools are held to it and to
each other.  A disagreement that is not a bug fixed here is listed in
KNOWN_DIVERGENCE with its issue, so it stays visible and cannot widen.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ava_patched import CommitLogComparator

N = 6
BASE = dict(schema_version="2.1.0", hart=0, priv="M", trap=None,
            instr="0x00a12023", regs={}, csrs={})


def _e(i, writes="__none__"):
    e = dict(BASE, seq=i, pc=f"0x{0x80000000 + 4 * i:08x}")
    if writes != "__none__":
        e["mem_writes"] = writes
    return e


def _w(addr="0x80001000", size=4, data="0x0000002a"):
    return {"addr": addr, "size": size, "data": data}


def _pair(rtl_w, iss_w, at=3):
    """Six entries per side; only entry `at` can differ."""
    rtl = [_e(i, [_w()]) for i in range(N)]
    iss = [_e(i, [_w()]) for i in range(N)]
    rtl[at] = _e(at, rtl_w)
    iss[at] = _e(at, iss_w)
    return rtl, iss


# name -> (rtl, iss, expected_to_fail)  -- expectation from the schema.
CASES = {
    "equal": (*_pair([_w()], [_w()]), False),
    "store_address_differs": (*_pair([_w(addr="0x80002000")], [_w()]), True),
    "store_size_differs": (*_pair([_w(size=2)], [_w()]), True),
    "store_data_differs": (*_pair([_w(data="0xbadbad00")], [_w()]), True),
    "missing_store_rtl": (*_pair([], [_w()]), True),
    "missing_store_iss": (*_pair([_w()], []), True),
    "one_sided_key": (
        [_e(i, [_w()]) for i in range(N)],
        [_e(i, [_w()]) if i != 3 else _e(i) for i in range(N)], True),
    "hex_vs_int_same_value": (
        *_pair([_w(addr=0x80001000, data=42)], [_w()]), False),
}

#: case -> (issue, which tool is wrong).  Empty unless a real divergence is found.
KNOWN_DIVERGENCE = {
    # AGENT_D never compares store size; the orchestrator does.  AGENT_D is the
    # one that is wrong, tracked in #142.
    "store_size_differs": "#142: AGENT_D ignores mem_size",
}


def _agent_d_fails(tmp_path, name, rtl, iss):
    paths = []
    for tag, log in (("rtl", rtl), ("iss", iss)):
        p = tmp_path / f"{name}_{tag}.jsonl"
        p.write_bytes(("\n".join(json.dumps(e) for e in log) + "\n").encode("utf-8"))
        paths.append(str(p))
    r = subprocess.run(
        [sys.executable, str(REPO_ROOT / "AGENT_D" / "compare_commitlogs.py"), *paths],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
        # This test is about the comparison, not the console.  AGENT_D's summary
        # crashes (exit 1) on a cp1252 console, which Windows CI has (#144).  Pin UTF-8 so a clean pair exits 0 there too.
        env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    out = (r.stdout or "") + (r.stderr or "")
    assert r.returncode in (0, 1), f"AGENT_D could not run: rc={r.returncode} {out[-600:]}"
    # rc 1 must be a store finding, not some other reason to exit 1
    assert (r.returncode == 1) == ("MEMMISMATCH" in out), (
        f"AGENT_D rc={r.returncode} does not match its own report:\n{out[-1200:]}")
    return r.returncode == 1


@pytest.mark.parametrize("name", sorted(CASES))
def test_orchestrator_and_agent_d_agree(tmp_path, name):
    rtl, iss, expect_fail = CASES[name]
    ava = bool(CommitLogComparator().compare(rtl, iss))
    agent_d = _agent_d_fails(tmp_path, name, rtl, iss)
    if name in KNOWN_DIVERGENCE:
        assert ava != agent_d, (
            f"{name} no longer diverges; remove it from KNOWN_DIVERGENCE "
            f"({KNOWN_DIVERGENCE[name]})")
        assert ava == expect_fail, f"{name}: the orchestrator must still be right"
        return
    assert agent_d == expect_fail, f"AGENT_D disagrees with the schema on {name}"
    assert ava == expect_fail, (
        f"{name}: expected fail={expect_fail}, orchestrator fail={ava}, AGENT_D fail={agent_d}")


def test_the_pairs_are_not_vacuous():
    """At least one pair must fail and one must pass, or agreement means nothing."""
    outcomes = {f for *_, f in CASES.values()}
    assert outcomes == {True, False}
