"""Regression tests for the kernel-adjacent layers: evidence channels, the
vacuity gate, witness provenance, the judgment bus, reputation and the ledger.

These lock in the properties that were established the hard way during bring-up
(three vacuity incidents and one reference-model bug). Pure Python: no EDA tools
required — channels run in mock mode.

    pytest tests/test_voe_kernel.py --import-mode=importlib -q
"""
import os, sys, importlib.util, contextlib, io
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "phase3"))
sys.path.insert(0, os.path.join(ROOT, "voe"))

from evidence_channels import (FormalChannel, SimChannel, StaticChannel, Evidence,
                               verify_witness, audit_knowledge, _stamp)
from obligations import generate_obligations
from board import Task, TaskBoard, ResourceLedger, ACTION_COST
from bus import JudgmentBus
from reputation import ReputationService
from workers import Worker
from specialists import (PropertyClass, SemanticMemory, Specialist,
                         unowned_properties)


def load_kernel():
    spec = importlib.util.spec_from_file_location(
        "vsa_kernel", os.path.join(ROOT, "docs", "vsa_reference.py"))
    k = importlib.util.module_from_spec(spec)
    with contextlib.redirect_stdout(io.StringIO()):
        spec.loader.exec_module(k)
    return k


K = load_kernel()


# --------------------------------------------------------------------------- #
# Kernel laws                                                                 #
# --------------------------------------------------------------------------- #
def test_wit1_judgment_requires_witness():
    """Wit-1: unjustified knowledge must be unrepresentable."""
    with pytest.raises(ValueError):
        K.Judgment("phi", K.Warrant.INDUCTIVE, {"n_eff": 5}, witness=None)


def test_risk_decreases_with_evidence_and_proof_discharges():
    ks, props = K.KnowledgeState(), {"phi": 4.0}
    r0 = K.R(ks, props)
    ks.believe(K.Judgment("phi", K.Warrant.INDUCTIVE, {"n_eff": 3}, witness="w"))
    r1 = K.R(ks, props)
    assert r1 < r0
    ks.believe(K.Judgment("phi", K.Warrant.DEDUCTIVE, {"n_eff": 3}, witness="proof"))
    assert K.R(ks, props) == 0.0          # Sem-1: a proof leaves the risk pool


def test_counterexample_leaves_risk_pool():
    ks, props = K.KnowledgeState(), {"phi": 4.0}
    ks.believe(K.Judgment("phi", K.Warrant.INDUCTIVE,
                          {"n_eff": 0, "counterexample": True}, witness="trace"))
    assert ks.disproven("phi")
    assert K.R(ks, props) == 0.0          # known bug: tracked in B, not residual


def test_risk_is_prior_free_and_recomputed():
    ks, props = K.KnowledgeState(), {"phi": 1.0}
    ks.believe(K.Judgment("phi", K.Warrant.INDUCTIVE, {"n_eff": 2}, witness="w"))
    assert K.R(ks, props) == K.R(ks, props)      # Safe-1: no hidden prior state
    assert not hasattr(ks, "_R_cached")          # Struct-2: derived, never stored


# --------------------------------------------------------------------------- #
# Vacuity gate — the invariant that caught three real incidents               #
# --------------------------------------------------------------------------- #
def test_gate_blocks_proof_when_no_negative_control():
    fc = FormalChannel(mock=True)                 # no negative_control declared
    ev = fc.prove("prove_add")
    assert ev.status == "gate_failed"
    armed, why = fc.gate_status()
    assert not armed and "no negative_control" in why


def test_gate_arms_when_negative_control_fails():
    fc = FormalChannel(mock=True, negative_control="bug_logic")
    armed, _ = fc.gate_status()
    assert armed                                  # mock: *bug* tasks return cex
    assert fc.prove("prove_add").status == "proved"


def test_gate_blocks_when_negative_control_passes(monkeypatch):
    """If the known-bad job PASSES the assertions are not binding: refuse to certify."""
    fc = FormalChannel(mock=True, negative_control="control")
    monkeypatch.setattr(fc, "_run_task",
                        lambda t: Evidence("formal", "proved", witness="w"))
    ev = fc.prove("prove_add")
    assert ev.status == "gate_failed"
    assert "must fail" in fc.gate_status()[1]


def test_gate_is_evaluated_once(monkeypatch):
    fc = FormalChannel(mock=True, negative_control="bug_logic")
    calls = []
    real = fc._run_task
    monkeypatch.setattr(fc, "_run_task", lambda t: (calls.append(t), real(t))[1])
    fc.prove("prove_add"); fc.prove("prove_cmp")
    assert calls.count("bug_logic") == 1          # cached, not re-run per property


# --------------------------------------------------------------------------- #
# Warrant typing — bounded vs proved                                          #
# --------------------------------------------------------------------------- #
def test_bmc_on_sequential_dut_is_not_a_proof(tmp_path):
    sby = tmp_path / "j.sby"
    sby.write_text("[options]\nmode bmc\ndepth 20\n")
    fc = FormalChannel(str(sby), mock=True, negative_control="bug_x")
    assert fc.prove("prove_x").status == "bounded_pass"   # NOT 'proved'


def test_bmc_on_combinational_dut_is_a_proof(tmp_path):
    sby = tmp_path / "j.sby"
    sby.write_text("[options]\nmode bmc\ndepth 2\n")
    fc = FormalChannel(str(sby), mock=True, combinational=True,
                       negative_control="bug_x")
    assert fc.prove("prove_x").status == "proved"


def test_kinduction_is_a_proof(tmp_path):
    sby = tmp_path / "j.sby"
    sby.write_text("[options]\nmode prove\ndepth 2\n")
    fc = FormalChannel(str(sby), mock=True, negative_control="bug_x")
    assert fc.prove("prove_x").status == "proved"


# --------------------------------------------------------------------------- #
# Witness provenance                                                          #
# --------------------------------------------------------------------------- #
def test_witness_verifies_and_detects_tampering(tmp_path):
    art = tmp_path / "status"
    art.write_text("PASS")
    w = _stamp(str(art))
    assert "#sha256:" in w
    assert verify_witness(w)[0]
    art.write_text("PASS (tampered)")
    ok, why = verify_witness(w)
    assert not ok and "CONTENT CHANGED" in why


def test_unstamped_missing_and_mock_witnesses_are_not_verified(tmp_path):
    assert not verify_witness("")[0]
    assert not verify_witness("<mock>/x/PASS")[0]
    assert not verify_witness(str(tmp_path / "nope") + "#sha256:abc")[0]
    assert not verify_witness("/plain/path")[0]          # unstamped


def test_audit_knowledge_reports_each_claim(tmp_path):
    art = tmp_path / "t.log"; art.write_text("x")
    ks = K.KnowledgeState()
    ks.believe(K.Judgment("good", K.Warrant.INDUCTIVE, {"n_eff": 1},
                          witness=_stamp(str(art))))
    ks.believe(K.Judgment("bad", K.Warrant.INDUCTIVE, {"n_eff": 1},
                          witness="<mock>/x"))
    ok, rows = audit_knowledge(ks)
    assert not ok and len(rows) == 2
    assert dict((p, o) for p, o, _ in rows) == {"good": True, "bad": False}


# --------------------------------------------------------------------------- #
# Judgment bus — confluence & precedence                                      #
# --------------------------------------------------------------------------- #
def _ind(phi, n, w="w"):
    return K.Judgment(phi, K.Warrant.INDUCTIVE, {"n_eff": n}, witness=w)


def _cex(phi, w="trace"):
    return K.Judgment(phi, K.Warrant.INDUCTIVE,
                      {"n_eff": 0, "counterexample": True}, witness=w)


def test_proof_dominates_inductive_regardless_of_order():
    a, b = K.KnowledgeState(), K.KnowledgeState()
    ba, bb = JudgmentBus(K, a), JudgmentBus(K, b)
    proof = lambda: K.Judgment("p", K.Warrant.DEDUCTIVE, {"n_eff": 0}, witness="pf")
    ba.publish("w1", _ind("p", 5)); ba.publish("w2", proof())
    bb.publish("w2", proof());      bb.publish("w1", _ind("p", 5))
    assert a.proven("p") and b.proven("p")        # Struct-1 confluence


def test_counterexample_flags_prior_passer_as_miscalibrated():
    ks = K.KnowledgeState(); bus = JudgmentBus(K, ks)
    bus.publish("explorer", _ind("p", 4))
    res = bus.publish("skeptic", _cex("p"))
    assert res.dominated_worker == "explorer"
    assert ("explorer", "p") in bus.calibration_events
    assert ks.disproven("p")


def test_weaker_inductive_does_not_override_stronger():
    ks = K.KnowledgeState(); bus = JudgmentBus(K, ks)
    bus.publish("w1", _ind("p", 9))
    res = bus.publish("w2", _ind("p", 2))
    assert not res.accepted and ks.n_eff("p") == 9


# --------------------------------------------------------------------------- #
# Reputation & ledger                                                         #
# --------------------------------------------------------------------------- #
def test_miscalibration_lowers_reputation():
    ks = K.KnowledgeState(); bus = JudgmentBus(K, ks)
    led = ResourceLedger(budget=100); rep = ReputationService()
    for w in ("careful", "careless"):
        led.charge(w, "formal")
    bus.publish("careless", _ind("p", 3))
    rep.record("careless", "sim", bus.publish("careless", _ind("p", 4)), 0.2, "pass")
    merge = bus.publish("careful", _cex("p"))
    rep.record("careful", "formal", merge, 2.0, "counterexample")
    r = rep.report(["careful", "careless"], led, bus)
    assert r["careless"]["miscalibrations"] == 1
    assert r["careful"]["reputation"] > r["careless"]["reputation"]


def test_ledger_refuses_over_budget():
    led = ResourceLedger(budget=ACTION_COST["formal"])
    assert led.can_afford("formal")
    led.charge("w", "formal")
    assert not led.can_afford("formal")
    assert led.remaining() == 0


def test_board_open_tasks_excludes_settled():
    ks = K.KnowledgeState()
    board = TaskBoard([Task("a", 1.0, "ta", False), Task("b", 1.0, "tb", False)])
    ks.believe(K.Judgment("a", K.Warrant.DEDUCTIVE, {"n_eff": 0}, witness="pf"))
    assert [t.phi for t in board.open_tasks(ks)] == ["b"]


# --------------------------------------------------------------------------- #
# Worker integration                                                          #
# --------------------------------------------------------------------------- #
def test_gate_failed_records_nothing_and_stops_retrying():
    ks = K.KnowledgeState()
    board = TaskBoard([Task("p", 5.0, "prove_p", False)])
    fc = FormalChannel(mock=True)                 # ungated -> gate_failed
    wk = Worker("skeptic", "skeptic", K, fc, SimChannel(mock=True))
    ev, j = wk.execute(ks, board, "p", "formal")
    assert ev.status == "gate_failed" and j is None
    assert "p" not in ks.K                        # nothing believed
    assert "p" in wk.skip                         # no infinite retry


def test_orchestrator_survives_uncertified_result():
    """A gate-blocked action yields no judgment; the VOE must charge for the work
    and carry on, never publish None (this crashed the orchestrator once)."""
    from voe import VOE
    fc = FormalChannel(mock=True)                 # ungated on purpose
    v = VOE([Task("p", 5.0, "prove_p", False)], budget=20.0, mock=True, formal=fc)
    with contextlib.redirect_stdout(io.StringIO()):
        v.run(max_steps=12)                       # must not raise
    # Simulation evidence still accumulates (sim needs no formal gate), but the
    # ungated formal PASS must never become a proof.
    assert not v.ks.proven("p")
    assert v.k.R(v.ks, v.board.weights()) > 0     # risk NOT discharged
    assert v.ledger.spent > 0                     # the work was still paid for
    assert not v.formal.gate_status()[0]


def test_archetypes_differ_in_method_choice():
    ks = K.KnowledgeState()
    board = TaskBoard([Task("p", 5.0, "prove_p", False)])
    led = ResourceLedger(budget=100)
    fc, sc = FormalChannel(mock=True, negative_control="bug"), SimChannel(mock=True)
    skeptic = Worker("skeptic", "skeptic", K, fc, sc)
    explorer = Worker("explorer", "explorer", K, fc, sc)
    assert skeptic.propose(ks, board, led).method == "formal"
    assert explorer.propose(ks, board, led).method == "sim"


# --------------------------------------------------------------------------- #
# Phase 4 — specialisation as state ownership                                 #
# --------------------------------------------------------------------------- #
def _spec(name, pattern, archetype="explorer", **mem):
    fc = FormalChannel(mock=True, negative_control="bug")
    return Specialist(name, archetype, K, fc, SimChannel(mock=True),
                      PropertyClass(name, pattern),
                      SemanticMemory(domain=name, **mem))


def test_specialist_only_bids_inside_its_class():
    ks = K.KnowledgeState()
    board = TaskBoard([Task("dut.shift", 5.0, "t1", False),
                       Task("dut.cmp", 5.0, "t2", False)])
    led = ResourceLedger(budget=100)
    sh = _spec("shift", r"\.shift$")
    assert sh.propose(ks, board, led).phi == "dut.shift"     # never bids on cmp


def test_specialist_refuses_to_execute_outside_its_class():
    ks = K.KnowledgeState()
    board = TaskBoard([Task("dut.cmp", 5.0, "t2", False)])
    sh = _spec("shift", r"\.shift$")
    with pytest.raises(ValueError, match="does not own"):
        sh.execute(ks, board, "dut.cmp", "sim")


def test_specialist_idle_when_class_is_empty():
    ks = K.KnowledgeState()
    board = TaskBoard([Task("dut.cmp", 5.0, "t2", False)])
    assert _spec("shift", r"\.shift$").propose(ks, board, ResourceLedger(100)) is None


def test_unowned_properties_are_surfaced():
    board = TaskBoard([Task("dut.shift", 1.0, "t", False),
                       Task("dut.mul", 1.0, "t", False)])     # nobody owns mul
    org = [_spec("shift", r"\.shift$")]
    assert unowned_properties(board, org) == ["dut.mul"]


def test_memory_shapes_the_plan():
    """M_s changes WHAT IS TRIED — a formal-first expert skips sampling."""
    ks = K.KnowledgeState()
    board = TaskBoard([Task("dut.shift", 5.0, "t", False)])
    led = ResourceLedger(budget=100)
    cautious = _spec("shift", r"\.shift$", preferred_method="formal",
                     formal_first=True, difficulty=0.8)
    casual = _spec("shift", r"\.shift$", preferred_method="sim", difficulty=0.2)
    assert cautious.propose(ks, board, led).method == "formal"
    assert casual.propose(ks, board, led).method == "sim"


def test_memory_cannot_certify():
    """Attack sheet 2.3: M_s must have NO path to residual risk.

    A specialist with rich domain experience asserting the property is fine has
    exactly the same risk as one that knows nothing — only witnessed evidence in
    the canonical state moves R.
    """
    props = {"dut.shift": 5.0}
    ks = K.KnowledgeState()
    ks.believe(K.Judgment("dut.shift", K.Warrant.INDUCTIVE, {"n_eff": 2}, witness="w"))
    baseline = K.R(ks, props)

    expert = _spec("shift", r"\.shift$",
                   known_failure_modes=["signedness demotion"] * 50,
                   preferred_method="formal", difficulty=0.0,
                   notes="I am certain this property holds")
    novice = _spec("shift", r"\.shift$")
    # Memory is attached to the workers; risk is a function of the state alone.
    assert K.R(ks, props) == baseline
    expert.memory.known_failure_modes.clear()
    expert.memory.difficulty = 1.0
    assert K.R(ks, props) == baseline
    assert not ks.proven("dut.shift")        # experience never proves anything
    assert baseline > 0                      # and never discharges risk


# --------------------------------------------------------------------------- #
# Phase 4b — obligations derived from RTL                                     #
# --------------------------------------------------------------------------- #
IBEX = os.path.join(ROOT, "voe_ibex", "rtl", "ibex_alu.sv")
_needs_ibex = pytest.mark.skipif(not os.path.exists(IBEX), reason="ibex_alu.sv absent")


@_needs_ibex
def test_generator_enumerates_every_output_of_real_rtl():
    tasks, s = generate_obligations(IBEX, module="ibex_alu")
    assert s["module"] == "ibex_alu" and s["outputs"] == 7
    # one obligation per output, plus the structural one
    assert len(tasks) == s["outputs"] + 1
    assert any(t.kind == "structural" for t in tasks)


@_needs_ibex
def test_unbound_obligations_have_no_evidence_path():
    """Naming a property is not checking it: unbound => declared, not dischargeable."""
    tasks, _ = generate_obligations(
        IBEX, module="ibex_alu",
        harness_map={"out.comparison_result_o": "prove_cmp"})
    by = {t.phi: t for t in tasks}
    assert by["ibex_alu.out.comparison_result_o"].has_evidence_path()
    assert not by["ibex_alu.out.result_o"].has_evidence_path()
    assert "NO CHECKER" in by["ibex_alu.out.result_o"].note


@_needs_ibex
def test_unverifiable_obligations_keep_contributing_risk():
    """The board must not go green while real outputs have no checker."""
    tasks, _ = generate_obligations(IBEX, module="ibex_alu")
    board = TaskBoard(tasks)
    ks = K.KnowledgeState()
    # discharge everything that CAN be discharged
    for t in board.actionable(ks):
        ks.believe(K.Judgment(t.phi, K.Warrant.DEDUCTIVE, {"n_eff": 0}, witness="pf"))
    assert K.R(ks, board.weights()) > 0            # residual risk remains
    assert board.unverifiable(ks)                  # and it is named


@_needs_ibex
def test_static_channel_proves_no_comb_loops_on_real_ibex():
    ev = StaticChannel(IBEX).check("comb_loops")
    assert ev.status == "proved"
    ok, _ = verify_witness(ev.witness)
    assert ok                                      # real, hash-verified artifact


def test_static_channel_reports_unsupported_property(tmp_path):
    rtl = tmp_path / "m.sv"
    rtl.write_text("module m(input a, output b); assign b = a; endmodule\n")
    assert StaticChannel(str(rtl)).check("fsm_legality").status == "unsupported"


def test_workers_ignore_obligations_with_no_checker():
    ks = K.KnowledgeState()
    board = TaskBoard([Task("m.out.x", 5.0)])       # no harness bound
    w = Worker("w", "explorer", K, FormalChannel(mock=True), SimChannel(mock=True))
    assert w.propose(ks, board, ResourceLedger(100)) is None
    assert board.unverifiable(ks) == ["m.out.x"]


def test_specialists_share_one_canonical_state():
    """Different owners, one truth: a proof by one specialist settles the
    property for the whole organisation (Struct-1 confluence)."""
    ks = K.KnowledgeState(); bus = JudgmentBus(K, ks)
    bus.publish("shift", K.Judgment("dut.shift", K.Warrant.DEDUCTIVE,
                                    {"n_eff": 0}, witness="pf"))
    board = TaskBoard([Task("dut.shift", 5.0, "t", False)])
    assert board.open_tasks(ks) == []        # nobody re-verifies it
