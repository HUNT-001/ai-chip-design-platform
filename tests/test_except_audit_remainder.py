"""#10 (part): the remaining class-A handlers in voe/ and AGENT_B/C/D/J.

Each handler below caught ``Exception`` around work its caller depends on, so a
programming error (TypeError) or a dropped input looked like "nothing to report":

* AGENT_J ``_parse_yosys_cdc_output``: a CDC line that could not be parsed was
  dropped without a trace, so a crossing could vanish from the report.
* AGENT_B ``testbench_generator.run_from_manifest``: any failure analysing the RTL
  read as "skipped".
* AGENT_C ``record_manifest_run``: any failure, a bug included, read as "no plateau".
* AGENT_D ``_serialise_cfg``: a non-config argument read as "no config".
* AGENT_D ``HypothesisEngine._score_mismatch``: a rule guard that crashed was
  skipped as though it had not matched.
* voe ``Worker.execute``: any failure feeding the structural probe was dropped.
* voe ``run_campaign``: a crash in ``gate_status`` read as "gate not armed".
* voe ``probe_structure``: a crash in the RTL parser became a regex guess that
  conditions the policy.

The contract, from the issue and not from the handlers:

(a) a failure that comes from the environment or the data (OSError, an
    unparseable line) is reported (a warning, or the existing verdict) rather
    than silently dropped; and
(b) a programming error (TypeError) raised by the guarded call propagates.

Where the old behaviour for the environmental case is already correct it is kept
as a control that passes before and after. The failing input is the only input in
every case, and each (b) case counts calls and asserts the stub was reached, so a
setup that never gets there fails as a setup error instead of passing for free.

Left alone, on purpose: ``voe/scan_corpus.py`` (needs the parser's real exception
set), and the AGENT_H ``self_evolving_engine`` / ``economics_engine`` fallbacks
(commented, not narrowed).
"""
import importlib
import json
import logging
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _raiser(exc, calls):
    def raiser(*a, **k):
        calls["n"] += 1
        raise exc("injected by test")
    return raiser


def _reached(calls, what):
    assert calls["n"] >= 1, f"setup: {what} was never reached, so this case proves nothing"


# ── AGENT_J: _parse_yosys_cdc_output ──────────────────────────────────────────

@pytest.fixture
def cdc():
    return importlib.import_module("AGENT_J.agent_j_cdc")


def test_cdc_parser_control_parses_a_wellformed_line(cdc):
    paths = cdc._parse_yosys_cdc_output("CDC: sig_a -> sig_b (clock domain crossing)")
    assert [p.signal for p in paths] == ["sig_a"]


def test_cdc_parser_unparseable_line_is_reported_not_dropped(cdc, caplog):
    with caplog.at_level(logging.DEBUG, logger=cdc.logger.name):
        paths = cdc._parse_yosys_cdc_output("CDC: ->")
    assert paths == []
    assert [r for r in caplog.records if r.levelno >= logging.WARNING], \
        "a CDC line that could not be parsed left no trace"


def test_cdc_parser_type_error_propagates(cdc, monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(cdc, "CDCPath", _raiser(TypeError, calls))
    with pytest.raises(TypeError):
        cdc._parse_yosys_cdc_output("CDC: sig_a -> sig_b")
    _reached(calls, "CDCPath")


# ── AGENT_B: testbench_generator.run_from_manifest ────────────────────────────

@pytest.fixture
def tbgen(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(os.path.join(ROOT, "AGENT_B"))
    mod = importlib.import_module("testbench_generator")
    rtl = tmp_path / "rtl"
    rtl.mkdir()
    mp = tmp_path / "run_manifest.json"
    mp.write_text(json.dumps({"run_dir": str(tmp_path), "rtl_dir": str(rtl)}),
                  encoding="utf-8")
    return mod, str(mp), tmp_path


def test_testbench_generator_unreadable_rtl_is_reported_as_skipped(
        tbgen, monkeypatch, caplog):
    mod, mp, run_dir = tbgen
    rg = importlib.import_module("AGENT_H.rtl_graph")
    calls = {"n": 0}
    monkeypatch.setattr(rg.RTLGraphAnalyzer, "from_dir",
                        staticmethod(_raiser(OSError, calls)))
    with caplog.at_level(logging.WARNING):
        assert mod.run_from_manifest(mp) == 0
    _reached(calls, "RTLGraphAnalyzer.from_dir")
    rep = json.loads((run_dir / "testbench_gen_report.json").read_text())
    assert rep["status"] == "skipped"
    assert [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_testbench_generator_type_error_propagates(tbgen, monkeypatch):
    mod, mp, _ = tbgen
    rg = importlib.import_module("AGENT_H.rtl_graph")
    calls = {"n": 0}
    monkeypatch.setattr(rg.RTLGraphAnalyzer, "from_dir",
                        staticmethod(_raiser(TypeError, calls)))
    with pytest.raises(TypeError):
        mod.run_from_manifest(mp)
    _reached(calls, "RTLGraphAnalyzer.from_dir")


# ── AGENT_C: iss_efficiency.record_manifest_run ───────────────────────────────

@pytest.fixture
def iss_eff(tmp_path):
    mod = importlib.import_module("AGENT_C.iss_efficiency")

    def call(manifest):
        return mod.record_manifest_run(
            manifest_path=manifest, db_path=tmp_path / "m.db",
            commit_count=1, duration_s=1.0, log_mode="rtl", spike_exit=0)
    return mod, call


def test_record_manifest_run_unreadable_manifest_warns_and_reports_no_plateau(
        iss_eff, tmp_path, caplog):
    _, call = iss_eff
    with caplog.at_level(logging.WARNING):
        assert call(tmp_path / "missing.json") is False
    assert [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_record_manifest_run_type_error_propagates(iss_eff, tmp_path, monkeypatch):
    mod, call = iss_eff
    mp = tmp_path / "run_manifest.json"
    mp.write_text(json.dumps({"isa": "rv32im"}))
    calls = {"n": 0}
    monkeypatch.setattr(mod.ISSEfficiencyTracker, "record_run",
                        _raiser(TypeError, calls))
    with pytest.raises(TypeError):
        call(mp)
    _reached(calls, "ISSEfficiencyTracker.record_run")


# ── AGENT_D: compare_commitlogs._serialise_cfg ────────────────────────────────

@pytest.fixture
def cmp_mod():
    return importlib.import_module("AGENT_D.compare_commitlogs")


def test_serialise_cfg_control_serialises_a_config(cmp_mod):
    assert isinstance(cmp_mod._serialise_cfg(cmp_mod.CompareConfig()), dict)


def test_serialise_cfg_non_config_is_an_error_not_no_config(cmp_mod):
    with pytest.raises(TypeError):
        cmp_mod._serialise_cfg(object())


# ── AGENT_D: bug_hypothesis rule guards ───────────────────────────────────────

def test_hypothesis_guard_type_error_propagates(monkeypatch):
    bh = importlib.import_module("AGENT_D.bug_hypothesis")
    calls = {"n": 0}
    rule = bh._rule("a hypothesis", 0.9, "meta", requires=_raiser(TypeError, calls))
    monkeypatch.setitem(bh.HYPOTHESIS_DB, "q7_probe_type", [rule])
    engine = bh.HypothesisEngine(max_hypotheses=3, min_confidence=0.5)
    with pytest.raises(TypeError):
        engine._score_mismatch({"mismatch_type": "q7_probe_type",
                                "rtl_entry": {"instr": "0x00000013"}})
    _reached(calls, "the rule guard")


# ── voe: Worker.execute -> learn_structure ────────────────────────────────────

@pytest.fixture
def static_worker(monkeypatch):
    monkeypatch.syspath_prepend(os.path.join(ROOT, "phase3"))
    monkeypatch.syspath_prepend(os.path.join(ROOT, "voe"))
    workers = importlib.import_module("workers")
    obligation_state = importlib.import_module("obligation_state")
    monkeypatch.setattr(obligation_state, "probe_structure",
                        lambda rtl: (False, True, "small"))

    class Static:
        def check(self, _name):
            return types.SimpleNamespace(status="proved", witness="")

        def rtl_for(self, _phi):
            return "unused.sv"

    class Board:
        def get(self, _phi):
            return types.SimpleNamespace(kind="invariant")   # not structural

    w = workers.Worker("w", "skeptic", kern=None, formal=None, sim=None,
                       static=Static())
    ks = types.SimpleNamespace(n_eff=lambda phi: 0)
    return w, (lambda: w.execute(ks, Board(), "phi", "static"))


def test_worker_execute_os_error_in_the_probe_is_tolerated(static_worker):
    w, run = static_worker
    calls = {"n": 0}
    w.learn_structure = _raiser(OSError, calls)
    run()
    _reached(calls, "learn_structure")


def test_worker_execute_type_error_in_learn_structure_propagates(static_worker):
    w, run = static_worker
    calls = {"n": 0}
    w.learn_structure = _raiser(TypeError, calls)
    with pytest.raises(TypeError):
        run()
    _reached(calls, "learn_structure")


# ── voe: evaluation.run_campaign -> gate_status ───────────────────────────────

def _voe(gate_status):
    ns = types.SimpleNamespace
    return ns(k=ns(R=lambda ks, w: 0.0),
              ks=ns(proven=lambda p: False, disproven=lambda p: False),
              board=ns(weights=lambda: {}, tasks=[]),
              run=lambda max_steps: None,
              ledger=ns(spent=0.0),
              formal=ns(gate_status=gate_status))


@pytest.fixture
def evaluation(monkeypatch):
    monkeypatch.syspath_prepend(os.path.join(ROOT, "phase3"))
    monkeypatch.syspath_prepend(os.path.join(ROOT, "voe"))
    return importlib.import_module("evaluation")


def test_run_campaign_os_error_in_gate_status_reads_as_not_armed(evaluation):
    calls = {"n": 0}
    res = evaluation.run_campaign(_voe(_raiser(OSError, calls)), "d", "p")
    _reached(calls, "gate_status")
    assert res.gate_armed is False


def test_run_campaign_type_error_in_gate_status_propagates(evaluation):
    calls = {"n": 0}
    with pytest.raises(TypeError):
        evaluation.run_campaign(_voe(_raiser(TypeError, calls)), "d", "p")
    _reached(calls, "gate_status")


# ── voe: obligation_state.probe_structure -> rtl_graph ────────────────────────

@pytest.fixture
def probe(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(os.path.join(ROOT, "voe"))
    mod = importlib.import_module("obligation_state")
    rtl = tmp_path / "d.sv"
    rtl.write_text("module d(input clk, input a, output reg q);\n"
                   "  always @(posedge clk) q <= a;\nendmodule\n", encoding="utf-8")
    return mod.probe_structure, str(rtl)


def test_probe_structure_control_reads_a_design(probe):
    fn, rtl = probe
    assert fn(rtl)[1] is True            # sequential


@pytest.mark.parametrize("fn_name", ["strip_comments", "parse_module"])
def test_probe_structure_type_error_in_the_parser_propagates(probe, monkeypatch, fn_name):
    fn, rtl = probe
    rg = importlib.import_module("AGENT_H.rtl_graph")
    calls = {"n": 0}
    monkeypatch.setattr(rg, fn_name, _raiser(TypeError, calls))
    with pytest.raises(TypeError):
        fn(rtl)
    _reached(calls, f"rtl_graph.{fn_name}")
