"""First STATEFUL DUT: the real cv32e40p_fifo.

Everything verified so far has been combinational, where a bounded check with
free inputs is already exhaustive. A FIFO has state, and that changes what
evidence *means*: bounded model checking can only say "no violation within N
cycles of reset", which is not a proof — the counter might still overflow at
cycle N+1.

This demo puts both on the same board so the difference is visible rather than
asserted:

    stage 1  bounded  (fifo_bmc.sby,   mode bmc,   depth 12)
             -> `bounded_pass`: lowers residual risk, NEVER discharges it
    stage 2  unbounded (fifo_prove.sby, mode prove, k-induction)
             -> `proved`: discharges the obligation for all time

The property that carries the point is `cnt <= DEPTH`. It is an inductive
invariant — true after reset and preserved by every transition, because a push
is blocked while `full_o` holds — so k-induction settles it and bounded checking
structurally cannot.

Both channels carry the same negative control (`bug_cnt`) against a mutated FIFO
whose `full_o` asserts one slot late, letting the counter reach 5.

    python run_voe_fifo.py --mock
    python run_voe_fifo.py --real
"""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "voe"))
sys.path.insert(0, os.path.join(HERE, "..", "phase3"))

from board import Task
from evidence_channels import FormalChannel, SimChannel
from specialists import PropertyClass, SemanticMemory, Specialist
from voe import VOE

FORMAL = os.path.join(HERE, "formal")
BMC_SBY   = os.path.join(FORMAL, "fifo_bmc.sby")
PROVE_SBY = os.path.join(FORMAL, "fifo_prove.sby")

TASKS = [
    Task("fifo.cnt_bound",  6.0, formal_task="cnt",      inject_bug=False,
         note="occupancy counter never exceeds DEPTH (inductive invariant)"),
    Task("fifo.flags",      4.0, formal_task="flags",    inject_bug=False,
         note="full_o/empty_o agree with the counter"),
    Task("fifo.no_overflow", 5.0, formal_task="overflow", inject_bug=False,
         note="a push against a full FIFO must not change occupancy"),
]


class StagedFormal:
    """Presents one sby *task family* through two channels of different strength.

    The board names a property ('cnt'); this resolves it to `bmc_cnt` or
    `prove_cnt` depending on which stage is active. Same property, same DUT,
    different warrant — which is the whole point of the demo.
    """

    def __init__(self, mock):
        # combinational=False: this DUT HAS state, so a bmc pass stays bounded.
        self.bmc_ch = FormalChannel(BMC_SBY, mock=mock, combinational=False,
                                    negative_control="bug_cnt")
        self.prove_ch = FormalChannel(PROVE_SBY, mock=mock, combinational=False,
                                      negative_control="bug_cnt")
        self.stage = "bmc"

    def _active(self):
        return self.bmc_ch if self.stage == "bmc" else self.prove_ch

    def prove(self, task):                       # channel interface
        return self._active().prove(f"{self.stage}_{task}")

    def gate_status(self):
        return self._active().gate_status()


def run_stage(stage, mock, tasks):
    formal = StagedFormal(mock)
    formal.stage = stage
    sim = SimChannel(mock=mock, covers=lambda phi: False)   # no TB for this DUT yet
    v = VOE(tasks, budget=60.0, mock=mock, formal=formal, sim=sim)
    mem = SemanticMemory(domain="fifo control",
                         known_failure_modes=["counter overflow past DEPTH",
                                              "flag/counter disagreement",
                                              "silent overwrite when full"],
                         preferred_method="formal", formal_first=True,
                         difficulty=0.7,
                         notes="state means bounded checks cannot close these")
    v.workers = [Specialist("fifo", "skeptic", v.k, formal, sim,
                            PropertyClass("fifo", r"^fifo\."), mem)]
    v.run(max_steps=40)
    return v


def main():
    mock = "--mock" in sys.argv or "--real" not in sys.argv
    print("=== First STATEFUL DUT: real cv32e40p_fifo (DEPTH=4) ===")
    print(f"    mode = {'MOCK (no toolchain)' if mock else 'REAL (sby + z3)'}")

    print("\n########## STAGE 1 — BOUNDED (mode bmc, depth 12) ##########")
    print("  a pass here means 'no violation within 12 cycles' — not a proof\n")
    v1 = run_stage("bmc", mock, [Task(**vars(t)) for t in TASKS])

    print("\n########## STAGE 2 — UNBOUNDED (mode prove, k-induction) ##########")
    print("  the same properties, established for all time\n")
    v2 = run_stage("prove", mock, [Task(**vars(t)) for t in TASKS])

    r1 = v1.k.R(v1.ks, v1.board.weights())
    r2 = v2.k.R(v2.ks, v2.board.weights())
    print("\n=== what the two stages establish ===")
    print(f"  bounded   : R = {r1:.3f}   proved = {[p for p in v1.board.tasks if v1.ks.proven(p)]}")
    print(f"  unbounded : R = {r2:.3f}   proved = {[p for p in v2.board.tasks if v2.ks.proven(p)]}")
    print("  Bounded evidence lowered risk but discharged nothing: on a design")
    print("  with state, 12 cycles of silence is not a guarantee. Only the")
    print("  induction proof closed the obligations.")


if __name__ == "__main__":
    main()
