"""#10 (part): AGENT_H handlers that hid a failure to read or process an input.

Eight handlers outside the per-record verifier loops caught every exception,
logged a warning and carried on, so a run could finish and report success
over an input it never processed:

* fault_injector: an unreadable manifest returned 0; a detector that raised
  was silently counted as "did not detect".
* contract_dsl, temporal_checker, security_intel: a contract / monitor /
  detector that raised was dropped, leaving the report a pass.
* cross_domain: a raw record that could not be translated was dropped.
* formal_fuzzer: a witness that could not be parsed fell back to canned seeds
  with a clean exit.
* digital_twin: a program that could not be screened vanished from the batch.

The contract, from the issue and not from the handlers, for each:

(a) a failure to read or process the input is reported, never a pass: the
    verdict is not pass / exit 0 (3 = "could not verify", #36 and #60), or for
    the one library method with no verdict, the failure is recorded on the
    object; and
(b) a programming error (TypeError) raised by the guarded call propagates.

The failing input is the only input in every case: no extra well-typed record
is added to make an assertion convenient. Each stubbed call counts its calls
and the case asserts it was reached, so a setup that never gets there fails as
a setup error instead of passing for free.
"""
import importlib
import json
from pathlib import Path

import pytest

REC = {"schema_version": "2.1.0", "seq": 0, "pc": "0x80000000",
       "insn": "0x00000013", "disasm": "addi x5,x0,1",
       "regs": {"x5": "0x1"}, "csrs": {}, "reads": [], "writes": [],
       "trap": None}
RAW_DMA = [{"channel": 0, "op": "READ", "src_addr": "0x1000", "length": 64}]


def _mod(name):
    return importlib.import_module(f"AGENT_H.{name}")


def _manifest(tmp_path, *, raw=None, dut=None):
    """A run directory whose only content is one commit-log record."""
    line = json.dumps(REC) + chr(10)
    (tmp_path / "rtl_commit.jsonl").write_text(line)
    (tmp_path / "iss_commit.jsonl").write_text(line)
    if raw is not None:
        (tmp_path / "raw_rtl.jsonl").write_text(
            chr(10).join(json.dumps(r) for r in raw) + chr(10))
    m = {"schema_version": "2.1.0", "run_id": "r10", "run_dir": str(tmp_path)}
    if dut:
        m["agent_config"] = {"dut_class": dut}
    p = tmp_path / "run_manifest.json"
    p.write_text(json.dumps(m))
    return p


def _raiser(exc, calls):
    def raiser(*a, **k):
        calls["n"] += 1
        raise exc("injected by test")
    return raiser


def _reached(calls, what):
    assert calls["n"] >= 1, f"setup: {what} was never reached, so this case proves nothing"


# --------------------------------------------------------------------------- #
# fault_injector: the manifest read                                            #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("kind", ["missing", "not_json"])
def test_fault_injector_unreadable_manifest_is_not_a_pass(kind, tmp_path):
    fi = _mod("fault_injector")
    p = tmp_path / "run_manifest.json"
    if kind == "not_json":
        p.write_text("{not json")
    rc = fi.run_from_manifest(p)
    assert rc == 3, (f"AGENT_H/fault_injector.py: run_from_manifest returned {rc} "
                     f"for a {kind} manifest; nothing was run, so 3 ('could not "
                     f"verify'), never 0")


def test_fault_injector_type_error_reading_the_manifest_propagates(tmp_path, monkeypatch):
    fi = _mod("fault_injector")
    p = _manifest(tmp_path)
    calls = {"n": 0}
    monkeypatch.setattr(fi.json, "load", _raiser(TypeError, calls))
    with pytest.raises(TypeError):
        fi.run_from_manifest(p)
    _reached(calls, "json.load in fault_injector.run_from_manifest")


# --------------------------------------------------------------------------- #
# fault_injector: a detector that raises                                       #
# --------------------------------------------------------------------------- #

def test_fault_injector_detector_failure_is_counted_and_not_a_pass(tmp_path, monkeypatch):
    fi = _mod("fault_injector")
    calls = {"n": 0}
    monkeypatch.setattr(fi, "default_detectors",
                        lambda: [_raiser(ValueError, calls)])
    p = _manifest(tmp_path)
    rc = fi.run_from_manifest(p, n=3)
    _reached(calls, "the detector")
    report = json.loads((tmp_path / "fault_report.json").read_text())
    assert report.get("detector_errors", 0) >= 1, (
        f"AGENT_H/fault_injector.py: a detector that raised was not counted "
        f"(detector_errors={report.get('detector_errors')!r})")
    assert rc == 3, (f"AGENT_H/fault_injector.py: returned {rc} although a "
                     f"detector could not run; expected 3")


def test_fault_injector_detector_type_error_propagates(tmp_path, monkeypatch):
    fi = _mod("fault_injector")
    calls = {"n": 0}
    monkeypatch.setattr(fi, "default_detectors",
                        lambda: [_raiser(TypeError, calls)])
    p = _manifest(tmp_path)
    with pytest.raises(TypeError):
        fi.run_from_manifest(p, n=3)
    _reached(calls, "the detector")


# --------------------------------------------------------------------------- #
# contract_dsl, temporal_checker, security_intel                               #
# --------------------------------------------------------------------------- #

def _contract_runner(exc, calls):
    cd = _mod("contract_dsl")
    c = cd.DesignContract(name="boom", description="d",
                          fn=_raiser(exc, calls), severity="HIGH")
    return cd, cd.ContractRunner([c])


def test_contract_that_raises_is_counted_and_not_a_pass():
    calls = {"n": 0}
    cd, runner = _contract_runner(ValueError, calls)
    report = runner.check([REC], [REC])
    _reached(calls, "the contract")
    assert report.get("contracts_errored") == 1, (
        f"AGENT_H/contract_dsl.py: a contract that raised was not counted "
        f"(contracts_errored={report.get('contracts_errored')!r})")
    assert report["pass"] is False, (
        "AGENT_H/contract_dsl.py: reported a pass although a contract could not run")


def test_contract_that_raises_gives_exit_3(tmp_path, monkeypatch):
    calls = {"n": 0}
    cd, _ = _contract_runner(ValueError, calls)
    monkeypatch.setattr(cd, "_REGISTRY", [cd.DesignContract(
        name="boom", description="d", fn=_raiser(ValueError, calls), severity="HIGH")])
    rc = cd.run_from_manifest(_manifest(tmp_path))
    _reached(calls, "the contract")
    assert rc == 3, f"AGENT_H/contract_dsl.py: returned {rc}; expected 3"


def test_contract_type_error_propagates():
    calls = {"n": 0}
    cd, runner = _contract_runner(TypeError, calls)
    with pytest.raises(TypeError):
        runner.check([REC], [REC])
    _reached(calls, "the contract")


def test_temporal_monitor_that_raises_is_counted_and_not_a_pass(monkeypatch):
    tc = _mod("temporal_checker")
    checker = tc.TemporalChecker([REC], [REC])
    calls = {"n": 0}
    monkeypatch.setattr(type(checker._monitors[0]), "step", _raiser(ValueError, calls))
    report = checker.run()
    _reached(calls, "the monitor")
    assert report.get("monitors_errored", 0) >= 1, (
        f"AGENT_H/temporal_checker.py: a monitor that raised was not counted "
        f"(monitors_errored={report.get('monitors_errored')!r})")
    assert report["pass"] is False, (
        "AGENT_H/temporal_checker.py: reported a pass although a monitor could not run")


def test_temporal_monitor_that_raises_gives_exit_3(tmp_path, monkeypatch):
    tc = _mod("temporal_checker")
    calls = {"n": 0}
    monkeypatch.setattr(type(tc.TemporalChecker([REC], [REC])._monitors[0]),
                        "step", _raiser(ValueError, calls))
    rc = tc.run_from_manifest(_manifest(tmp_path))
    _reached(calls, "the monitor")
    assert rc == 3, f"AGENT_H/temporal_checker.py: returned {rc}; expected 3"


def test_temporal_type_error_propagates(monkeypatch):
    tc = _mod("temporal_checker")
    checker = tc.TemporalChecker([REC], [REC])
    calls = {"n": 0}
    monkeypatch.setattr(type(checker._monitors[0]), "step", _raiser(TypeError, calls))
    with pytest.raises(TypeError):
        checker.run()
    _reached(calls, "the monitor")


def test_security_detector_that_raises_is_counted_and_not_clean(monkeypatch):
    si = _mod("security_intel")
    intel = si.SecurityIntelligence([REC], [REC])
    calls = {"n": 0}
    monkeypatch.setattr(type(intel._detectors[0]), "detect", _raiser(ValueError, calls))
    report = intel.run()
    _reached(calls, "the detector")
    assert report.get("detector_errors", 0) >= 1, (
        f"AGENT_H/security_intel.py: a detector that raised was not counted "
        f"(detector_errors={report.get('detector_errors')!r})")


def test_security_detector_that_raises_gives_exit_3(tmp_path, monkeypatch):
    si = _mod("security_intel")
    calls = {"n": 0}
    monkeypatch.setattr(type(si.SecurityIntelligence([REC], [REC])._detectors[0]),
                        "detect", _raiser(ValueError, calls))
    rc = si.run_from_manifest(_manifest(tmp_path))
    _reached(calls, "the detector")
    assert rc == 3, f"AGENT_H/security_intel.py: returned {rc}; expected 3"


def test_security_type_error_propagates(monkeypatch):
    si = _mod("security_intel")
    intel = si.SecurityIntelligence([REC], [REC])
    calls = {"n": 0}
    monkeypatch.setattr(type(intel._detectors[0]), "detect", _raiser(TypeError, calls))
    with pytest.raises(TypeError):
        intel.run()
    _reached(calls, "the detector")


# --------------------------------------------------------------------------- #
# cross_domain: a raw record that cannot be translated                         #
# --------------------------------------------------------------------------- #

def test_untranslatable_record_is_counted_not_silently_dropped(monkeypatch):
    cdm = _mod("cross_domain")
    adapter = cdm.get_adapter("dma")
    calls = {"n": 0}
    monkeypatch.setattr(type(adapter), "translate_record", _raiser(ValueError, calls))
    out = adapter.translate(RAW_DMA)
    _reached(calls, "translate_record")
    assert out == []
    assert getattr(adapter, "dropped", 0) == 1, (
        f"AGENT_H/cross_domain.py: a record that could not be translated was "
        f"dropped without being counted (dropped={getattr(adapter, 'dropped', None)!r})")


def test_untranslatable_record_gives_exit_3(tmp_path, monkeypatch):
    cdm = _mod("cross_domain")
    calls = {"n": 0}
    monkeypatch.setattr(type(cdm.get_adapter("dma")), "translate_record",
                        _raiser(ValueError, calls))
    rc = cdm.run_from_manifest(_manifest(tmp_path, raw=RAW_DMA, dut="dma"))
    _reached(calls, "translate_record")
    assert rc == 3, f"AGENT_H/cross_domain.py: returned {rc}; expected 3"


def test_translate_type_error_propagates(monkeypatch):
    cdm = _mod("cross_domain")
    adapter = cdm.get_adapter("dma")
    calls = {"n": 0}
    monkeypatch.setattr(type(adapter), "translate_record", _raiser(TypeError, calls))
    with pytest.raises(TypeError):
        adapter.translate(RAW_DMA)
    _reached(calls, "translate_record")


# --------------------------------------------------------------------------- #
# formal_fuzzer: a witness that cannot be parsed                               #
# --------------------------------------------------------------------------- #

def test_unparseable_witness_is_reported_and_gives_exit_3(tmp_path):
    ff = _mod("formal_fuzzer")
    (tmp_path / "equiv_witness.json").write_text("{not json")
    bridge = ff.FormalFuzzBridge(tmp_path / "equiv_witness.json", tmp_path / "seeds")
    report = bridge.run()
    assert report.get("witness_error"), (
        "AGENT_H/formal_fuzzer.py: a witness that could not be parsed was not "
        "reported; the run fell back to canned seeds with nothing said")
    run = tmp_path / "r"
    run.mkdir()
    (run / "equiv_witness.json").write_text("{not json")
    mp = run / "run_manifest.json"
    mp.write_text(json.dumps({"schema_version": "2.1.0", "run_id": "r10",
                              "run_dir": str(run)}))
    rc = ff.run_from_manifest(mp)
    assert rc == 3, f"AGENT_H/formal_fuzzer.py: returned {rc}; expected 3"


def test_witness_type_error_propagates(tmp_path, monkeypatch):
    ff = _mod("formal_fuzzer")
    (tmp_path / "equiv_witness.json").write_text("[]")
    bridge = ff.FormalFuzzBridge(tmp_path / "equiv_witness.json")
    calls = {"n": 0}
    monkeypatch.setattr(ff.FormalFuzzBridge, "_load_witness", _raiser(TypeError, calls))
    with pytest.raises(TypeError):
        bridge.extract_seeds()
    _reached(calls, "_load_witness")


# --------------------------------------------------------------------------- #
# digital_twin: a program that cannot be screened                              #
# --------------------------------------------------------------------------- #

def test_unscreenable_program_is_recorded_not_dropped(tmp_path):
    dt = _mod("digital_twin")
    twin = dt.DigitalTwin()
    out = twin.batch_screen([tmp_path / "does_not_exist.S"])
    assert out == []
    errors = getattr(twin, "screen_errors", None)
    assert errors and len(errors) == 1, (
        f"AGENT_H/digital_twin.py: a program that could not be screened vanished "
        f"from the batch with no record of it (screen_errors={errors!r})")


def test_screen_type_error_propagates(tmp_path, monkeypatch):
    dt = _mod("digital_twin")
    twin = dt.DigitalTwin()
    calls = {"n": 0}
    monkeypatch.setattr(dt.DigitalTwin, "simulate", _raiser(TypeError, calls))
    with pytest.raises(TypeError):
        twin.batch_screen([tmp_path / "x.S"])
    _reached(calls, "simulate")
