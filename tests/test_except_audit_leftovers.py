"""#10 (part): the leftover broad handlers that could turn a failure into a pass.

Each handler below caught ``Exception`` around work whose result something else
depends on, so a programming error (TypeError) or an unreadable input read as
"nothing there", "tool absent" or "no stimulus":

* AGENT_B ``run_simulation``: a commit log whose last line could not be read
  silently fell back to the rough cycle estimate.
* AGENT_C ``probe_spike``: a Spike that exists but cannot run ``--help`` was
  still reported ``found`` with every capability off, so the run went on with a
  degraded log format.
* AGENT_E ``BuildCache._load_index``: a corrupt cache index read as empty.
* AGENT_G ``run_spike``: any failure returned ``None``, which ``generate_tests``
  reads as "not a failure", so a TypeError looked like a skipped run.
* AGENT_H ``confidence_scorer._load_json`` / ``EconomicsEngine._load_json``: a
  corrupt report read as "file not found".
* ava_patched ``UCB1CoverageDirector.adapt_cold_paths``: any failure became an
  empty stimulus list.
* ava_patched ``AVA.__init__``: any failure opening the coverage database was a
  warning, a programming error included.

The contract, from the issue and not from the handlers:

(a) a failure from the environment or the data (OSError, a corrupt JSON file) is
    visible (a warning, or the module's own "not found" result) and never reads
    as a pass or a skip; and
(b) a programming error (TypeError) raised by the guarded call propagates.

Every (b) case counts calls and asserts the stub was reached, so a setup that
never gets there fails as a setup error and cannot pass for free. Where the old
behaviour for the environmental case is already right it stays as a control that
passes before and after.
"""
import importlib
import importlib.util
import logging
import os
import sqlite3
import subprocess
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


def _warned(caplog):
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


# ── AGENT_B: run_rtl.run_simulation ───────────────────────────────────────────

def _run_rtl():
    path = os.path.join(ROOT, "AGENT_B", "backends", "run_rtl.py")
    spec = importlib.util.spec_from_file_location("leftovers_run_rtl", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _simulate(mod, monkeypatch, run_dir, commit_log):
    """Run run_simulation with a simulator stub that writes ``commit_log``."""
    def fake_run(cmd, *a, **k):
        (run_dir / "rtl.commitlog.jsonl").write_text(commit_log, encoding="utf-8")
        return types.SimpleNamespace(returncode=0)
    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    args = types.SimpleNamespace(trace=False, sig_out=None, max_insns=10,
                                 flush_every=1, mem_base="0x80000000",
                                 mem_size=1024, verbose=False)
    params = {"elf": "x.elf", "runid": "r", "seed": 1}
    return mod.run_simulation(args, params, run_dir / "sim", run_dir)


def test_run_rtl_control_reads_the_last_seq(tmp_path, monkeypatch):
    out = _simulate(_run_rtl(), monkeypatch, tmp_path, '{"seq": 7}\n')
    assert out["total_cycles"] == 21


def test_run_rtl_unreadable_last_line_is_reported(tmp_path, monkeypatch, capsys):
    out = _simulate(_run_rtl(), monkeypatch, tmp_path, '{"seq": 7}\n{not json\n')
    assert out["total_cycles"] == 6          # the estimate: 2 retired x 3
    assert capsys.readouterr().err.strip(), \
        "the commit log could not be read and the estimate was used without a word"


def test_run_rtl_type_error_propagates(tmp_path, monkeypatch):
    mod = _run_rtl()
    calls = {"n": 0}
    monkeypatch.setattr(mod.json, "loads", _raiser(TypeError, calls))
    with pytest.raises(TypeError):
        _simulate(mod, monkeypatch, tmp_path, '{"seq": 7}\n')
    _reached(calls, "json.loads")


# ── AGENT_C: run_iss.probe_spike ──────────────────────────────────────────────

@pytest.fixture
def run_iss():
    return importlib.import_module("AGENT_C.run_iss")


def test_probe_spike_control_missing_binary_is_not_found(run_iss, tmp_path):
    assert run_iss.probe_spike(str(tmp_path / "nope"))["found"] is False


def test_probe_spike_that_cannot_run_is_not_found(run_iss, tmp_path, caplog):
    spike = tmp_path / "spike"
    spike.write_text("not executable\n")          # exists, but exec raises OSError
    spike.chmod(0o644)
    with caplog.at_level(logging.WARNING):
        caps = run_iss.probe_spike(str(spike))
    assert caps["found"] is False, \
        "a Spike that cannot run --help was reported found, so the run goes on blind"
    assert _warned(caplog)


def test_probe_spike_type_error_propagates(run_iss, tmp_path, monkeypatch):
    spike = tmp_path / "spike"
    spike.write_text("x\n")
    calls = {"n": 0}
    monkeypatch.setattr(run_iss.subprocess, "run", _raiser(TypeError, calls))
    with pytest.raises(TypeError):
        run_iss.probe_spike(str(spike))
    _reached(calls, "subprocess.run")


# ── AGENT_E: run_compliance.BuildCache ────────────────────────────────────────

@pytest.fixture
def compliance():
    return importlib.import_module("AGENT_E.run_compliance")


def _cache_dir(tmp_path, text):
    d = tmp_path / "cache"
    d.mkdir()
    (d / "cache_index.json").write_text(text, encoding="utf-8")
    return d


def test_build_cache_control_reads_a_good_index(compliance, tmp_path):
    d = _cache_dir(tmp_path, '{"a": "h"}')
    assert compliance.BuildCache(d)._index == {"a": "h"}


def test_build_cache_corrupt_index_is_reported(compliance, tmp_path, caplog):
    d = _cache_dir(tmp_path, "{not json")
    with caplog.at_level(logging.WARNING):
        cache = compliance.BuildCache(d)
    assert cache._index == {}
    assert _warned(caplog), "a corrupt build-cache index was read as empty without a word"


def test_build_cache_type_error_propagates(compliance, tmp_path, monkeypatch):
    d = _cache_dir(tmp_path, '{"a": "h"}')
    calls = {"n": 0}
    monkeypatch.setattr(compliance.json, "loads", _raiser(TypeError, calls))
    with pytest.raises(TypeError):
        compliance.BuildCache(d)
    _reached(calls, "json.loads")


# ── AGENT_G: asm_builder.run_spike and its caller ─────────────────────────────

@pytest.fixture
def asm_builder():
    return importlib.import_module("AGENT_G.asm_builder")


@pytest.fixture
def spike_on_path(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name, *a, **k: "/usr/bin/spike")


def test_run_spike_control_returns_the_exit_code(asm_builder, spike_on_path, monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=3))
    assert asm_builder.run_spike(tmp_path / "t.elf") == 3


def test_run_spike_that_cannot_launch_is_reported(
        asm_builder, spike_on_path, monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(subprocess, "run", _raiser(OSError, {"n": 0}))
    with caplog.at_level(logging.WARNING):
        assert asm_builder.run_spike(tmp_path / "t.elf") is None
    assert _warned(caplog)


def test_run_spike_type_error_propagates(asm_builder, spike_on_path, monkeypatch, tmp_path):
    calls = {"n": 0}
    monkeypatch.setattr(subprocess, "run", _raiser(TypeError, calls))
    with pytest.raises(TypeError):
        asm_builder.run_spike(tmp_path / "t.elf")
    _reached(calls, "subprocess.run")


def test_generate_tests_does_not_see_a_skipped_run_when_run_spike_raises(
        spike_on_path, monkeypatch, tmp_path):
    """generate_tests treats spike_exit None as 'not a failure'; a TypeError inside
    run_spike must therefore surface as an error, not as a test with no verdict."""
    gt = importlib.import_module("AGENT_G.generate_tests")
    calls = {"n": 0}
    monkeypatch.setattr(subprocess, "run", _raiser(TypeError, calls))
    monkeypatch.setattr(gt, "write_test",
                        lambda **k: {"elf": str(tmp_path / "t.elf"), "asm": None})
    results, errors = gt.run_random(
        n=1, base_seed=1, length=8, outdir=tmp_path, assemble=False,
        verify_spike=True, trap_rate=0.0, weights={})
    _reached(calls, "subprocess.run")
    assert errors, "run_spike raised a TypeError and the caller recorded no error"
    assert not [r for r in results if r["spike_exit"] is None], \
        "a test whose Spike run blew up was recorded as a skipped run"


# ── AGENT_H: confidence_scorer / economics_engine _load_json ──────────────────

def _corrupt(tmp_path):
    p = tmp_path / "report.json"
    p.write_text("{not json", encoding="utf-8")
    return p


def _loaders():
    cs = importlib.import_module("AGENT_H.confidence_scorer")
    ee = importlib.import_module("AGENT_H.economics_engine")
    return [("confidence_scorer", cs, cs._load_json),
            ("economics_engine", ee, ee.EconomicsEngine._load_json)]


@pytest.mark.parametrize("which", [0, 1], ids=["confidence_scorer", "economics_engine"])
def test_load_json_control_reads_a_good_file(tmp_path, which):
    p = tmp_path / "ok.json"
    p.write_text('{"a": 1}', encoding="utf-8")
    assert _loaders()[which][2](p) == {"a": 1}


@pytest.mark.parametrize("which", [0, 1], ids=["confidence_scorer", "economics_engine"])
def test_load_json_corrupt_report_is_reported_not_read_as_absent(tmp_path, caplog, which):
    p = _corrupt(tmp_path)
    with caplog.at_level(logging.WARNING):
        assert _loaders()[which][2](p) is None
    assert any("report.json" in r.getMessage() for r in _warned(caplog)), \
        "a corrupt report read as 'not found' and nothing named the file"


@pytest.mark.parametrize("which", [0, 1], ids=["confidence_scorer", "economics_engine"])
def test_load_json_type_error_propagates(tmp_path, monkeypatch, which):
    _, mod, fn = _loaders()[which]
    p = tmp_path / "ok.json"
    p.write_text('{"a": 1}', encoding="utf-8")
    calls = {"n": 0}
    monkeypatch.setattr(mod.json, "load", _raiser(TypeError, calls))
    with pytest.raises(TypeError):
        fn(p)
    _reached(calls, "json.load")


# ── ava_patched: UCB1CoverageDirector.adapt_cold_paths ────────────────────────

@pytest.fixture
def ava_patched():
    return importlib.import_module("ava_patched")


def _director(ava_patched):
    return ava_patched.UCB1CoverageDirector(target_coverage=90.0)


_COLD = {"lines": [{"file": "a.sv", "line": 3}]}
_COV = {"line": 10.0, "branch": 10.0, "toggle": 10.0, "expression": 10.0}


def test_adapt_cold_paths_control_returns_stimulus(ava_patched):
    out = _director(ava_patched).adapt_cold_paths(dict(_COV), None, cold_path_detail=_COLD)
    assert out and out[0]["target_metric"]


def test_adapt_cold_paths_type_error_propagates(ava_patched):
    d = _director(ava_patched)
    calls = {"n": 0}
    d._test_gen.generate_directed_tests = _raiser(TypeError, calls)
    with pytest.raises(TypeError):
        d.adapt_cold_paths(dict(_COV), None, cold_path_detail=_COLD)
    _reached(calls, "generate_directed_tests")


# ── ava_patched: AVA.__init__ coverage database ───────────────────────────────

def _make_ava(ava_patched, tmp_path):
    return ava_patched.AVA(enable_llm=False, timeout=30, target_coverage=90.0,
                           run_base_dir=str(tmp_path / "runs"), enable_database=True)


def test_coverage_database_that_cannot_open_is_reported_and_skipped(
        ava_patched, tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(ava_patched, "COVERAGE_PIPELINE_AVAILABLE", True)
    monkeypatch.setattr(ava_patched, "CoverageDatabase",
                        _raiser(sqlite3.OperationalError, {"n": 0}), raising=False)
    with caplog.at_level(logging.WARNING):
        ava = _make_ava(ava_patched, tmp_path)
    assert ava._db is None
    assert _warned(caplog)


def test_coverage_database_type_error_propagates(ava_patched, tmp_path, monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(ava_patched, "COVERAGE_PIPELINE_AVAILABLE", True)
    monkeypatch.setattr(ava_patched, "CoverageDatabase",
                        _raiser(TypeError, calls), raising=False)
    with pytest.raises(TypeError):
        _make_ava(ava_patched, tmp_path)
    _reached(calls, "CoverageDatabase")
