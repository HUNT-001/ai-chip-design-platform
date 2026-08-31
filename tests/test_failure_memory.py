"""FAILURE MEMORY — permanent adversarial cases for everything that fooled us.

This suite is deliberately separate from `test_voe_kernel.py`, which tests what
the system is supposed to do. This one tests what previously *worked and was
wrong* — the configurations that produced confident, plausible, incorrect
results and were caught only afterwards.

An organisation gets stronger partly because it never forgets what fooled it.
Domain memory says "formal tends to close invariants cheaply". Failure memory
says "this measurement configuration previously produced false confidence" — and
for a system whose entire thesis is that claims require witnesses, the second is
the more valuable of the two.

Each test below names its incident. None of them is hypothetical; all eight
happened, in this order, and every one produced a green result before it was
found.

IMPORTANT FRAMING: no kernel or RTL defect has been EXPOSED by these
experiments. That is not the same as the kernel being correct — it has not yet
been falsified, and it should stay under attack while the layers above it change.
The kernel-law cases below exist for exactly that reason.
"""
import contextlib, importlib.util, io, os, sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "phase3"))
sys.path.insert(0, os.path.join(ROOT, "voe"))

from evidence_channels import (FormalChannel, SimChannel, Evidence,
                               verify_witness, _stamp)
from board import Task, TaskBoard, ResourceLedger
from evaluation import CampaignResult, Aggregate, promote_aggregate
from obligation_state import probe_structure, _has_datapath_multiply
from regime import RegimeBelief
from workers import Worker


def _kernel():
    spec = importlib.util.spec_from_file_location(
        "vsa_kernel", os.path.join(ROOT, "docs", "vsa_reference.py"))
    k = importlib.util.module_from_spec(spec)
    with contextlib.redirect_stdout(io.StringIO()):
        spec.loader.exec_module(k)
    return k


K = _kernel()


# --------------------------------------------------------------------------- #
# 1. VACUOUS PROOFS — sv2v stripped every assert; the board proved everything  #
# --------------------------------------------------------------------------- #
def test_incident_1_a_checker_that_cannot_fail_may_not_prove():
    """sv2v emits synthesizable Verilog and silently dropped assert/assume, so
    five properties 'proved' against a harness containing no assertions."""
    fc = FormalChannel(mock=True)                      # no negative control
    assert fc.prove("good").status == "gate_failed"
    assert not fc.gate_status()[0]


def test_incident_2_a_negative_control_must_actually_fail():
    """The mv_filter mutant did not break the property it was meant to break,
    so the control passed and the whole board became uncertifiable."""
    fc = FormalChannel(mock=True, negative_control="ctl")
    fc._run_task = lambda t: Evidence("formal", "proved", witness="w")
    assert fc.prove("good").status == "gate_failed"
    assert "must fail" in fc.gate_status()[1]


def test_incident_3_bounded_is_not_proved_on_a_stateful_design(tmp_path):
    """A bmc pass on a design WITH state is not a proof — the property may break
    one cycle past the horizon."""
    sby = tmp_path / "j.sby"
    sby.write_text("[options]\nmode bmc\ndepth 12\n")
    fc = FormalChannel(str(sby), mock=True, negative_control="bug_x")
    assert fc.prove("prove_x").status == "bounded_pass"


# --------------------------------------------------------------------------- #
# 4. FALSE REFUTATION — an off-by-one testbench 'found' bugs in correct RTL    #
# --------------------------------------------------------------------------- #
def test_incident_4_a_checker_that_always_fails_may_not_refute(monkeypatch):
    """The vacuity gate protected proofs; NOTHING protected refutations. A
    counterexample settles an obligation under Sem-1, so a broken testbench
    could close any property it liked — and did, twice."""
    sc = SimChannel(mock=True)
    monkeypatch.setattr(sc, "_run_uncached",
                        lambda bug, seed, nvec: Evidence("sim", "counterexample",
                                                         witness="log", n=nvec))
    assert sc.run(inject_bug=True, seed=1).status == "control_failed"


def test_incident_5_a_testbench_pass_is_not_evidence_for_what_it_never_checked():
    """One shared testbench compared result_o and nothing else, yet its pass was
    credited to imd_val_d_o — a property it never examined."""
    sc = SimChannel(mock=True, covers=r"\.out\.result_o$")
    assert not sc.covers("m.out.imd_val_d_o")
    ks = K.KnowledgeState()
    board = TaskBoard([Task("m.out.imd_val_d_o", 5.0, formal_task="prove_imd")])
    w = Worker("w", "explorer", K, FormalChannel(mock=True, negative_control="bug"), sc)
    ev, j = w.execute(ks, board, "m.out.imd_val_d_o", "sim")
    assert ev.status == "unsupported" and j is None


# --------------------------------------------------------------------------- #
# 6. METRIC GAMING — E rewarded shaving risk while proving nothing            #
# --------------------------------------------------------------------------- #
def test_incident_6_inductive_shaving_is_not_discharge():
    """A sim-heavy policy scored E=5.0 with ZERO obligations closed and ranked
    first, because simulation lowers R via n_eff without settling anything."""
    r = CampaignResult("p", "d", risk_before=20.0, risk_after=10.0, cost=2.0)
    r.closed_weight = 0.0
    assert r.shaving_efficiency == 5.0 and r.efficiency == 0.0


def test_incident_6b_the_same_conflation_recurred_in_the_belief_layer():
    """Fixed in the metric, then re-made one layer up: `closed = (gain > 0)`
    made every simulation pass look like a success, and the policy ran 162 sim
    passes in one campaign. Conceptual clarity did not prevent the repeat."""
    b = RegimeBelief(seed=1)
    sig = ("invariant", False, True)
    for _ in range(6):
        b.observe(sig, "sim", closed=False, phi="p1")
    al, be = b.posterior(sig, "sim", phi="p1")
    assert al / (al + be) < 0.2


# --------------------------------------------------------------------------- #
# 7. MISLABELLED FEATURES — a policy conditioned on facts that were not true   #
# --------------------------------------------------------------------------- #
def test_incident_7_comments_and_indices_are_not_arithmetic(tmp_path):
    """ibex_alu was labelled arithmetic (from `*` inside /* */ comments and from
    `i*4` index strides) and stateful (from always_comb) — it is neither."""
    f = tmp_path / "m.sv"
    f.write_text("/* stars * in * comments */\n"
                 "module m(input a, output logic b);\n"
                 "  always_comb b = ~a;\nendmodule\n")
    arith, seq, _ = probe_structure(str(f))
    assert not arith and not seq
    assert not _has_datapath_multiply("assign y = q[2*N*(seg+1)-1 : 2*N*seg];")
    assert _has_datapath_multiply("assign dst = srcA * srcB;")


# --------------------------------------------------------------------------- #
# 8. FALSE CONFIDENCE — a statistic that could not detect variance            #
# --------------------------------------------------------------------------- #
def test_incident_8_the_repeat_seed_must_reach_the_simulation():
    """`_seed` depended only on the worker NAME, so five repeats simulated
    identically and std=0.000 was true by construction. The statistic was not
    measuring variance — it was incapable of it, and a single hidden seed then
    flipped the H-vs-G verdict."""
    import zlib
    from policy import PolicyWorker, DIAGNOSTIC
    fc, sc = FormalChannel(mock=True), SimChannel(mock=True)
    a = PolicyWorker("x", DIAGNOSTIC, K, fc, sc, seed=1000)
    b = PolicyWorker("x", DIAGNOSTIC, K, fc, sc, seed=1001)
    assert a._seed != b._seed                     # repeats must actually differ
    again = PolicyWorker("x", DIAGNOSTIC, K, fc, sc, seed=1000)
    assert a._seed == again._seed                 # ...but stay reproducible
    assert Worker("n", "e", K, fc, sc)._seed == (zlib.crc32(b"n") % 1000) + 1


def test_incident_8b_a_margin_inside_the_noise_is_not_a_result():
    """+2.2% against a spread of 0.036 was reported as a win in one section and
    undecided in the next, because only one of them consulted the variance."""
    def agg(name, es):
        a = Aggregate(name, "held_out")
        for e in es:
            r = CampaignResult(name, "held_out", risk_before=e * 10, risk_after=0,
                               cost=10.0)
            r.closed_weight = e * 10; r.gate_armed = True; r.proofs = 1
            a.runs.append(r)
        return a
    noisy = agg("H", [1.32, 1.22, 1.30, 1.24, 1.31, 1.23, 1.29])
    steady = agg("G", [1.256] * 7)
    assert not promote_aggregate(noisy, steady, dev_designs=[]).accepted


# --------------------------------------------------------------------------- #
# Kernel laws — kept under attack, not assumed correct                        #
# --------------------------------------------------------------------------- #
def test_kernel_still_refuses_unwitnessed_knowledge():
    with pytest.raises(ValueError):
        K.Judgment("phi", K.Warrant.INDUCTIVE, {"n_eff": 9}, witness=None)


def test_kernel_still_refuses_risk_rising_without_a_commit():
    ks, props = K.KnowledgeState(), {"phi": 5.0}
    ks.believe(K.Judgment("phi", K.Warrant.DEDUCTIVE, {"n_eff": 0}, witness="pf"))
    prev = K.R(ks, props)
    del ks.K["phi"]                                # retraction
    assert K.R(ks, props) > prev
    assert all(v for _, v in K.check_laws(ks, props, prev, "commit")[1])
    assert not all(v for _, v in K.check_laws(ks, props, prev, "update")[1])


def test_a_tampered_witness_is_detected(tmp_path):
    art = tmp_path / "status"
    art.write_text("PASS")
    w = _stamp(str(art))
    art.write_text("PASS (edited)")
    ok, why = verify_witness(w)
    assert not ok and "CONTENT CHANGED" in why


# --------------------------------------------------------------------------- #
# Experiment 8 — four defects in the multi-step arm, each of which produced a  #
# confident number from machinery that was not running. Kept permanently.      #
# --------------------------------------------------------------------------- #
def _ms_worker(static=None):
    """A K-multistep worker with the yield layer attached, no campaign."""
    import policy as P
    from obligation_state import YieldModel
    w = P.PolicyWorker.__new__(P.PolicyWorker)
    w.policy, w.static = P.MULTISTEP, static
    w._probed, w._structprobed, w._probe_clean, w._simcount = set(), set(), set(), {}
    w._known_struct, w._struct = {}, {}
    w.features, w.yields = {}, YieldModel()
    w.sim = type("S", (), {"covers": staticmethod(lambda p: True)})()
    return w


class _Ledger:
    def can_afford(self, m):
        return True


def test_multistep_branch_is_reachable_without_a_belief_layer():
    """The branch was guarded on `hasattr(self, "belief")`, copied from the
    uncertainty-aware policy. A multistep worker has no posterior, so the guard
    was never true and the structural read never executed in ANY campaign —
    while the arm still reported +39% and was about to be promoted."""
    w = _ms_worker(static=object())
    assert w._pick_method_conditioned("phi_x", _Ledger()) == "probe"


def test_second_diagnostic_actually_fires_after_a_clean_probe():
    w = _ms_worker(static=object())
    L = _Ledger()
    assert w._pick_method_conditioned("phi_x", L) == "probe"
    w._probe_clean.add("phi_x")                    # probe came back clean
    assert w._pick_method_conditioned("phi_x", L) == "static", \
        "the chain must reach step 2; a gate that is never true is dead code"


def test_second_diagnostic_is_skipped_when_the_probe_found_a_bug():
    """A counterexample settles the obligation. Buying structure afterwards is
    overhead, not lookahead — the version that probed unconditionally lost 40%."""
    w = _ms_worker(static=object())
    L = _Ledger()
    w._pick_method_conditioned("phi_x", L)         # step 1
    assert "phi_x" not in w._probe_clean           # probe refuted it
    assert w._pick_method_conditioned("phi_x", L) != "static"


def test_structure_saying_formal_is_costly_does_not_licence_endless_sim():
    """`hard -> sim` with no cap is the 162-sim-pass loop returning: simulation
    raises n_eff and closes nothing, so an uncapped preference shaves risk
    forever. Structure may bias the ORDER of escalation, never remove it."""
    from obligation_state import ObligationFeatures
    w = _ms_worker(static=object())
    L = _Ledger()
    w._probed.add("phi_x"); w._structprobed.add("phi_x"); w._probe_clean.add("phi_x")
    w.features["phi_x"] = ObligationFeatures("invariant", True, False, "large")
    w._simcount["phi_x"] = w.policy.explore_budget
    assert w._pick_method_conditioned("phi_x", L) == "formal"


def test_a_diagnostic_action_must_not_be_charged_for_facts_already_held():
    """Both arms start structurally ignorant. The first version handed every
    policy the probed structure up front, so the multi-step arm paid 0.5 for
    information it already had and the experiment could not test its own
    hypothesis — it measured overhead and called the result a finding."""
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "voe_bench"))
    from run_multistep import unknown_features
    feats = unknown_features()
    assert feats and all(f.depth_class == "unknown" and not f.arithmetic
                         for f in feats.values())


def test_the_ablation_arm_actually_acquires_structure():
    """L-static-onestep is only a valid control if it really obtains the SAME
    information K does. An ablation whose treatment-minus-control difference is
    not the intended variable measures nothing — which is how Experiment 8's
    confound (a control that never read the RTL at all) arose in the first place."""
    import policy as P
    w = _ms_worker(static=object())
    w.policy = P.STATIC_ONE
    assert w._pick_method_conditioned("phi_x", _Ledger()) == "static"


def test_structural_information_is_not_double_charged():
    """K's extra diagnostic spend is already in the denominator of E. Adding it
    to the promotion bar as well would charge it twice and understate a real
    effect — the mirror of the metric-gaming defect, biased the other way."""
    from institutional_memory import promotion_verdict
    lenient, _ = promotion_verdict(0.061, 0.05, 0.029, 0.0)
    twice, _ = promotion_verdict(0.061, 0.05, 0.029, 0.025)
    assert not lenient and not twice      # this effect fails either way...
    generous, _ = promotion_verdict(0.090, 0.05, 0.029, 0.0)
    penalised, _ = promotion_verdict(0.090, 0.05, 0.029, 0.025)
    assert generous and not penalised     # ...but double-charging can flip one


def test_structure_is_bought_once_per_design_not_once_per_obligation():
    """The recurring defect, third occurrence. L paid 0.5 for a structural read
    on every obligation, but structure is a property of a DESIGN. Six of eleven
    reads on the full board were repeat purchases of a fact already held. The
    overhead amortised over 20 obligations and dominated over 7, so L won the
    full benchmark and LOST held-out — which looked exactly like overfitting and
    was not."""
    import policy as P
    w = _ms_worker(static=type("S", (), {"rtl_for": staticmethod(lambda p: "d.sv")})())
    w.policy = P.STATIC_CACHED
    L = _Ledger()
    assert w._pick_method_conditioned("phi_a", L) == "static"
    w.learn_structure("phi_a", True, False, "large")     # the read happens
    # a SIBLING obligation of the same design must not be charged again
    assert w._pick_method_conditioned("phi_b", L) != "static"
    assert w.features["phi_b"].arithmetic, "the cached fact must still be applied"


def test_uncached_variant_still_repeat_buys_so_the_contrast_is_real():
    """Guards the ablation itself: if L and M behaved identically, Experiment 11
    would be comparing a policy against itself and reporting the difference as
    an effect."""
    import policy as P
    w = _ms_worker(static=type("S", (), {"rtl_for": staticmethod(lambda p: "d.sv")})())
    w.policy = P.STATIC_ONE                              # no cache_structure
    L = _Ledger()
    w._pick_method_conditioned("phi_a", L)
    w.learn_structure("phi_a", True, False, "large")
    assert w._pick_method_conditioned("phi_b", L) == "static"


# --------------------------------------------------------------------------- #
# The default is an EVIDENCE-BACKED claim, not a preference. Guard it.        #
# --------------------------------------------------------------------------- #
def test_the_default_policy_is_the_one_the_evidence_promoted():
    """Defaults drift silently. This pins RECOMMENDED to the policy that won a
    pre-registered comparison on real tools, so changing it requires changing a
    test that names the experiment — not editing one line."""
    import policy as P
    assert P.RECOMMENDED is P.STATIC_CACHED
    assert P.RECOMMENDED.cache_structure and P.RECOMMENDED.structural_first
    assert not P.RECOMMENDED.uncertainty_aware, "H was rejected"
    assert not P.RECOMMENDED.multistep, "K was not promoted"


def test_the_promotion_is_recorded_with_its_held_out_caveat():
    """A promotion whose evidence is not written down is a preference wearing a
    number. The caveat matters as much as the verdict: the held-out margin was
    +0.9% on two design families."""
    import json, os
    led = os.path.join(os.path.dirname(__file__), "..", "voe_bench",
                       "capability_ledger.json")
    recs = json.load(open(led))
    promoted = [r for r in recs if r["treatment"] == "M-static-cached"]
    assert promoted and promoted[0]["decision"] == "PROMOTED"
    assert "not 'better everywhere'" in promoted[0]["complexity_note"].lower() \
        or "NOT 'better everywhere'" in promoted[0]["complexity_note"]
    assert any(r["treatment"] == "L-static-onestep" and r["decision"] == "REJECTED"
               for r in recs), "the rejected predecessor must stay on the record"


# --------------------------------------------------------------------------- #
# Experiment 12 — the grounded coupling probe. Five defects, all in the        #
# MEASURING APPARATUS, each of which made a broken instrument look green.      #
# These are source-level guards: the properties are structural, so a test can  #
# read the harness and refuse to let them regress.                             #
# --------------------------------------------------------------------------- #
import os as _os

_FV = _os.path.join(_os.path.dirname(__file__), "..", "voe_fifo", "formal",
                    "fifo_fv.v")
_SBY = _os.path.join(_os.path.dirname(__file__), "..", "voe_fifo", "formal",
                     "fifo_coupling.sby")


def _fv():
    return open(_FV).read()


def test_reference_model_does_not_read_the_signals_it_checks():
    """THE defect that mattered most. The shadow FIFO decided whether to accept
    a push from the DUT's own `full_o`. The mutant asserts full_o one slot late,
    so the model accepted the same illegal push, wrapped identically, corrupted
    the same slot — and reported agreement. The negative control detected
    NOTHING, and only appeared to work while unrelated modelling bugs happened
    to desync the two. A model gated on the signals under test is not a model."""
    src = _fv()
    body = src[src.index("`ifdef SHADOW"):src.index("`ifdef CLASS_DATA_INTEGRITY")]
    assert "push && !full" not in body and "pop && !empty" not in body
    assert "s_full" in body and "s_empty" in body, "must derive its own flags"


def test_the_lemma_task_does_not_assert_the_property_it_supports():
    """state_match once ran with -DCLASS_DATA_INTEGRITY too, so it failed on the
    integrity assertion rather than its own. A lemma that cannot be proved until
    the property it supports is already provable is circular, and the failure
    points at the wrong line entirely."""
    sby = open(_SBY).read()
    line = [l for l in sby.splitlines() if l.startswith("state_match:")][0]
    assert "CLASS_STATE_MATCH" in line
    assert "CLASS_DATA_INTEGRITY" not in line


def test_no_hierarchical_references_in_the_harness():
    """Neither yosys NOR sv2v resolves `dut.u_fifo.status_cnt_q`; both leave an
    implicitly-declared, UNDRIVEN wire of that literal name. The lemma was then
    ASSUMED against floating signals, which the solver satisfies by choosing
    their values — an assumption constraining nothing, invisible without a
    connectivity control. Internal state must be exposed as real ports."""
    src = _fv()
    assert "u_fifo." not in src.replace("cv32e40p_fifo.sv", "")
    assert "CLASS_TAP_CONTROL" in src, "the connectivity control must exist"


def test_shadow_storage_resets_like_the_dut():
    """cv32e40p_fifo does `mem_q <= '0` on reset. Leaving the shadow array
    uninitialised made the two memories differ from cycle 0 by construction, so
    no lemma over pointers could ever close the property."""
    src = _fv()
    body = src[src.index("`ifdef SHADOW"):src.index("`ifdef CLASS_DATA_INTEGRITY")]
    assert "shadow[i] <= 4'd0" in body


def test_shadow_memory_write_is_not_gated_by_flush():
    """The DUT keeps memory in a SEPARATE always block with no flush handling —
    gate_clock falls purely on `push_i && ~full_o`. Putting the shadow's write
    inside an `else if (flush)` chain made it skip writes the DUT performed."""
    src = _fv()
    body = src[src.index("`ifdef SHADOW"):src.index("`ifdef CLASS_DATA_INTEGRITY")]
    mem_blk = body[body.index("// MEMORY."):body.index("// POINTERS AND COUNT.")]
    # strip comments: the block's own explanation mentions flush, the CODE must not
    code = "\n".join(l.split("//")[0] for l in mem_blk.splitlines())
    assert "flush" not in code, "memory must not be gated by flush"
    assert "do_push" in code, "and must use the model's own acceptance signal"


def test_determinism_is_detected_by_distinct_outcomes_not_by_std_equality():
    """`std == 0.0` on a COMPUTED standard deviation is fragile: variance over
    twelve identical values still returns ~2.3e-16 of floating-point residue, so
    the exact test never fired and the warning it guarded stayed invisible. A
    silent guard is worse than no guard — it reads as 'checked' in the source."""
    import statistics
    identical = [1.1666666666666667] * 12
    assert statistics.pstdev(identical) != 0.0 or True   # may or may not be exact
    assert len({round(v, 12) for v in identical}) == 1, "distinctness is exact"


def test_a_deterministic_board_does_not_claim_statistical_replication():
    """Experiment 13's board is fully deterministic — every action is formal and
    memoised, and neither ordering uses randomness — so N campaigns are ONE
    campaign repeated N times. The committed noise gate is vacuous there, and the
    report must say so rather than let '12 seeds' imply replication."""
    import os
    src = open(os.path.join(os.path.dirname(__file__), "..", "voe_bench",
                            "run_coupled_real.py")).read()
    assert "NOT {seeds} independent samples" in src
    assert "distinct <= 2" in src, "determinism must be detected by distinctness"


def test_seed_variation_does_not_imply_outcome_variation():
    """Experiment 15's finding, kept so it is not rediscovered. Wiring a real
    testbench into a board does NOT make it stochastic: these benches are
    effective enough that 20k random vectors never change a verdict — true
    properties pass on every seed, the mutant is caught on every seed. A seed
    count is therefore not a sample count, and 'N seeds' must never be read as
    replication without checking that outcomes actually differ."""
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "voe_bench"))
    from run_benchmark import SimRouter
    r = SimRouter(True)
    for phi, bug in (("mvf.sticky", False), ("mvf.bug", True)):
        outs = {r.run(inject_bug=bug, seed=s, nvec=20000, phi=phi).status
                for s in (1, 7, 42, 999, 31337)}
        assert len(outs) == 1, f"{phi} unexpectedly varies: {outs}"


def test_an_experiment_refuses_to_report_when_its_premise_fails():
    """Experiment 15 stops before printing a verdict if the board did not become
    noisy. An experiment whose premise failed must not fall through to reporting
    E — that is how a number from a broken setup gets recorded as a finding."""
    import os
    src = open(os.path.join(os.path.dirname(__file__), "..", "voe_bench",
                            "run_coupled_noise.py")).read()
    body = src[src.index("c1 = distinct(m)"):]
    assert "Do not record a verdict" in body
    assert body.index("return") < body.index("prereg.decide"), \
        "the premise check must return BEFORE any verdict is computed"
