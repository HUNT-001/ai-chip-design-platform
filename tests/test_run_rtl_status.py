"""AGENT_B's run_rtl must record AGENT_A's lifecycle_status values for each step.

Expected values are hand-written from the rule in AGENT_A/interfaces.md
(a build step that fails is build_error, a simulator that crashes is infra_error --
"a partial log is not evidence of a RTL bug" -- and a simulator that ran and
reported a result is passed or failed), not read back from the code.

build_status reaches the manifest through a variable, so this drives main() with the
build and simulation steps replaced by stubs and reads the manifest it writes.
"""
import importlib.util
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _run_rtl():
    path = ROOT / "AGENT_B" / "backends" / "run_rtl.py"
    spec = importlib.util.spec_from_file_location("status_run_rtl", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sim_result(tmp_path, exit_code):
    return {
        "commitlog_path": str(tmp_path / "rtl.commitlog.jsonl"),
        "coverage_path": str(tmp_path / "coverage.dat"),
        "trace_path": None,
        "sig_path": None,
        "retired": 3,
        "total_cycles": 9,
        "sim_exit_code": exit_code,
        "elapsed_sec": 0.5,
    }


def _drive(monkeypatch, tmp_path, *, build_error=None, sim_exit=0, sim_error=None,
           standalone=False):
    """Run main() with stubbed build/sim; return the manifest dict it left behind."""
    mod = _run_rtl()
    elf = tmp_path / "t.elf"
    elf.write_bytes(b"\x7fELF")
    rtl = tmp_path / "dut.v"
    rtl.write_text("module dut; endmodule\n", encoding="utf-8")
    fake_verilator = tmp_path / "verilator"
    fake_verilator.write_text("", encoding="utf-8")
    run_dir = tmp_path / "run"

    def fake_build(*a, **k):
        if build_error:
            raise RuntimeError(build_error)
        return tmp_path / "Vdut", 1.0

    def fake_sim(*a, **k):
        if sim_error:
            raise RuntimeError(sim_error)
        return _sim_result(tmp_path, sim_exit)

    monkeypatch.setattr(mod, "verilator_build", fake_build)
    monkeypatch.setattr(mod, "run_simulation", fake_sim)

    if standalone:
        argv = ["run_rtl.py", "--rtl", str(rtl), "--top", "dut", "--elf", str(elf),
                "--out", str(run_dir), "--verilator", str(fake_verilator)]
        manifest_path = run_dir / "run_manifest.json"
    else:
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps({
            "rundir": str(run_dir), "seed": 1, "binary": str(elf), "dut": "dut",
            "rtl_files": [str(rtl)], "phases": {},
        }), encoding="utf-8")
        argv = ["run_rtl.py", "--manifest", str(manifest_path),
                "--verilator", str(fake_verilator)]
    monkeypatch.setattr(sys, "argv", argv)
    rc = mod.main()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    return rc, manifest


@pytest.mark.parametrize("standalone", [False, True])
def test_passing_simulation_records_passed(monkeypatch, tmp_path, standalone):
    rc, m = _drive(monkeypatch, tmp_path, sim_exit=0, standalone=standalone)
    assert rc == 0
    assert m["phases"]["build"]["status"] == "passed"
    assert m["phases"]["rtl"]["status"] == "passed"


@pytest.mark.parametrize("standalone", [False, True])
def test_simulation_that_reports_a_failure_records_failed(monkeypatch, tmp_path, standalone):
    rc, m = _drive(monkeypatch, tmp_path, sim_exit=1, standalone=standalone)
    assert rc == 1
    assert m["phases"]["build"]["status"] == "passed"
    assert m["phases"]["rtl"]["status"] == "failed"


def test_failing_build_records_build_error(monkeypatch, tmp_path):
    rc, m = _drive(monkeypatch, tmp_path, build_error="Verilator build failed (exit 2)")
    assert rc == 1
    assert m["phases"]["build"]["status"] == "build_error"
    assert "rtl" not in m["phases"], "the simulator must not have run after a failed build"


def test_crashing_simulator_records_infra_error_and_keeps_the_build_passed(monkeypatch, tmp_path):
    rc, m = _drive(monkeypatch, tmp_path, sim_error="Simulation crashed (exit 139)")
    assert rc == 1
    assert m["phases"]["build"]["status"] == "passed"
    assert m["phases"]["rtl"]["status"] == "infra_error"
