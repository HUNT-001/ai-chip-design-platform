"""Experiment 3 — obligation-level adaptive planning, against an oracle.

Experiment 2 established that allocation tracks the regime BETWEEN designs, and
that it cannot track it WITHIN one: on the multiplier board formal refuted one
obligation in seconds and timed out on the other, and a single per-channel rate
cannot represent that. Every policy came out ~6x off optimal.

So this board is deliberately heterogeneous IN ONE CAMPAIGN. Obligations are
drawn from three real designs at once, exactly as a subsystem would present
them, and each carries its own evidence channels:

    lfsr.*    invariants over shallow control logic   formal closes cheaply
    mvf.*     invariants over a small counter         formal closes cheaply
    mul.equiv equivalence over a 32x32 multiplier     formal TIMES OUT; nothing closes
    mul.bug   dense defect in that multiplier         simulation closes for 1

Compared:

    A-random / B-cheapest / C-engineer   fixed heuristics
    D-human-org                          the hand-designed incumbent
    E-adaptive                           design-level: one rate per CHANNEL
    F-obligation                         pi(a | Omega_i): rate per obligation
                                         SIGNATURE, so what is learned on one
                                         obligation transfers to a similar unseen one
    ORACLE                               retrospectively cheapest closing action

The question: does obligation-level conditioning approach the oracle while the
design-level learner cannot?

    python run_experiment3.py --mock
    python run_experiment3.py --real
"""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
for p in ("voe", "phase3"):
    sys.path.insert(0, os.path.join(HERE, "..", p))

from board import Task, ACTION_COST
from evidence_channels import FormalChannel, SimChannel
from evaluation import run_campaign, aggregate, compare_aggregates
from obligation_state import ObligationFeatures, features_for
from policy import POLICY_SET_V2, HUMAN_ORG, ADAPTIVE, OBLIGATION, PolicyWorker
from voe import VOE

REPEATS = 5
HELD = os.path.join(HERE, "..", "voe_heldout")
HOST = os.path.join(HERE, "..", "voe_hostile")

# ---- the three source designs, each with its own channels ------------------ #
DESIGNS = {
    "lfsr": dict(
        sby=os.path.join(HELD, "formal", "lfsr.sby"), control="bug_onehot",
        top="tb_lfsr", covers=r"lfsr\.(onehot|consistent)$",
        rtl=os.path.join(HELD, "rtl", "lfsr_8bit.sv"),
        src=[os.path.join(HELD, "rtl", f) for f in
             ("lfsr_8bit.sv", "lfsr_8bit_mut.sv", "lfsr_wrap.sv", "lfsr_wrap_mut.sv")]
            + [os.path.join(HELD, "sim", "tb_lfsr.sv")],
        wrap=("lfsr_wrap", "lfsr_wrap_mut"), seq=True, finds_bug=False),
    "mvf": dict(
        sby=os.path.join(HELD, "formal", "mvf.sby"), control="bug_sticky",
        top="tb_mvf", covers=r"mvf\.(sticky|clear)$",
        rtl=os.path.join(HELD, "rtl", "mv_filter.sv"),
        src=[os.path.join(HELD, "rtl", f) for f in
             ("mv_filter.sv", "mv_filter_mut.sv", "mvf_wrap.sv", "mvf_wrap_mut.sv")]
            + [os.path.join(HELD, "sim", "tb_mvf.sv")],
        wrap=("mvf_wrap", "mvf_wrap_mut"), seq=True, finds_bug=False),
    "mul": dict(
        sby=os.path.join(HOST, "formal", "mul.sby"), control="bug_equiv",
        top="tb_mul", covers=r"mul\.",
        rtl=os.path.join(HOST, "rtl", "mul_dut.sv"),
        src=[os.path.join(HOST, "rtl", "mul_dut.sv"),
             os.path.join(HOST, "sim", "tb_mul.sv")],
        wrap=("mul_wrap", "mul_wrap_mut"), seq=False, finds_bug=True,
        mock_timeouts=("prove_equiv",)),
}

TASKS = [
    Task("lfsr.onehot",     7.0, formal_task="prove_onehot"),
    Task("lfsr.consistent", 5.0, formal_task="prove_consistent"),
    Task("mvf.sticky",      6.0, formal_task="prove_sticky"),
    Task("mvf.clear",       4.0, formal_task="prove_clear"),
    Task("mul.equiv",       6.0, formal_task="prove_equiv", inject_bug=False),
    Task("mul.bug",         6.0, formal_task="bug_equiv",   inject_bug=True),
]
# which design each obligation belongs to, and what kind of property it is
OWNER = {"lfsr.onehot": "lfsr", "lfsr.consistent": "lfsr",
         "mvf.sticky": "mvf", "mvf.clear": "mvf",
         "mul.equiv": "mul", "mul.bug": "mul"}
KIND = {"lfsr.onehot": "invariant", "lfsr.consistent": "invariant",
        "mvf.sticky": "invariant", "mvf.clear": "invariant",
        "mul.equiv": "equivalence", "mul.bug": "equivalence"}
SBY_TASK_OWNER = {t.formal_task: OWNER[t.phi] for t in TASKS}


# ---- routers: dispatch each obligation to its own design's channels -------- #
class FormalRouter:
    def __init__(self, mock):
        self.ch = {n: FormalChannel(sby_file=d["sby"], mock=mock,
                                    combinational=(n == "mul"),
                                    negative_control=d["control"],
                                    mock_timeouts=d.get("mock_timeouts", ()))
                   for n, d in DESIGNS.items()}

    def prove(self, sby_task):
        return self.ch[SBY_TASK_OWNER[sby_task]].prove(sby_task)

    def gate_status(self):
        bad = [(n, c.gate_status()) for n, c in self.ch.items() if not c.gate_status()[0]]
        if bad:
            return False, f"{bad[0][0]}: {bad[0][1][1]}"
        return True, "every design's negative control failed as required"


class SimRouter:
    def __init__(self, mock):
        self.ch = {}
        for n, d in DESIGNS.items():
            g, m = d["wrap"]
            self.ch[n] = SimChannel(
                mock=mock, sources=d["src"], top=d["top"],
                defines_for=lambda bug, g=g, m=m: [f"DUT={m if bug else g}"],
                covers=d["covers"], mock_finds_bug=d["finds_bug"])

    def _for(self, phi):
        return self.ch[OWNER[phi]]

    def covers(self, phi):
        return phi in OWNER and self._for(phi).covers(phi)

    def run(self, inject_bug=False, seed=1, nvec=20000, phi=None):
        return self._for(phi).run(inject_bug=inject_bug, seed=seed, nvec=nvec)

    @property
    def _control_state(self):
        return None


def build_features(mock, verbose=False):
    """Omega_i for each obligation: declared property kind + PROBED structure.

    The structural half is read from the real RTL by `probe_structure` — whether
    the design contains multiplication, whether the property spans state, and a
    size band from the parsed signal count. An earlier version hard-coded these
    per design, which would have made the policy condition on a label I wrote
    rather than on the design itself, and nothing about that generalises.
    """
    from obligation_state import probe_structure
    feats = {}
    for t in TASKS:
        d = DESIGNS[OWNER[t.phi]]
        arith, seq, depth = probe_structure(d["rtl"])
        feats[t.phi] = ObligationFeatures(KIND[t.phi], arith, seq, depth)
        if verbose:
            print(f"    {t.phi:16s} Omega = {feats[t.phi].describe():28s}"
                  f" sig={feats[t.phi].signature()}")
    return feats


def build(policy, mock, budget, routers, seed):
    formal, sim = routers
    v = VOE([Task(**vars(t)) for t in TASKS], budget=budget, mock=mock,
            formal=formal, sim=sim)
    w = PolicyWorker(policy.name, policy, v.k, formal, sim, seed=seed)
    if policy.obligation_conditioned:
        w.attach_features(build_features(mock))
    v.workers = [w]
    return v


# ---- the oracle ------------------------------------------------------------ #
def oracle(routers, mock):
    """Retrospectively cheapest action that CLOSES each obligation.

    Not a policy — it is allowed to know the answer, which is exactly why it is
    the right upper reference. Anything unclosable is skipped for free, which is
    the decision no learner in this experiment reliably makes.
    """
    formal, sim = routers
    closed, cost, plan = 0.0, 0.0, {}
    for t in TASKS:
        options = []
        if sim.covers(t.phi):
            ev = sim.run(inject_bug=t.inject_bug, seed=7, nvec=20000, phi=t.phi)
            if ev.status == "counterexample":
                options.append(("sim", ACTION_COST["sim"]))
        ev = formal.prove(t.formal_task)
        if ev.status in ("proved", "counterexample"):
            options.append(("formal", ACTION_COST["formal"]))
        if options:
            m, c = min(options, key=lambda o: o[1])
            closed += t.weight; cost += c; plan[t.phi] = m
        else:
            plan[t.phi] = "SKIP (nothing closes it)"
    return (closed / cost if cost else 0.0), closed, cost, plan


def main():
    mock = "--mock" in sys.argv or "--real" not in sys.argv
    budget = 60.0
    print("=== Experiment 3: obligation-level planning vs an oracle ===")
    print(f"    one heterogeneous board, obligations from 3 real designs")
    print(f"    budget = {budget}   mode = {'MOCK' if mock else 'REAL'}\n")
    for t in TASKS:
        print(f"    {t.phi:16s} w={t.weight:<4} {KIND[t.phi]:12s} design={OWNER[t.phi]}")
    print()

    print("=== Omega_i — probed from the real RTL, not declared ===")
    build_features(mock, verbose=True)
    print()

    routers = (FormalRouter(mock), SimRouter(mock))
    o_E, o_closed, o_cost, o_plan = oracle(routers, mock)
    print("=== oracle (retrospectively optimal) ===")
    for phi, m in o_plan.items():
        print(f"    {phi:16s} -> {m}")
    print(f"    closed={o_closed:.1f}  cost={o_cost:.1f}  E_oracle={o_E:.3f}\n")

    results = []
    for p in POLICY_SET_V2:
        for i in range(REPEATS):
            results.append(run_campaign(build(p, mock, budget, routers, 1000 + i),
                                        "hetero", p.name))
        print(f"    {p.name:14s} done", flush=True)

    aggs = aggregate(results)
    print()
    print(compare_aggregates(aggs, incumbent=HUMAN_ORG.name))

    by = {a.policy: a for a in aggs}
    inc, des, obl = by[HUMAN_ORG.name], by[ADAPTIVE.name], by[OBLIGATION.name]
    print("\n=== the progression this experiment tests ===")
    print(f"  human heuristic     E={inc.mean_E:.3f}   ({inc.mean_E/o_E:5.1%} of oracle)")
    print(f"  design-adaptive     E={des.mean_E:.3f}   ({des.mean_E/o_E:5.1%} of oracle)")
    print(f"  obligation-adaptive E={obl.mean_E:.3f}   ({obl.mean_E/o_E:5.1%} of oracle)")
    print(f"  ORACLE              E={o_E:.3f}")
    if obl.mean_E > des.mean_E * 1.05:
        print("\n  Obligation-level conditioning closed part of the gap that")
        print("  design-level conditioning structurally could not. The learned")
        print("  quantity is per property-signature, so it transfers to an")
        print("  unseen obligation with similar structure rather than to a name.")
    else:
        print("\n  Obligation-level conditioning did NOT beat the design-level")
        print("  learner here. The features are too coarse, the board too small,")
        print("  or the signature does not carry the distinction that matters —")
        print("  a negative result about the representation, not the idea.")


if __name__ == "__main__":
    main()
