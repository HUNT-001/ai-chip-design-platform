"""AGENT_E probe_spike: "found" means Spike runs, not that a file exists (#74).

probe_spike ended in `except Exception: result = (True, "unknown")`, so a
binary on PATH that the kernel refuses to execute, or one that hangs on
--version, was reported found, and a TypeError inside the probe read the same
way. The contract, from the decision on the issue:

* exists but cannot execute (OSError) or times out -> (False, <reason>), so
  the run stops with the existing tool-error exit, and says why;
* runs cleanly but prints no version -> (True, "unknown");
* a programming error propagates.

probe_spike caches per binary for the whole process and every case here
probes the same name ("spike" on PATH), so each case starts from an empty
cache and checks it was not answered from a stale entry.
"""
import importlib
import subprocess

import pytest

QUIET = b"#!/bin/sh\nexit 0\n"


@pytest.fixture
def rc(monkeypatch):
    mod = importlib.import_module("AGENT_E.run_compliance")
    monkeypatch.setattr(mod, "_SPIKE_VERSION_CACHE", {})
    monkeypatch.setattr(mod, "_SPIKE_SIGNATURE_CACHE", {})
    monkeypatch.setattr(mod, "_TOOLCHAIN_CACHE", None)
    return mod


def _spike_on_path(tmp_path, monkeypatch, content):
    spike = tmp_path / "spike"
    spike.write_bytes(content)
    spike.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))       # this spike and nothing else
    return "spike"


def _probe(rc, name):
    assert name not in rc._SPIKE_VERSION_CACHE, "answered from a stale cache entry"
    result = rc.probe_spike(name)
    assert rc._SPIKE_VERSION_CACHE[name] == result, "probe did not use the cache under test"
    return result


def _stub_check_output(rc, monkeypatch, exc):
    calls = []

    def stub(*args, **kwargs):
        calls.append(args)
        raise exc

    monkeypatch.setattr(rc.subprocess, "check_output", stub)
    return calls


def test_spike_on_path_that_cannot_execute_is_not_found(rc, tmp_path, monkeypatch):
    name = _spike_on_path(tmp_path, monkeypatch, b"\x00garbage")   # exec fails: ENOEXEC
    found, reason = _probe(rc, name)
    assert found is False, "a Spike that cannot execute was reported found"
    assert reason, "found=False must say why"
    runner = rc.ComplianceRunner(rc.RunConfig(spike_bin=name, out_dir=tmp_path / "out"))
    errors = runner._validate_tools()
    assert any(reason in e for e in errors), errors


@pytest.mark.parametrize("exc", [
    subprocess.TimeoutExpired(["spike", "--version"], 5),
    PermissionError(13, "Permission denied"),
], ids=["timeout", "permission-denied"])
def test_spike_that_cannot_answer_version_is_not_found(rc, tmp_path, monkeypatch, exc):
    name = _spike_on_path(tmp_path, monkeypatch, QUIET)
    calls = _stub_check_output(rc, monkeypatch, exc)
    found, reason = _probe(rc, name)
    assert calls, "the probe never ran Spike"
    assert found is False, f"{type(exc).__name__} was reported found"
    assert reason, "found=False must say why"


def test_spike_that_runs_but_prints_no_version_is_found_unknown(rc, tmp_path, monkeypatch):
    name = _spike_on_path(tmp_path, monkeypatch, QUIET)
    assert _probe(rc, name) == (True, "unknown")


def test_spike_that_prints_a_version_reports_it(rc, tmp_path, monkeypatch):
    name = _spike_on_path(
        tmp_path, monkeypatch,
        b"#!/bin/sh\necho 'Spike RISC-V ISA Simulator 1.1.1-dev'\n")
    assert _probe(rc, name) == (True, "Spike RISC-V ISA Simulator 1.1.1-dev")


def test_a_programming_error_in_the_probe_propagates(rc, tmp_path, monkeypatch):
    name = _spike_on_path(tmp_path, monkeypatch, QUIET)
    calls = _stub_check_output(rc, monkeypatch, TypeError("probe defect"))
    with pytest.raises(TypeError, match="probe defect"):
        rc.probe_spike(name)
    assert calls, "the probe never ran Spike"
    assert name not in rc._SPIKE_VERSION_CACHE, "a probe that raised left a cached result"
