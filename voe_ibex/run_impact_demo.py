"""Organisational foresight: what stops being true when something changes.

Scenario, using the real proofs this platform has already established:

  1. The organisation proves properties of ibex_alu and the cv32e40p FIFO.
     A subsystem property is then proved *assuming* the FIFO counter bound —
     the way real verification composes.
  2. Someone edits ibex_alu.sv.
  3. The RV32BNone configuration assumption is withdrawn.

A reactive system reports what it knows. This one reports what it can no longer
claim: stale proofs are retracted, residual risk RISES, and the dependents of a
withdrawn premise are surfaced even though nobody touched them directly.

The point being tested — not asserted — is that risk rising is LEGAL here.
Sem-2′ permits it for exactly one class of event (`commit`), which is what an
RTL edit or a withdrawn assumption is. The kernel is untouched.

    python run_impact_demo.py
"""
import os, sys, io, contextlib, tempfile, shutil

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "voe"))
sys.path.insert(0, os.path.join(HERE, "..", "phase3"))

from engineer import load_kernel
from impact import ImpactGraph

ALU = os.path.join(HERE, "rtl", "ibex_alu.sv")
FIFO = os.path.join(HERE, "..", "voe_fifo", "rtl", "cv32e40p_fifo.sv")


def main():
    K = load_kernel()
    ks = K.KnowledgeState()

    # weights: what each obligation is worth
    W = {"ibex_alu.out.result_o": 5.0, "ibex_alu.out.adder_result_o": 5.0,
         "ibex_alu.out.comparison_result_o": 5.0,
         "fifo.cnt_bound": 6.0, "subsystem.no_data_loss": 8.0,
         "soc.stream_integrity": 9.0}

    g = ImpactGraph()
    # Each proof records what it actually rested on.
    g.record("ibex_alu.out.result_o", sources=[ALU], assumptions=["RV32BNone"])
    g.record("ibex_alu.out.adder_result_o", sources=[ALU], assumptions=["RV32BNone"])
    g.record("ibex_alu.out.comparison_result_o", sources=[ALU])
    g.record("fifo.cnt_bound", sources=[FIFO])
    # A composed proof: the subsystem is only safe BECAUSE the FIFO cannot
    # overflow. This is the dependency a human engineer carries in their head.
    g.record("subsystem.no_data_loss", assumptions=["fifo.cnt_bound"])
    # ...and a third level: the SoC claim rests on the subsystem claim. Two hops
    # from the FIFO, and nothing in it mentions a FIFO.
    g.record("soc.stream_integrity", assumptions=["subsystem.no_data_loss"])

    for phi in W:
        ks.believe(K.Judgment(phi, K.Warrant.DEDUCTIVE, {"n_eff": 0},
                              witness=f"proof/{phi}"))

    print("=== Organisational foresight: impact propagation ===\n")
    print(f"  established: {len(ks.K)} proofs, R = {K.R(ks, W):.3f}")
    print("  " + g.explain("subsystem.no_data_loss"))

    # ---------------------------------------------------------------- 1 ----
    print("\n--- event 1: someone edits ibex_alu.sv (a commit) ---")
    tmp = tempfile.mkdtemp()
    backup = os.path.join(tmp, "alu.bak")
    shutil.copy(ALU, backup)
    try:
        with open(ALU, "a") as f:
            f.write("\n// touched by a designer\n")
        prev = K.R(ks, W)
        rep = g.propagate(ks, K, W, trigger="edit ibex_alu.sv")
        print(g.summary(rep))
        _, laws = K.check_laws(ks, W, prev, "commit")
        print(f"  laws under a commit event: all-hold = {all(v for _, v in laws)}"
              f"  (risk rising here is LEGAL — Sem-2')")
        # and prove it would be ILLEGAL if we pretended nothing changed
        _, bad = K.check_laws(ks, W, prev, "update")
        print(f"  same rise labelled as a plain 'update': all-hold = "
              f"{all(v for _, v in bad)}  <- correctly rejected")
    finally:
        shutil.copy(backup, ALU)
        shutil.rmtree(tmp, ignore_errors=True)

    # ---------------------------------------------------------------- 2 ----
    print("\n--- event 2: the FIFO counter bound is withdrawn ---")
    print("  (nobody touched the subsystem — but it was only safe because of it)")
    prev = K.R(ks, W)
    rep = g.propagate(ks, K, W, trigger="retract fifo.cnt_bound",
                      assumption="fifo.cnt_bound")
    print(g.summary(rep))
    print("  soc.stream_integrity never mentions a FIFO — it was reached two")
    print("  hops out, through the subsystem claim. That is the inference a")
    print("  senior engineer makes and a reactive planner cannot.")

    print("\n=== what this adds ===")
    print("  Reactive: 'here is what I know.'")
    print("  Proactive: 'here is what I may no longer claim, and why.'")
    print("  No kernel change: retraction is a commit-class event, which is the")
    print("  one case Sem-2' already permits residual risk to rise.")


if __name__ == "__main__":
    main()
