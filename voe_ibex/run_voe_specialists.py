"""Phase-4 demo: a specialised verification ORGANISATION on the real ibex_alu.

Four domain specialists, each owning a region of the failure space plus the
schemas for that domain, crossed with cognitive archetypes:

    arith    (add/sub)     explorer  — cheap to sample, well-understood
    bitwise  (and/or/xor)  skeptic   — owns the mutant property too
    shift    (sll/srl/sra) skeptic   — formal-first; see its M_s below
    compare  (lt/ge/eq)    explorer  — signed/unsigned boundary cases

Specialisation is **state ownership**, not a job title: a specialist may only
bid on properties inside its class, and any property nobody owns is reported as
a coverage gap rather than silently skipped.

The shift specialist's semantic memory carries a real lesson learned on this
very DUT: a Verilog signedness demotion turned an arithmetic shift into a
logical one in the *reference model*, invisible to 20,000 random vectors because
the simulation checker modelled it differently and was accidentally correct.
That experience makes it go formal-first. Note what this does and does not do —
it changes the PLAN, never the RISK. Memory cannot certify (attack sheet 2.3).

    python run_voe_specialists.py --mock
    python run_voe_specialists.py --real
"""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "voe"))
sys.path.insert(0, os.path.join(HERE, "..", "phase3"))

from board import Task
from evidence_channels import FormalChannel, SimChannel
from specialists import PropertyClass, SemanticMemory, Specialist
from voe import VOE
from run_voe_ibex import TASKS, IBEX_SBY, IBEX_SRC


def build_org(kern_holder, formal, sim):
    """The organisation: (property class, semantic memory, archetype) triples."""
    specs = [
        ("arith", r"\.add$", "explorer", SemanticMemory(
            domain="integer arithmetic",
            known_failure_modes=["carry/borrow at word boundary",
                                 "signed overflow wrap"],
            preferred_method="sim", difficulty=0.3,
            notes="adder is the most exercised path; random sampling is cheap")),
        ("bitwise", r"\.logic(\[mut\])?$", "skeptic", SemanticMemory(
            domain="bitwise logic",
            known_failure_modes=["operand-negate mux (RV32B ANDN/ORN/XNOR)",
                                 "narrow input-specific corruption"],
            preferred_method="formal", difficulty=0.5,
            notes="defects here can be a single input value — sampling misses them")),
        ("shift", r"\.shift$", "skeptic", SemanticMemory(
            domain="shifts",
            known_failure_modes=[
                "REFERENCE-MODEL signedness demotion: in a Verilog ternary, one "
                "unsigned branch makes the whole expression unsigned and turns "
                "$signed(a)>>>amt into a LOGICAL shift (found on this DUT)",
                "shift amount masking (b[4:0]) vs full-width",
                "arithmetic shift sign extension at amt=0 and amt=31"],
            preferred_method="formal", formal_first=True, difficulty=0.8,
            notes="checker bugs here are invisible to random sim; prove early")),
        ("compare", r"\.cmp$", "explorer", SemanticMemory(
            domain="comparisons",
            known_failure_modes=["signed vs unsigned GTE at the sign boundary",
                                 "equality via adder result == 0"],
            preferred_method="sim", difficulty=0.4,
            notes="boundary cases cluster at 0x7FFFFFFF/0x80000000")),
    ]
    return [Specialist(name, arch, kern_holder, formal, sim,
                       PropertyClass(name, pat), mem)
            for name, pat, arch, mem in specs]


def main():
    mock = "--mock" in sys.argv or "--real" not in sys.argv
    formal = FormalChannel(sby_file=IBEX_SBY, mock=mock, combinational=True,
                           negative_control="bug_logic")
    sim = SimChannel(mock=mock, sources=IBEX_SRC, top="tb_ibex_alu",
                     defines_for=lambda bug: ["DUT=ibex_alu_mut"] if bug else ["DUT=ibex_alu"])

    print("=== Phase-4 specialised organisation on REAL ibex_alu ===")
    print(f"    mode = {'MOCK (no toolchain)' if mock else 'REAL (verilator + sby)'}")

    v = VOE(TASKS, budget=70.0, mock=mock, formal=formal, sim=sim)
    v.workers = build_org(v.k, formal, sim)      # replace the generalist roster
    print("    organisation:")
    for s in v.workers:
        print(f"      {s.name:8s} [{s.archetype:8s}] owns /{s.prop_class.pattern}/"
              f"  M_s: {s.memory.explain()}")
    print()
    v.run(max_steps=120)


if __name__ == "__main__":
    main()
