"""Phase-2 VOE scaled to the REAL ibex_alu (RV32BNone).

The board now holds genuine per-op-class obligations over the unmodified corpus
RTL (`corpus/ibex_rtl/ibex_alu.sv`, vendored here with its package), plus one
bug-finding obligation over a narrowly-mutated copy. Two heterogeneous
engineers work it through the frozen kernel exactly as before — only the DUT and
evidence channels changed, which is the whole point: the logic is DUT-agnostic.

    python run_voe_ibex.py --mock     # pipeline + laws + reputation, no tools
    python run_voe_ibex.py --real     # real verilator + sby on ibex_alu

Expected (real): the four clean per-op proofs are discharged deductively, the
explorer's random simulation passes the mutant (the defect only fires on a
single operand value), and the skeptic's formal job returns the counterexample
— refuting that pass and ranking the engineers by evidence.
"""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "voe"))
sys.path.insert(0, os.path.join(HERE, "..", "phase3"))

from board import Task
from evidence_channels import FormalChannel, SimChannel
from voe import VOE

RTL    = os.path.join(HERE, "rtl")
FORMAL = os.path.join(HERE, "formal")
SIM    = os.path.join(HERE, "sim")

IBEX_SBY = os.path.join(FORMAL, "ibex_alu.sby")
IBEX_SRC = [os.path.join(RTL, "ibex_pkg.sv"),
            os.path.join(RTL, "ibex_alu.sv"),
            os.path.join(RTL, "ibex_alu_mut.sv"),
            os.path.join(SIM, "tb_ibex_alu.sv")]

TASKS = [
    Task("ibex_alu.add",   5.0, formal_task="prove_add",   inject_bug=False),
    Task("ibex_alu.logic", 5.0, formal_task="prove_logic", inject_bug=False),
    Task("ibex_alu.shift", 5.0, formal_task="prove_shift", inject_bug=False),
    Task("ibex_alu.cmp",   5.0, formal_task="prove_cmp",   inject_bug=False),
    Task("ibex_alu.logic[mut]", 5.0, formal_task="bug_logic", inject_bug=True),
]


def main():
    mock = "--mock" in sys.argv or "--real" not in sys.argv
    # ibex_alu is combinational -> a bmc-depth-1 pass is a complete deductive proof.
    formal = FormalChannel(sby_file=IBEX_SBY, mock=mock, combinational=True)
    sim = SimChannel(mock=mock, sources=IBEX_SRC, top="tb_ibex_alu",
                     defines_for=lambda bug: ["DUT=ibex_alu_mut"] if bug else ["DUT=ibex_alu"])
    print("=== Phase-2 VOE on REAL ibex_alu (2 engineers) ===")
    print(f"    mode = {'MOCK (no toolchain)' if mock else 'REAL (verilator + sby)'}\n")
    VOE(TASKS, budget=60.0, mock=mock, formal=formal, sim=sim).run(max_steps=120)


if __name__ == "__main__":
    main()
