"""Phase-4b: the board is DERIVED FROM THE RTL, not hand-written.

Every earlier demo used properties a human chose. This one reads the real
`ibex_alu.sv` and enumerates the obligations the design itself implies — one per
output port, plus structural checks — then lets the specialist organisation
discharge whatever it actually can.

Expect the board NOT to go green, and that is the point. Our four hand-written
proofs cover the comparison outputs and the ALU result for four op classes; the
generated board reveals the outputs that no checker has ever touched. A property
nobody wrote is a property nobody verified, and this makes that visible instead
of leaving it to be noticed in silicon.

    python run_voe_generated.py --mock
    python run_voe_generated.py --real
"""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "voe"))
sys.path.insert(0, os.path.join(HERE, "..", "phase3"))

from evidence_channels import FormalChannel, SimChannel, StaticChannel
from obligations import generate_obligations, describe
from specialists import PropertyClass, SemanticMemory, Specialist
from voe import VOE
from run_voe_ibex import IBEX_SBY, IBEX_SRC

RTL = os.path.join(HERE, "rtl", "ibex_alu.sv")

# Bind the checkers we have. Every output now has one — these harnesses were
# written specifically to close the gap this generated board exposed.
#
# `result_o` is bound with an explicit SCOPE: the checker proves it for the full
# RV32I opcode set, not for the RV32B opcodes (which this RV32BNone build does
# not implement). Partial coverage therefore emits a declared-only remainder
# obligation, so a checker that handles part of an output can never be mistaken
# for one that handles all of it.
HARNESS_MAP = {
    "out.comparison_result_o": "prove_cmp",
    "out.is_equal_result_o":   "prove_cmp",
    "out.adder_result_o":      "prove_adder",
    "out.adder_result_ext_o":  "prove_adder_ext",
    "out.imd_val_d_o":         "prove_imd",
    "out.imd_val_we_o":        "prove_imd",
    "out.result_o": {"task": "prove_result",
                     "scope": "all 16 RV32I opcodes",
                     "uncovered": "rv32b-opcodes"},
}

ORG = [
    ("structure", r"\.struct\.",        "skeptic",  "static structure",
     ["combinational loops", "unreachable logic"], "formal", True, 0.2),
    ("compare",   r"\.out\.(comparison|is_equal)", "explorer", "comparisons",
     ["signed/unsigned GTE boundary", "equality via adder"], "sim", False, 0.4),
    ("datapath",  r"\.out\.(result|adder)", "skeptic", "datapath results",
     ["per-opcode result mux", "sign extension"], "formal", True, 0.7),
    ("plumbing",  r"\.out\.(imd_val|unused)", "explorer", "internal plumbing",
     ["multicycle intermediate handoff"], "sim", False, 0.5),
]


def main():
    mock = "--mock" in sys.argv or "--real" not in sys.argv
    print("=== Phase-4b: obligations DERIVED FROM RTL (real ibex_alu) ===")
    print(f"    mode = {'MOCK (no toolchain)' if mock else 'REAL (verilator + sby + rtl_graph)'}\n")

    tasks, summary = generate_obligations(RTL, module="ibex_alu",
                                          harness_map=HARNESS_MAP)
    print(describe(summary))
    print()
    for t in tasks:
        mark = "checker" if t.has_evidence_path() else "NO CHECKER"
        print(f"    [{mark:10s}] {t.phi:34s} w={t.weight:<4} {t.note}")
    print()

    formal = FormalChannel(sby_file=IBEX_SBY, mock=mock, combinational=True,
                           negative_control="bug_logic")
    # tb_ibex_alu compares result_o against a golden and checks NOTHING else, so
    # its scope is declared: a pass may only be credited to result_o. Formal is
    # property-specific by construction; one shared testbench is not.
    sim = SimChannel(mock=mock, sources=IBEX_SRC, top="tb_ibex_alu",
                     defines_for=lambda bug: ["DUT=ibex_alu_mut"] if bug else ["DUT=ibex_alu"],
                     covers=r"\.out\.result_o$")
    static = StaticChannel(RTL, mock=mock)

    v = VOE(tasks, budget=80.0, mock=mock, formal=formal, sim=sim)
    v.workers = [
        Specialist(name, arch, v.k, formal, sim, PropertyClass(name, pat),
                   SemanticMemory(domain=dom, known_failure_modes=fm,
                                  preferred_method=pref, formal_first=ff,
                                  difficulty=diff),
                   static=static)
        for name, pat, arch, dom, fm, pref, ff, diff in ORG
    ]
    v.run(max_steps=120)

    print("\n  --- what this board says that a hand-written one could not ---")
    unver = v.board.unverifiable(v.ks)
    print(f"  obligations with NO checker: {len(unver)}")
    for phi in unver:
        print(f"    · {phi}")
    print("  These are real outputs of the real design that nothing in this")
    print("  project currently verifies. They keep contributing residual risk.")


if __name__ == "__main__":
    main()
