"""A DUT that exits non-zero after a matching commit log must not complete the run (#157).

Written from the issue and from the exit-code contract at the top of
ava_patched.py. A Verilator testbench exits non-zero on `$fatal`, a failed
immediate assertion, `$stop`, or when it cannot load its program. The exit
code alone does not say which, so whatever the DUT printed before failing is
not evidence of a clean run: the run "could not verify", `incomplete` / 3.
It is never `completed`.

The RTL simulator is a real child process. `_simulate_rtl` launches
`[sim_binary, "+seed=<n>", ...]` with `cwd=<run_dir>`; with `sim_binary`
set to this Python, the interpreter runs the script file named `+seed=<n>`
in the run dir, the same on Linux and Windows. It prints COMMIT lines that
match the ISS log exactly, so the exit code is the only thing that differs
between the two cases. Only Spike is stubbed (CI has none), and
`_generate_tb_suite` is wrapped only to say where the DUT is.
"""
import asyncio
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import ava_patched
from ava_patched import AVA, EXIT_INCOMPLETE, EXIT_PASS, SpikeISS

RTL = """
module riscv_core (
    input  wire        clk,
    input  wire        rst,
    input  wire [31:0] instr_in,
    output reg  [31:0] data_out
);
    reg [31:0] fetch_pc;
    reg [31:0] decode_instr;
endmodule
"""

PCS = [f"0x{0x80000000 + 4 * i:08x}" for i in range(20)]


def _run_with_dut(tmp_path, monkeypatch, exit_code):
    dut = "".join(f"print('COMMIT pc={pc} instr=0x00000013')\n" for pc in PCS)
    if exit_code:
        dut += ("import sys\n"
                "sys.stderr.write('%Error: core.sv:7: Assertion failed in TOP.riscv_core\\n')\n"
                f"sys.exit({exit_code})\n")
    original = AVA._generate_tb_suite

    async def _tb_suite(self, *args, **kwargs):
        suite = await original(self, *args, **kwargs)
        suite["sim_binary"] = sys.executable
        run_dir = Path(self.spike_iss._run_dir)          # the cwd _simulate_rtl uses
        (run_dir / f"+seed={int(suite.get('seed', 1))}").write_text(dut, encoding="utf-8")
        return suite

    async def _iss(self, semantic_map, stimulus, tb_suite):
        log = [{"pc": pc, "instr": "0x00000013"} for pc in PCS]
        return {"instructions": len(log), "commit_log": log, "seed": 1}

    monkeypatch.setattr(AVA, "_generate_tb_suite", _tb_suite)
    monkeypatch.setattr(SpikeISS, "_simulate_iss", _iss)
    ava = AVA(enable_llm=False, timeout=60, target_coverage=90.0,
              run_base_dir=str(tmp_path / f"exit{exit_code}"), enable_database=False)
    result = asyncio.run(ava.generate_suite(
        rtl_spec=RTL, microarch="in_order", seed=42, save_results=False))
    assert result["initial_results"]["bugs"] == [], "the logs must match, or this tests the comparator"
    return result


def test_a_dut_that_exits_0_completes(tmp_path, monkeypatch):
    """The control: the same harness can complete, so a non-completed run
    below can only have come from the exit code."""
    result = _run_with_dut(tmp_path, monkeypatch, 0)
    assert result["status"] == "completed"
    assert ava_patched.exit_code_for_status(result["status"]) == EXIT_PASS


@pytest.mark.parametrize("exit_code", [1, 2])
def test_a_dut_that_exits_non_zero_after_a_matching_log_is_incomplete(
        tmp_path, monkeypatch, exit_code):
    result = _run_with_dut(tmp_path, monkeypatch, exit_code)
    status = result["status"]
    assert (status, ava_patched.exit_code_for_status(status)) == ("incomplete", EXIT_INCOMPLETE)
    incomplete = result["extended_reports"][ava_patched.VERDICT_KEY]["incomplete"]
    assert any(label.startswith("rtl_simulation") and str(exit_code) in label
               for label in incomplete), incomplete
