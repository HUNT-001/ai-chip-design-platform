"""The diverse-regime benchmark — every design built so far, in one board.

Experiment 4A could not test diagnostic planning, because its board contained a
single ambiguous class. The requirement that came out of it was not "more
designs" but **causal diversity**: signature classes whose MEMBERS genuinely
differ, so that structure alone cannot tell you which action will pay.

That requirement is satisfiable from what already exists. Every design here has
both a correct variant and a mutant, so each class contains

    TRUE properties   -> need a proof, which may be cheap (control logic) or
                         out of reach (a 32x32 multiply)
    FALSE properties  -> need a counterexample, which is usually cheap

Structure cannot separate those two: they share RTL, property kind, and
sequential character. Only evidence separates them — which is precisely the
ambiguity a cheap diagnostic probe exists to resolve, and precisely what the
earlier boards lacked.

Six designs, six verification regimes:

    toy_alu    combinational equivalence, narrow injected defect (sim misses it)
    ibex_alu   combinational equivalence over real RISC-V ALU logic
    fifo       deeply sequential invariants; NO testbench, formal-only
    lfsr       sequential invariants over shallow control logic
    mv_filter  sequential invariants over a saturating counter
    multiplier arithmetic equivalence; formal times out
"""
from __future__ import annotations
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _p(*a):
    return os.path.join(ROOT, *a)


# name -> everything needed to run evidence for that design
DESIGNS = {
    "toy_alu": dict(
        sby=_p("phase3", "formal", "alu.sby"), control="buggy", combinational=True,
        rtl=_p("phase3", "rtl", "alu.sv"),
        src=[_p("phase3", "rtl", "alu.sv"), _p("phase3", "sim", "tb_alu.sv")],
        top="tb_alu", covers=r"^toy_alu\.", wrap=None,
        defines=lambda bug: (["INJECT_BUG=1"] if bug else []),
        # the toy defect fires on ONE input value in 2^32 — random vectors
        # essentially never reach it. Modelled as such in mock.
        finds_bug=False),
    "ibex_alu": dict(
        sby=_p("voe_ibex", "formal", "ibex_alu.sby"), control="bug_logic",
        combinational=True, rtl=_p("voe_ibex", "rtl", "ibex_alu.sv"),
        src=[_p("voe_ibex", "rtl", f) for f in
             ("ibex_pkg.sv", "ibex_alu.sv", "ibex_alu_mut.sv")]
            + [_p("voe_ibex", "sim", "tb_ibex_alu.sv")],
        top="tb_ibex_alu", covers=r"^ibex\.result", wrap=None,
        defines=lambda bug: [f"DUT={'ibex_alu_mut' if bug else 'ibex_alu'}"],
        finds_bug=False),
    "fifo": dict(
        sby=_p("voe_fifo", "formal", "fifo_prove.sby"), control="bug_cnt",
        combinational=False, rtl=_p("voe_fifo", "rtl", "cv32e40p_fifo.sv"),
        src=None, top=None, covers=None, wrap=None, defines=None,
        finds_bug=False),          # no testbench exists: formal-only regime
    "lfsr": dict(
        sby=_p("voe_heldout", "formal", "lfsr.sby"), control="bug_onehot",
        combinational=False, rtl=_p("voe_heldout", "rtl", "lfsr_8bit.sv"),
        src=[_p("voe_heldout", "rtl", f) for f in
             ("lfsr_8bit.sv", "lfsr_8bit_mut.sv", "lfsr_wrap.sv", "lfsr_wrap_mut.sv")]
            + [_p("voe_heldout", "sim", "tb_lfsr.sv")],
        top="tb_lfsr", covers=r"^lfsr\.(onehot|consistent|bug)",
        wrap=("lfsr_wrap", "lfsr_wrap_mut"), defines=None, finds_bug=True),
    "mv_filter": dict(
        sby=_p("voe_heldout", "formal", "mvf.sby"), control="bug_sticky",
        combinational=False, rtl=_p("voe_heldout", "rtl", "mv_filter.sv"),
        src=[_p("voe_heldout", "rtl", f) for f in
             ("mv_filter.sv", "mv_filter_mut.sv", "mvf_wrap.sv", "mvf_wrap_mut.sv")]
            + [_p("voe_heldout", "sim", "tb_mvf.sv")],
        top="tb_mvf", covers=r"^mvf\.(sticky|clear|bug)",
        wrap=("mvf_wrap", "mvf_wrap_mut"), defines=None, finds_bug=True),
    "multiplier": dict(
        sby=_p("voe_hostile", "formal", "mul.sby"), control="bug_equiv",
        combinational=True, rtl=_p("voe_hostile", "rtl", "mul_dut.sv"),
        src=[_p("voe_hostile", "rtl", "mul_dut.sv"),
             _p("voe_hostile", "sim", "tb_mul.sv")],
        top="tb_mul", covers=r"^mul\.", wrap=("mul_wrap", "mul_wrap_mut"),
        defines=None, finds_bug=True, mock_timeouts=("prove_equiv",)),
}


# (phi, weight, sby_task, design, property-kind, is_mutant_obligation)
# Each class deliberately mixes TRUE properties with mutant-refuted FALSE ones.
OBLIGATIONS = [
    # --- combinational equivalence, no arithmetic --------------------------- #
    ("ibex.result_rv32i", 7.0, "prove_result",    "ibex_alu",  "equivalence", False),
    ("ibex.adder",        5.0, "prove_adder",     "ibex_alu",  "equivalence", False),
    ("ibex.adder_ext",    3.0, "prove_adder_ext", "ibex_alu",  "equivalence", False),
    ("ibex.imd_tieoff",   3.0, "prove_imd",       "ibex_alu",  "invariant",   False),
    ("ibex.logic_bug",    6.0, "bug_logic",       "ibex_alu",  "equivalence", True),
    ("toy_alu.equiv",     4.0, "good",            "toy_alu",   "equivalence", False),
    ("toy_alu.bug",       4.0, "buggy",           "toy_alu",   "equivalence", True),
    # --- arithmetic equivalence (formal out of reach) ----------------------- #
    ("mul.equiv",         6.0, "prove_equiv",     "multiplier", "equivalence", False),
    ("mul.bug",           6.0, "bug_equiv",       "multiplier", "equivalence", True),
    # --- sequential invariants ---------------------------------------------- #
    ("lfsr.onehot",       7.0, "prove_onehot",    "lfsr",      "invariant",   False),
    ("lfsr.consistent",   5.0, "prove_consistent","lfsr",      "invariant",   False),
    ("lfsr.stable",       4.0, "prove_stable",    "lfsr",      "invariant",   False),
    ("lfsr.bug",          6.0, "bug_onehot",      "lfsr",      "invariant",   True),
    ("mvf.sticky",        6.0, "prove_sticky",    "mv_filter", "invariant",   False),
    ("mvf.clear",         4.0, "prove_clear",     "mv_filter", "invariant",   False),
    ("mvf.bug",           6.0, "bug_sticky",      "mv_filter", "invariant",   True),
    # --- deeply sequential, formal-only (no testbench exists) --------------- #
    ("fifo.cnt_bound",    7.0, "prove_cnt",       "fifo",      "invariant",   False),
    ("fifo.flags",        4.0, "prove_flags",     "fifo",      "invariant",   False),
    ("fifo.no_overflow",  5.0, "prove_overflow",  "fifo",      "invariant",   False),
    ("fifo.bug",          6.0, "bug_cnt",         "fifo",      "invariant",   True),
]

OWNER = {o[0]: o[3] for o in OBLIGATIONS}
KIND = {o[0]: o[4] for o in OBLIGATIONS}
IS_MUTANT = {o[0]: o[5] for o in OBLIGATIONS}
SBY_TASK = {o[0]: o[2] for o in OBLIGATIONS}


def summary():
    from collections import Counter
    c = Counter(OWNER.values())
    t = sum(1 for v in IS_MUTANT.values() if not v)
    f = sum(1 for v in IS_MUTANT.values() if v)
    return (f"{len(OBLIGATIONS)} obligations across {len(c)} designs "
            f"({t} true properties, {f} mutant-refuted)")
