"""The program ends at its terminal self-loop; Spike's tohost spin is not the program (#165).

Rule, from the issue: the program's end is the first entry that repeats its own
PC (a terminal self-loop, `j .`). Both logs are compared up to and including
that entry, and both must contain it at the same index. The repeats after it
are dropped and counted in the run metadata, never dropped silently.

Inputs are the recorded fixtures in tests/fixtures/spike/ (real gcc + Spike,
see PROVENANCE.md). The DUT side is the ISS trace in the DUT parser's shape
(pc / instr / rd / rd_val), i.e. the log a correct core prints.

  rv32im_smoke: aligned ISS log 4995 entries; self-loop 0x8000010c at index 67,
                then 4927 repeats
  csr_trap:     aligned ISS log 5016 entries; self-loop 0x80000058 at index 27,
                then 4988 repeats
"""
import asyncio
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import AGENT_F.coverage_pipeline as cp
from ava_patched import AVA, MismatchKind, SpikeISS

FIX = REPO_ROOT / "tests" / "fixtures" / "spike"

#: name -> (loop pc, loop index, trailing repeats), read off the recorded logs
PROGRAMS = {
    "rv32im_smoke": ("0x8000010c", 67, 4927),
    "csr_trap":     ("0x80000058", 27, 4988),
}


def _iss_log(name):
    log = cp.parse_spike_commit_log((FIX / f"{name}.log").read_text(encoding="utf-8"))
    aligned, reason = cp.align_to_entry(log, cp.elf_entry_pc(FIX / f"{name}.elf"))
    assert reason is None
    return aligned


def _dut(entries):
    """What the DUT parser yields for the same instructions."""
    return [{"pc": e["pc"], "instr": e["instr"], "rd": e["rd"], "rd_val": e["rd_val"]}
            for e in entries]


def _compare(tmp_path, rtl, iss):
    sim = SpikeISS(timeout=30, run_dir=tmp_path / "cmp")
    return asyncio.run(sim._compare_results(
        {"commit_log": rtl, "instructions": len(rtl)},
        {"commit_log": iss, "instructions": len(iss)}))


def _length_findings(comparison):
    return [b for b in comparison["bugs"] if b["kind"] == MismatchKind.INSTR_CNT.value]


@pytest.fixture(params=sorted(PROGRAMS))
def program(request):
    name = request.param
    pc, index, repeats = PROGRAMS[name]
    iss = _iss_log(name)
    assert iss[index]["pc"] == pc and iss[index + 1]["pc"] == pc, "fixture facts changed"
    assert len(iss) == index + 1 + repeats
    return name, pc, index, repeats, iss


# (a) / (d): the ISS log is truncated at the self-loop and the repeats are counted
def test_the_trailing_spin_is_dropped_and_counted(tmp_path, program):
    name, pc, index, repeats, iss = program
    comparison = _compare(tmp_path, _dut(iss[:index + 1]), iss)
    assert comparison.get("program_end") == {
        "pc": pc, "index": index,
        "iss_repeats_dropped": repeats, "rtl_repeats_dropped": 0,
    }


# (b) / (d): a DUT log equal to the ISS log through the self-loop passes
def test_a_correct_core_that_stops_at_the_self_loop_passes(tmp_path, program):
    name, pc, index, repeats, iss = program
    comparison = _compare(tmp_path, _dut(iss[:index + 1]), iss)
    assert comparison["bugs"] == []


def test_a_correct_core_that_spins_too_passes_and_its_repeats_are_counted(tmp_path, program):
    name, pc, index, repeats, iss = program
    comparison = _compare(tmp_path, _dut(iss[:index + 1 + 10]), iss)
    assert comparison["bugs"] == []
    assert comparison.get("program_end", {}).get("rtl_repeats_dropped") == 10


# (c) anti-vacuity: the rule must not make every log pass
@pytest.mark.parametrize("short_by", [1, 2, 20])
def test_a_core_that_stops_before_the_self_loop_fails_the_length_check(
        tmp_path, program, short_by):
    """Short by 1 or 2 is inside the comparator's 5% length tolerance on a
    68- or 28-entry program, so only the program-end rule can catch it."""
    name, pc, index, repeats, iss = program
    comparison = _compare(tmp_path, _dut(iss[:index + 1 - short_by]), iss)
    assert _length_findings(comparison), comparison["bugs"]


def test_a_core_that_reaches_the_self_loop_at_a_different_index_fails(tmp_path, program):
    name, pc, index, repeats, iss = program
    dut = _dut(iss[:index // 2] + iss[index // 2 + 1:index + 1])   # one instruction missing
    assert dut[-1]["pc"] == pc and len(dut) == index
    comparison = _compare(tmp_path, dut, iss)
    assert _length_findings(comparison), comparison["bugs"]


# run metadata: the counts reach VerificationResult.metadata
def test_the_counts_reach_the_run_metadata(tmp_path, monkeypatch):
    pc, index, repeats = PROGRAMS["rv32im_smoke"]
    dut = _dut(_iss_log("rv32im_smoke")[:index + 1])

    async def _rtl(self, tb_suite, semantic_map):
        return {"cycles": 100, "instructions": len(dut), "coverage_data": {},
                "performance": {"ipc": 0.7, "branch_predictions": 0, "branch_correct": 0},
                "commit_log": [dict(e) for e in dut], "state_snapshots": [], "seed": 1}

    async def _process(self, cmd, cwd, timeout_msg, launch_msg):
        return (types.SimpleNamespace(returncode=0), b"",
                (FIX / "rv32im_smoke.log").read_bytes())

    original = AVA._generate_tb_suite

    async def _tb_suite(self, *args, **kwargs):
        suite = await original(self, *args, **kwargs)
        suite["elf_path"] = str(FIX / "rv32im_smoke.elf")
        self.spike_iss.spike_binary = sys.executable     # "installed": the process is replaced
        return suite

    monkeypatch.setattr(SpikeISS, "_simulate_rtl", _rtl)
    monkeypatch.setattr(SpikeISS, "_run_simulator", _process)
    monkeypatch.setattr(AVA, "_generate_tb_suite", _tb_suite)
    ava = AVA(enable_llm=False, timeout=60, target_coverage=90.0,
              run_base_dir=str(tmp_path / "runs"), enable_database=False)
    result = asyncio.run(ava.generate_suite(
        rtl_spec="module riscv_core(input wire clk, input wire rst);\n"
                 "  reg [31:0] fetch_pc;\nendmodule\n",
        microarch="in_order", seed=42, save_results=False))
    assert result["initial_results"]["bugs"] == []
    assert result["initial_results"]["metadata"].get("program_end", {}).get(
        "iss_repeats_dropped") == repeats
