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


# --------------------------------------------------------------------------- #
# Experiment 15 -> the ELIGIBILITY gate. Every other gate asks whether a RESULT #
# is trustworthy; this asks whether the environment can OBSERVE the phenomenon. #
# --------------------------------------------------------------------------- #
def test_a_stochastic_experiment_is_refused_on_a_deterministic_board():
    """The gate Experiment 15 produced. Its board had real tools, a real
    testbench, and every control armed — and still could not answer, because
    seed variation had nothing to act on. No sample size fixes that."""
    from eligibility import STOCHASTIC, admit
    e = admit("X", STOCHASTIC, distinct_outcomes=1)
    assert not e.eligible
    assert any("no stochastic treatment effect" in r for r in e.reasons)
    assert admit("X", STOCHASTIC, distinct_outcomes=4).eligible


def test_not_evaluable_is_not_not_met():
    """Two different claims. NOT MET: the effect is absent or too small. NOT
    EVALUABLE: the question was never asked. Collapsing them records a benchmark
    mismatch as a scientific finding."""
    from eligibility import NOT_EVALUABLE
    assert NOT_EVALUABLE == "NOT EVALUABLE"
    assert NOT_EVALUABLE not in ("NOT MET", "UNDERPOWERED", "MET")


def test_a_stochastic_experiment_must_measure_variance_not_assume_it():
    """Declaring a board stochastic is not evidence that it is."""
    from eligibility import STOCHASTIC, admit
    e = admit("X", STOCHASTIC)                      # nothing measured
    assert not e.eligible
    assert any("must MEASURE outcome variance" in r for r in e.reasons)


def test_seed_count_is_not_a_sample_count_on_a_deterministic_board():
    """Experiments 13 and 14 each ran ONE campaign N times. Their gains were
    exact arithmetic, not estimates, and their noise criteria were vacuous."""
    from eligibility import seeds_are_replication
    assert not seeds_are_replication(1)
    assert seeds_are_replication(2)


def test_the_eligibility_gate_runs_before_any_campaign():
    """Checking afterwards still produces a number someone may quote. Experiment
    15 must return before a single campaign executes."""
    import os
    src = open(os.path.join(os.path.dirname(__file__), "..", "voe_bench",
                            "run_coupled_noise.py")).read()
    assert src.index("elig = admit(") < src.index("run_campaign(")
    assert "NOT EVALUABLE" in src


# --------------------------------------------------------------------------- #
# The stochastic benchmark. Four defects, and the fourth is the recurring one. #
# --------------------------------------------------------------------------- #
def test_operands_that_must_be_independent_are_not_consecutive_urandom_calls():
    """MEASURED: two consecutive $urandom() calls in Verilator are correlated.
    The marginals were exactly uniform (1 in 255, 1 in 256) while the joint
    corner ran 2x high — 25 observed against 12.2 expected, +3.6 sigma. Neither
    number alone could reveal it; only measuring both did. Separated bit lanes
    of ONE draw brought it to +1.35 sigma."""
    import os
    tb = open(os.path.join(os.path.dirname(__file__), "..", "voe_stoch",
                           "sim", "tb_satmac.sv")).read()
    body = "\n".join(l.split("//")[0] for l in tb.splitlines())
    # The invariant is about the OPERANDS, which must be mutually independent.
    # `clr` keeps its own draw: it is a separate control signal, and it cannot
    # mask a detection because the checker compares every cycle, so a corner
    # mismatch is caught on the corner cycle itself. (Its draw IS consecutive
    # with the operand draw, so clr and the operands may correlate — a plausible
    # contributor to the residual +1.35 sigma, and harmless here only because
    # clr does not affect whether the corner occurs.)
    assert "a   = r[7:0]" in body and "b   = r[23:16]" in body, \
        "operands must come from separated lanes of ONE draw"
    assert "a   = $urandom()" not in body and "b   = $urandom()" not in body


def test_a_measurement_is_not_gated_on_the_statistic_it_validates():
    """The fourth instance of 'a control that cannot observe'. The stimulus
    characterisation ran only when P(detect) looked wrong, so the benchmark's
    headline property went unmeasured whenever the derived statistic happened to
    look fine. A check that fires only on suspicion never confirms the healthy
    case — it can only ever fail to fire."""
    import os
    src = open(os.path.join(os.path.dirname(__file__), "..", "voe_stoch",
                            "characterise.py")).read()
    assert "UNCONDITIONAL" in src
    # the pairing call must not sit inside a discrepancy branch
    i = src.index("report_pairing(paired_corner_and_detect")
    preceding = src[:i]
    assert "if abs(p - predicted)" not in preceding.rsplit("if not mock:", 1)[-1]


def test_a_rate_is_reported_with_its_precision():
    """17 events knows the corner rate only to within ~2.8x. '1 in 47059' reads
    far more precise than the sample supports, so the interval is printed with
    it — a point estimate without its width invites exactly the overclaiming
    this project keeps catching."""
    import os
    src = open(os.path.join(os.path.dirname(__file__), "..", "voe_stoch",
                            "characterise.py")).read()
    assert "corner rate 95% CI" in src and "known to ~" in src


def test_the_benchmark_documents_measured_not_assumed_properties():
    """Admission required 0 < P < 1; USE requires the documented property to
    match the measured one. Both verdicts must exist in the source."""
    import os
    src = open(os.path.join(os.path.dirname(__file__), "..", "voe_stoch",
                            "characterise.py")).read()
    assert "quantitative block is LIFTED" in src
    assert "NOT fully characterised" in src


# --------------------------------------------------------------------------- #
# INCIDENT 21 — a campaign-length sweep that could not sweep.                  #
# --------------------------------------------------------------------------- #
# SimChannel.build() compiles the campaign length in via -DNVEC, but cached the
# resulting binary under `self._built[inject_bug]` and built it into a directory
# named only for the variant. The first campaign length therefore won: every
# later length silently re-ran the first one's binary. Nothing in the corpus
# could expose this while every caller asked for 20000 vectors. The moment the
# cross-design transfer experiment swept campaign length it would have produced
# a perfectly flat curve and a confident, entirely wrong conclusion about the
# design's detection behaviour.
#
# Found by reading the channel before trusting it, not by a failing run — which
# is the only way this class of defect is ever found.
def test_build_cache_is_keyed_by_campaign_length_not_just_variant():
    seen = []

    class FakeProc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kw):
        seen.append(list(cmd))
        # emulate verilator producing the binary where -Mdir says
        mdir = cmd[cmd.index("-Mdir") + 1]
        os.makedirs(mdir, exist_ok=True)
        open(os.path.join(mdir, "Vtb"), "w").close()
        return FakeProc()

    import subprocess as _sp
    import evidence_channels as ec
    ch = SimChannel(mock=True, sources=["x.sv"], top="tb_x")
    ch.mock = False                      # exercise the real build path
    old = ec.subprocess.run
    try:
        ec.subprocess.run = fake_run
        a, err_a = ch.build(False, nvec=64)
        b, err_b = ch.build(False, nvec=4096)
    finally:
        ec.subprocess.run = old

    assert not err_a and not err_b
    # two campaign lengths are two different binaries, in two directories
    assert a != b, "different campaign lengths shared one binary"
    assert ("-DNVEC=64" in seen[0]) and ("-DNVEC=4096" in seen[1])
    # and the cache must distinguish them
    assert ch._built[(False, 64)] != ch._built[(False, 4096)]


def test_testbenches_read_the_campaign_length_the_channel_passes():
    """tb_satmac.sv defined NCYC and defaulted it to 20000 while the channel
    passed -DNVEC. The define never matched, so nvec was inert: every sat_mac
    campaign ran 20000 cycles regardless of what was asked for. Harmless while
    every caller asked for 20000, and a silently flat curve otherwise."""
    for rel in (("voe_stoch", "sim", "tb_satmac.sv"),
                ("voe_stoch2", "sim", "tb_pfifo.sv")):
        src = open(os.path.join(ROOT, *rel)).read()
        # comments are stripped first: the fix is documented by name in a
        # comment in both files, and a test that trips over its own
        # explanation is a test that discourages writing the explanation.
        code = "\n".join(ln.split("//")[0] for ln in src.splitlines())
        assert "`NVEC" in code, f"{rel[-1]} does not read NVEC"
        assert "NCYC" not in code, f"{rel[-1]} still reads the dead NCYC define"


# --------------------------------------------------------------------------- #
# INCIDENT 22 — a zero-width interval around a degenerate rate.                #
# --------------------------------------------------------------------------- #
# The first sat_mac characterisation used a Wald interval and labelled it
# "rough". At p = 0 or p = 1 Wald reports a width of exactly zero, so a board
# that missed the bug on all 12 seeds would have been recorded as
# P(detect) = 0.000 +/- 0.000 — a certainty manufactured by the formula. Exact
# intervals are not a refinement here; they are the difference between "we did
# not see it" and "it does not happen".
def test_exact_interval_refuses_to_manufacture_certainty():
    from binomial import clopper_pearson, non_degenerate
    lo, hi = clopper_pearson(0, 12)
    assert lo == 0.0 and hi > 0.25, "0/12 must not imply P = 0"
    lo, hi = clopper_pearson(12, 12)
    assert hi == 1.0 and lo < 0.75, "12/12 must not imply P = 1"
    # pinned against independently computed Clopper-Pearson values
    lo, hi = clopper_pearson(3, 12)
    assert abs(lo - 0.0549) < 1e-3 and abs(hi - 0.5719) < 1e-3


def test_admission_needs_both_outcomes_not_a_point_estimate():
    """One detection in 12 seeds is a point estimate of 0.083 and is one lucky
    campaign away from zero. Admitting a benchmark on it would be admitting
    noise, so admission counts OUTCOMES rather than thresholding a rate."""
    from binomial import non_degenerate
    assert not non_degenerate(0, 12)[0]
    assert not non_degenerate(1, 12)[0]      # too rare to sample
    assert not non_degenerate(12, 12)[0]     # deterministic classifier
    assert not non_degenerate(11, 12)[0]
    assert non_degenerate(4, 12)[0]


# --------------------------------------------------------------------------- #
# INCIDENT 23 — a shape statistic nobody had calibrated.                       #
# --------------------------------------------------------------------------- #
# Four times now this project has shipped a control that was present, correct,
# and unable to observe. The cross-design transfer verdict rests entirely on
# whether a curve can be excluded by its own best-fitting memoryless law, and
# that statistic had no measured error rate until it was simulated against two
# boards whose answers are known by construction.
#
# The result is worth keeping precisely because it is counterintuitive: the
# statistic gets WORSE calibrated as seeds increase (about 5% false alarms at
# 24 seeds, about 14% at 48), because the exact intervals shrink faster than a
# one-parameter fit to correlated points can track. "Run it with more seeds" is
# therefore not an available response to an ambiguous verdict, and the seed
# count has to be committed in advance like any other configuration.
def test_the_shape_statistic_can_actually_see_the_difference():
    import random
    sys.path.insert(0, os.path.join(ROOT, "voe_bench"))
    from run_stoch_transfer import fit_memoryless, BOARDS
    from binomial import clopper_pearson

    def flagged(points):
        p, _ = fit_memoryless(points)
        return any(not (clopper_pearson(k, t)[0] <= 1 - (1 - p) ** n
                        <= clopper_pearson(k, t)[1])
                   for n, k, t in points)

    p0, n = 1.0 / 65536.0, 24
    random.seed(1)
    memoryless = [(c, sum(random.random() < 1 - (1 - p0) ** c for _ in range(n)), n)
                  for c in BOARDS["sat_mac"]["grid"]]
    random.seed(1)
    warmup = [(c, sum(random.random() < (0.0 if c < 200 else
                                         min(1.0, (c - 200) / 1500.0))
                      for _ in range(n)), n)
              for c in BOARDS["pfifo"]["grid"]]
    assert not flagged(memoryless), "a truly memoryless curve was flagged"
    assert flagged(warmup), "the statistic cannot see a warm-up — it is blind"


def test_seed_count_is_committed_because_more_is_worse():
    src = open(os.path.join(ROOT, "voe_bench", "run_stoch_transfer.py")).read()
    assert "MUST NOT BE RAISED" in src
    assert "--calibrate" in src


# --------------------------------------------------------------------------- #
# INCIDENT 24 — a mutant that was not one line.                                #
# --------------------------------------------------------------------------- #
# Both stochastic benchmarks rest on the claim that the mutant differs from the
# good design in exactly one line, because that is what licenses reading a
# detection as "the stimulus reached this specific corner". The fifo coupling
# probe already produced a reference model that had quietly inherited the
# mutant's bug; a mutant that quietly differs in two places is the same error
# from the other direction, and it would make the instrumentation's corner
# count unrelated to what detection actually measures.
def test_the_pfifo_mutant_differs_in_exactly_one_line():
    def body(path):
        # Strip comments and collapse whitespace before comparing. Column
        # alignment and explanatory comments are not part of the design, and a
        # test that counted them would fail for reasons that have nothing to do
        # with what the two modules DO.
        out = []
        for ln in open(path):
            s = ln.split("//")[0].strip()
            if not s:
                continue
            out.append(" ".join(s.split()).replace("pfifo_mut", "pfifo"))
        return out

    good = body(os.path.join(ROOT, "voe_stoch2", "rtl", "pfifo.sv"))
    mut = body(os.path.join(ROOT, "voe_stoch2", "rtl", "pfifo_mut.sv"))
    assert len(good) == len(mut), "the two designs differ in structure, not one line"
    diffs = [(a, b) for a, b in zip(good, mut) if a != b]
    assert len(diffs) == 1, f"expected exactly one differing line, got {diffs}"
    assert "wr_q" in diffs[0][0], "the difference is not on the write pointer"


def test_pfifo_depth_is_not_a_power_of_two():
    """The mutant drops the explicit wrap. On a depth-8 FIFO the 3-bit pointer
    wraps by itself, so the mutant would be bit-identical to the good design and
    the entire benchmark would measure nothing while looking perfectly healthy."""
    src = open(os.path.join(ROOT, "voe_stoch2", "rtl", "pfifo.sv")).read()
    assert "DEPTH = 6" in src
    assert "LOAD-BEARING" in src


# --------------------------------------------------------------------------- #
# INCIDENT 25 — the refused move, in a new costume.                            #
# --------------------------------------------------------------------------- #
# Experiment 15 refused to shrink nvec until the bug was sometimes missed. The
# second board cannot inherit a campaign length the way sat_mac did, so the same
# temptation returns as "pick a principled N". It is defused by committing a
# GRID and publishing the whole curve: there is no point to select, because
# every point is reported, including the degenerate ones.
def test_the_second_board_commits_a_grid_not_a_chosen_length():
    src = open(os.path.join(ROOT, "voe_stoch2", "characterise.py")).read()
    assert "GRID = (" in src
    assert "degenerate points included" in src or "degenerate" in src
    # and the refusal must remain explicit rather than implied
    assert "refused" in src


def test_a_degenerate_grid_is_not_answered_by_extending_the_grid():
    for rel in (("voe_stoch2", "characterise.py"),
                ("voe_bench", "run_stoch_transfer.py")):
        src = open(os.path.join(ROOT, *rel)).read()
        assert "wearing a different hat" in src, rel


def test_benchmark_commitments_are_tamper_evident():
    """A configuration hashed before the data is only a control if something
    re-checks it afterwards. A stamp nothing verifies is decoration — the same
    reasoning that made witnesses re-hashed rather than merely recorded."""
    import tempfile
    from preregistration import Commitment
    d = tempfile.mkdtemp()
    path = os.path.join(d, "c.json")
    c = Commitment("t", path, {"grid": [1, 2, 3]}, "notes").commit()
    assert Commitment.load(path).intact
    text = open(path).read().replace("[\n      1,", "[\n      9,")
    open(path, "w").write(text)
    assert not Commitment.load(path).intact


# --------------------------------------------------------------------------- #
# INCIDENT 26 — a prose comment that stopped the compiler.                     #
# --------------------------------------------------------------------------- #
# The pfifo header paragraph wrapped so that one line began with the word
# "Verilator". The simulator parses a comment starting with that word as a
# pragma, did not recognise this one, and refused to compile. Every campaign in
# the S1 grid then returned status 'error'; the positive control correctly
# reported an unvalidated checker and the whole run was discarded.
#
# Two things make this worth a permanent test rather than a one-line fix.
# First, the failure was invisible at the level anyone was looking: the verdict
# said "the testbench does not pass on correct RTL", which sends a reader to the
# checker, and the checker was fine. Second, it is pure prose — no amount of
# care about the DESIGN would have prevented it, and it will recur the moment
# someone rewraps a paragraph.
def test_no_comment_line_starts_with_the_pragma_word():
    import re
    bad = []
    for sub in ("voe_stoch", "voe_stoch2", "voe_fifo", "voe_heldout", "phase3"):
        base = os.path.join(ROOT, sub)
        for dirpath, _dirs, files in os.walk(base):
            for fn in files:
                if not fn.endswith((".sv", ".v")):
                    continue
                path = os.path.join(dirpath, fn)
                for i, ln in enumerate(open(path, errors="replace"), 1):
                    # a real pragma is fine; an unrecognised one is fatal, and
                    # prose is never a recognised one.
                    m = re.match(r"\s*(?://|/\*)\s*verilator\b(.*)", ln, re.I)
                    if not m:
                        continue
                    rest = m.group(1).strip().lower()
                    known = ("lint_off", "lint_on", "lint_save", "lint_restore",
                             "public", "public_flat", "public_flat_rd",
                             "public_flat_rw", "no_inline", "coverage_off",
                             "coverage_on", "tracing_off", "tracing_on",
                             "isolate_assignments", "sc_bv", "clocker",
                             "no_clocker", "split_var", "timing", "no_timing",
                             "hier_block", "inline_module", "unroll_disable",
                             "unroll_full", "forceable")
                    if not rest.startswith(known):
                        bad.append(f"{path}:{i}: {ln.strip()[:70]}")
    assert not bad, ("comment lines beginning with the pragma word are parsed "
                     "as pragmas and abort the build:\n" + "\n".join(bad))
