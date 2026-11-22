"""An RTL log may end before the terminal self-loop only if it says why (#178).

#165 defined the program's end as the terminal self-loop (`j .`) and made a log
that stops short of it a finding. A testbench that halts on a store, as Ibex's
SimCtrl does on the write of 1 to its sim-control word, never retires that
`j .`, so every correct run of it was flagged.

The rule under test, from the issue and the maintainer's decision:

  * The DUT side attests why its log stopped:
    `rtl_results["rtl_ended"] = {"reason": "halt_write", "addr": "0x..."}`.
    The commit-log schema has no log-level metadata slot (every property is
    per-entry, `unevaluatedProperties: false`), so the declaration travels in
    the dict that carries the log, beside `commit_log`.
  * The comparator verifies the attestation against the log. The early end is
    accepted only if the last RTL entry is a store to that address AND
    everything the ISS has after the RTL's last entry is the terminal
    self-loop and its repeats.
  * An early end without a declaration is a finding, as before.
  * A declaration that fails any check is itself a finding, never ignored.
  * The verdict records what was declared in `program_end`.

Inputs are the four real Ibex runs and Spike's logs on the same ELFs
(validation/ibex/fixtures/, #175), never edited. The declaration in these
tests takes its address from the log's own last store.
"""
import asyncio
import copy
import functools
import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ava_patched import SpikeISS

VAL = REPO_ROOT / "validation" / "ibex"
FIX = VAL / "fixtures"
PROGRAMS = ("arith_branch", "muldiv", "loadstore", "csr")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ibex = _load("declared_halt_trace_to_commitlog", VAL / "trace_to_commitlog.py")
spike = _load("declared_halt_spike_to_commitlog", VAL / "spike_to_commitlog.py")
harness = _load("declared_halt_compare", VAL / "compare.py")


@functools.lru_cache(maxsize=None)
def _pair(prog):
    rtl = ibex.convert_file(str(FIX / f"{prog}.ibex.log"))
    iss = spike.convert((FIX / f"{prog}.spike.log").read_text(encoding="utf-8"),
                        FIX / f"{prog}.elf")
    return rtl, iss


def logs(prog):
    rtl, iss = _pair(prog)
    return copy.deepcopy(rtl), copy.deepcopy(iss)


def halt_addr(rtl):
    return rtl[-1]["mem_writes"][-1]["addr"]


def declared(addr):
    return {"reason": "halt_write", "addr": addr}


def compare(tmp_path, rtl, iss, rtl_ended="absent"):
    """SpikeISS._compare_results, the orchestrator's comparison."""
    rtl_results = {"commit_log": copy.deepcopy(rtl), "instructions": len(rtl)}
    if rtl_ended != "absent":
        rtl_results["rtl_ended"] = copy.deepcopy(rtl_ended)
    sim = SpikeISS(timeout=30, run_dir=tmp_path / "cmp")
    return asyncio.run(sim._compare_results(
        rtl_results, {"commit_log": copy.deepcopy(iss), "instructions": len(iss)}))


def texts(comparison):
    return [b["description"] for b in comparison["bugs"]]


def says_end_differs(comparison):
    return any("Program end differs" in t for t in texts(comparison))


def rejects_declaration(comparison):
    return any("declared" in t.lower() for t in texts(comparison))


# ── the fixtures hold what these cases need ──────────────────────────────────

@pytest.mark.parametrize("prog", PROGRAMS)
def test_the_fixture_ends_at_a_halt_store_and_spike_runs_on(prog):
    """Anti-vacuity: the early end really is the case under test."""
    rtl, iss = logs(prog)
    n = len(rtl)
    assert any(w["addr"] == halt_addr(rtl) for w in rtl[-1]["mem_writes"])
    assert all(e["pc"] != rtl[-1]["pc"] for e in rtl[:-1]), "the halt store is not unique"
    # Spike has the loop starting right after the halt store, and only the loop after it
    assert iss[n]["pc"] == iss[n + 1]["pc"] != rtl[-1]["pc"]
    assert len({e["pc"] for e in iss[n:]}) == 1 and len(iss) > n + 10


# ── accepted: declared, last entry is the store, only the loop remains ───────

@pytest.mark.parametrize("prog", PROGRAMS)
def test_a_declared_halt_write_ending_before_the_loop_passes(tmp_path, prog):
    rtl, iss = logs(prog)
    c = compare(tmp_path, rtl, iss, declared(halt_addr(rtl)))
    assert c["bugs"] == [], texts(c)


@pytest.mark.parametrize("prog", PROGRAMS)
def test_the_verdict_records_what_was_declared(tmp_path, prog):
    rtl, iss = logs(prog)
    addr = halt_addr(rtl)
    c = compare(tmp_path, rtl, iss, declared(addr))
    pe = c["program_end"]
    assert pe is not None, "an accepted early end must not be silent"
    assert pe["end"] == "rtl_stopped_before_terminal_loop"
    assert pe["rtl_ended"] == {"reason": "halt_write", "addr": addr}
    assert pe["index"] == len(rtl) and pe["rtl_repeats_dropped"] == 0
    assert pe["iss_repeats_dropped"] == len(iss) - len(rtl)


# ── refused: no declaration ──────────────────────────────────────────────────

@pytest.mark.parametrize("prog", PROGRAMS)
def test_the_same_logs_without_a_declaration_still_fail(tmp_path, prog):
    rtl, iss = logs(prog)
    c = compare(tmp_path, rtl, iss)
    assert says_end_differs(c), texts(c)
    assert not rejects_declaration(c), "nothing was declared, so nothing can be rejected"


# ── refused: the declaration does not hold against the log ───────────────────

@pytest.mark.parametrize("prog", PROGRAMS)
def test_a_declaration_for_another_address_fails(tmp_path, prog):
    rtl, iss = logs(prog)
    wrong = f"0x{int(halt_addr(rtl), 16) + 8:08x}"
    c = compare(tmp_path, rtl, iss, declared(wrong))
    assert says_end_differs(c) and rejects_declaration(c), texts(c)


@pytest.mark.parametrize("prog", PROGRAMS)
@pytest.mark.parametrize("back", [1, 2, 3])
def test_a_declaration_on_a_log_that_stops_short_of_the_halt_store_fails(tmp_path, prog, back):
    """back=1 is the log missing its final store; 2 and 3 stop earlier still."""
    rtl, iss = logs(prog)
    addr = halt_addr(rtl)
    c = compare(tmp_path, rtl[:-back], iss, declared(addr))
    assert says_end_differs(c) and rejects_declaration(c), texts(c)


@pytest.mark.parametrize("prog", PROGRAMS)
def test_a_declaration_on_a_log_truncated_mid_program_fails(tmp_path, prog):
    rtl, iss = logs(prog)
    addr = halt_addr(rtl)
    for cut in (1, len(rtl) // 4, len(rtl) // 2, 3 * len(rtl) // 4):
        c = compare(tmp_path, rtl[:cut], iss, declared(addr))
        assert says_end_differs(c) and rejects_declaration(c), (cut, texts(c))


@pytest.mark.parametrize("prog", PROGRAMS)
def test_a_declaration_fails_when_the_loop_is_not_the_only_remainder(tmp_path, prog):
    """The RTL ends on the halt store, but Spike retired another instruction
    between that store and the loop, so the log stopped short of the program."""
    rtl, iss = logs(prog)
    n = len(rtl)
    extra = {"seq": n, "pc": "0x00100ff0", "instr": "0x00000013", "regs": {}}
    iss = iss[:n] + [extra] + iss[n:]
    c = compare(tmp_path, rtl, iss, declared(halt_addr(rtl)))
    assert says_end_differs(c) and rejects_declaration(c), texts(c)


@pytest.mark.parametrize("prog", PROGRAMS)
def test_a_declared_log_with_an_instruction_after_the_loop_fails(tmp_path, prog):
    rtl, iss = logs(prog)
    n = len(rtl)
    loop = copy.deepcopy(iss[n])
    after = {"seq": n + 1, "pc": f"0x{int(loop['pc'], 16) + 4:08x}",
             "instr": "0x00000013", "regs": {}}
    c = compare(tmp_path, rtl + [loop, after], iss, declared(halt_addr(rtl)))
    assert c["bugs"] and rejects_declaration(c), texts(c)


@pytest.mark.parametrize("prog", PROGRAMS)
def test_a_declaration_on_a_log_that_kept_running_into_the_loop_fails(tmp_path, prog):
    """It says it stopped on the halt write, but the log goes on to retire the
    loop, so the stop reason it gave is not what ended the log."""
    rtl, iss = logs(prog)
    n = len(rtl)
    spin = [dict(copy.deepcopy(iss[n]), seq=n + k) for k in range(3)]
    c = compare(tmp_path, rtl + spin, iss, declared(halt_addr(rtl)))
    assert rejects_declaration(c), texts(c)


@pytest.mark.parametrize("bad", [
    {}, [], "halt_write", 7,
    {"reason": "timeout", "addr": "ADDR"},
    {"reason": "halt_write"},
    {"addr": "ADDR"},
    {"reason": "halt_write", "addr": "not-an-address"},
    {"reason": "halt_write", "addr": None},
], ids=lambda b: repr(b)[:40])
def test_a_malformed_declaration_is_a_finding_not_ignored(tmp_path, bad):
    rtl, iss = logs("loadstore")
    if isinstance(bad, dict):
        bad = {k: (halt_addr(rtl) if v == "ADDR" else v) for k, v in bad.items()}
    c = compare(tmp_path, rtl, iss, bad)
    assert says_end_differs(c) and rejects_declaration(c), texts(c)


# ── a log cut at the halt on both sides: nothing to align, still verified ────

@pytest.mark.parametrize("prog", PROGRAMS)
def test_a_declaration_on_logs_both_cut_at_the_halt_store_is_accepted(tmp_path, prog):
    rtl, iss = logs(prog)
    n = len(rtl)
    c = compare(tmp_path, rtl, iss[:n], declared(halt_addr(rtl)))
    assert c["bugs"] == [], texts(c)


@pytest.mark.parametrize("prog", PROGRAMS)
def test_a_declaration_on_logs_cut_one_entry_short_of_the_halt_fails(tmp_path, prog):
    """The 5% length tolerance alone passes this on a long program (#178); the
    declaration's own check is what catches it."""
    rtl, iss = logs(prog)
    n = len(rtl)
    c = compare(tmp_path, rtl[:-1], iss[:n], declared(halt_addr(rtl)))
    assert rejects_declaration(c), texts(c)


# ── the Ibex harness passes the declaration, and takes the address from config ─

@pytest.fixture
def no_agent_d(monkeypatch):
    monkeypatch.setattr(harness, "agent_d", lambda *a, **k: {})


@pytest.mark.parametrize("prog", PROGRAMS)
def test_the_ibex_harness_declares_the_halt_write(no_agent_d, prog):
    rtl, iss = logs(prog)
    res = harness.compare_pair(rtl, iss)
    assert res["orchestrator"]["verdict"] == "pass", res["orchestrator"]
    pe = res["orchestrator"]["program_end"]
    assert pe and pe["end"] == "rtl_stopped_before_terminal_loop", pe
    assert pe["rtl_ended"]["reason"] == "halt_write"


@pytest.mark.parametrize("prog", PROGRAMS)
def test_the_harness_address_is_the_one_the_core_actually_stored_to(prog):
    rtl, _ = logs(prog)
    decl = getattr(harness, "IBEX_RTL_ENDED", None)
    assert decl is not None, "the harness has no declaration"
    assert int(decl["addr"], 0) == int(halt_addr(rtl), 0)


def test_the_harness_address_matches_ibexs_own_sim_control_registers():
    """Read from the Ibex tree the simulator was built from, when it is there."""
    header = REPO_ROOT / "work" / "ibex" / "examples" / "sw" / "simple_system" / "common" / "simple_system_regs.h"
    if not header.exists():
        pytest.skip("work/ibex is not cloned (validation/ibex/build_ibex.sh)")
    text = header.read_text(encoding="utf-8")
    base = int(re.search(r"#define SIM_CTRL_BASE\s+(0x[0-9a-fA-F]+)", text).group(1), 16)
    ctrl = int(re.search(r"#define SIM_CTRL_CTRL\s+(0x[0-9a-fA-F]+)", text).group(1), 16)
    assert int(harness.IBEX_RTL_ENDED["addr"], 0) == base + ctrl


def test_the_comparator_knows_no_ibex_address():
    """No sim-control address and no halt-store special case in the comparator."""
    src = (REPO_ROOT / "ava_patched.py").read_text(encoding="utf-8")
    assert not re.search(r"0x0*2000[0-9a-fA-F]\b|\bibex\b", src, re.I)
