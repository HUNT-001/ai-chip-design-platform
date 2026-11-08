"""#137: a store that differs between RTL and ISS must be a comparator finding.

The canonical commit-log schema carries stores as `mem_writes`
([{addr, size, data}]).  AGENT_D reports a differing store as MEMMISMATCH;
the orchestrator's CommitLogComparator must agree, and the run verdict must
follow (failed / 1).  The logs below differ in nothing but `mem_writes`.
"""
import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import ava_patched
from ava_patched import AVA, CommitLogComparator, SpikeISS
from AGENT_D.compare_commitlogs import CommitEntry, CompareConfig, _FieldComparator

N = 20
RTL_SRC = """\
module demo_core (
    input  wire        clk, rst_n,
    input  wire [31:0] instr_i,
    output reg  [31:0] data_o
);
    reg [31:0] pc;
endmodule
"""


def _entry(i, data="0x0000002a", addr="0x80001000"):
    return {"pc": f"0x{0x80000000 + 4 * i:08x}", "instr": "0x00a12023",
            "regs": {}, "csrs": {},
            "mem_writes": [{"addr": addr, "size": 4, "data": data}]}


def _logs(corrupt_data=None, corrupt_addr=None):
    iss = [_entry(i) for i in range(N)]
    rtl = [_entry(i) for i in range(N)]
    if corrupt_data:
        rtl[7] = _entry(7, data=corrupt_data)
    if corrupt_addr:
        rtl[7] = _entry(7, addr=corrupt_addr)
    return rtl, iss


def test_identical_stores_are_clean():
    rtl, iss = _logs()
    assert CommitLogComparator().compare(rtl, iss) == []


def test_differing_store_data_is_a_finding():
    rtl, iss = _logs(corrupt_data="0xbadbad00")
    bugs = CommitLogComparator().compare(rtl, iss)
    assert [b.kind for b in bugs] == [ava_patched.MismatchKind.MEMORY], bugs


def test_differing_store_address_is_a_finding():
    rtl, iss = _logs(corrupt_addr="0x80002000")
    bugs = CommitLogComparator().compare(rtl, iss)
    assert [b.kind for b in bugs] == [ava_patched.MismatchKind.MEMORY], bugs


def test_a_store_reported_by_only_one_side_is_a_finding():
    """RTL reports a store, ISS has no `mem_writes` key: nothing was compared,
    which must not read as a clean match."""
    rtl, iss = _logs()
    del iss[7]["mem_writes"]
    bugs = CommitLogComparator().compare(rtl, iss)
    assert [b.kind for b in bugs] == [ava_patched.MismatchKind.MEMORY], bugs
    assert "one side did not report stores" in bugs[0].description


def test_a_store_reported_only_by_the_iss_is_a_finding():
    rtl, iss = _logs()
    del rtl[7]["mem_writes"]
    bugs = CommitLogComparator().compare(rtl, iss)
    assert [b.kind for b in bugs] == [ava_patched.MismatchKind.MEMORY], bugs


def test_both_sides_without_the_key_stay_clean():
    rtl, iss = _logs()
    for e in rtl + iss:
        del e["mem_writes"]
    assert CommitLogComparator().compare(rtl, iss) == []


def test_a_one_sided_empty_list_is_not_a_store():
    rtl, iss = _logs()
    del iss[7]["mem_writes"]
    rtl[7]["mem_writes"] = []
    assert CommitLogComparator().compare(rtl, iss) == []


def test_a_malformed_store_entry_is_a_mismatch_not_a_skip():
    rtl, iss = _logs()
    rtl[7]["mem_writes"] = ["not-a-dict"]
    iss[7]["mem_writes"] = []
    bugs = CommitLogComparator().compare(rtl, iss)
    assert [b.kind for b in bugs] == [ava_patched.MismatchKind.MEMORY], bugs


def test_a_malformed_entry_against_a_well_formed_one_is_a_mismatch():
    rtl, iss = _logs()
    rtl[7]["mem_writes"] = [rtl[7]["mem_writes"][0], 42]
    bugs = CommitLogComparator().compare(rtl, iss)
    assert [b.kind for b in bugs] == [ava_patched.MismatchKind.MEMORY], bugs


def test_the_orchestrator_agrees_with_agent_d_on_the_same_entries():
    rtl, iss = _logs(corrupt_data="0xbadbad00")
    base = dict(schema_version="2.1.0", hart=0, seq=7, priv="M", trap=None)
    a = CommitEntry.from_dict(dict(base, **rtl[7]), xlen=32, lineno=1, source="r")
    b = CommitEntry.from_dict(dict(base, **iss[7]), xlen=32, lineno=1, source="i")
    agent_d = _FieldComparator(CompareConfig()).compare(a, b)
    assert agent_d, "control: AGENT_D must see this divergence, or the test is vacuous"
    assert CommitLogComparator().compare(rtl, iss), (
        "AGENT_D reports the store mismatch; the orchestrator's comparator did not")


def test_the_run_verdict_follows_a_corrupted_store(tmp_path, monkeypatch):
    def run(rtl_log, iss_log, label):
        async def _rtl(self, tb_suite, semantic_map):
            return {"cycles": 1000, "instructions": N,
                    "coverage_data": {"lines_hit": 196, "total_lines": 200,
                                      "branches_hit": 94, "total_branches": 100},
                    "performance": {"ipc": 0.9, "branch_predictions": 100,
                                    "branch_correct": 95},
                    "commit_log": [dict(e) for e in rtl_log],
                    "state_snapshots": [], "seed": 1}

        async def _iss(self, semantic_map, stimulus, tb_suite):
            return {"instructions": N, "commit_log": [dict(e) for e in iss_log],
                    "seed": 1}
        monkeypatch.setattr(SpikeISS, "_simulate_rtl", _rtl)
        monkeypatch.setattr(SpikeISS, "_simulate_iss", _iss)
        ava = AVA(enable_llm=False, timeout=30, target_coverage=90.0,
                  run_base_dir=str(tmp_path / label), enable_database=False)
        return asyncio.run(ava.generate_suite(
            rtl_spec=RTL_SRC, microarch="in_order", seed=42, save_results=False))

    clean = run(*_logs(), "clean")
    bad = run(*_logs(corrupt_data="0xbadbad00"), "bad")
    assert clean["status"] == "completed"
    assert bad["status"] == "failed", bad["status"]
    assert ava_patched.exit_code_for_status(bad["status"]) == 1
