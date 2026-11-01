"""#10 (part): a pipeline stage that fails must not read as a pass (ava_patched).

``AVA._run_extended_pipeline`` runs about 55 stages, and each wraps its work in
``except Exception`` + ``logger.warning`` + carry on. A stage that could not run
leaves no report, and the verdict (#40, #43) is folded from reports and recorded
exit codes, so a stage that died looked exactly like a stage that found nothing.

The contract, from the issue and from the verdict convention #43 set up
(``status`` "completed"/0, "failed"/1, "incomplete"/3, where incomplete means
"could not verify"):

(a) a stage that fails for an environmental reason (OSError here) makes the run
    "incomplete" / exit 3, never "completed", and the stages after it still run;
(b) a programming error (TypeError) raised by a stage propagates; and
(c) a stage that works still passes.

Three stages of different shape are covered; the others follow in later PRs:

* ``agent_j``  -- Agents I-L, called through the ``_call`` wrapper;
* ``contracts`` -- a class-invoked gating verifier that writes a report;
* ``power``    -- one of the ten module verifiers in the shared loop.

Only the simulator and the commit-log source are stubbed. The failing input is
the one injected exception. Each (a) case also counts the stage after the one
that failed, and every injection counts its own calls and asserts it was
reached, so a setup that never gets there fails as a setup error.
"""
import asyncio
import json
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import ava_patched
from ava_patched import AVA, SpikeISS

RTL = """
module riscv_core (
    input  wire        clk,
    input  wire        rst,
    input  wire [31:0] instr_in,
    output reg  [31:0] data_out
);
    reg [31:0] fetch_pc;
    reg [31:0] decode_instr;
endmodule
"""

_COVERAGE_OK = {"lines_hit": 196, "total_lines": 200,
                "branches_hit": 94, "total_branches": 100}

REC = {"schema_version": "2.1.0", "seq": 0, "pc": "0x80000000",
       "insn": "0x00000013", "disasm": "addi x5,x0,1",
       "regs": {"x5": "0x1"}, "csrs": {}, "reads": [], "writes": [],
       "trap": None}


async def _good_sim(self, tb_suite, semantic_map):
    return {
        "cycles": 1000, "instructions": 900,
        "coverage_data": dict(_COVERAGE_OK),
        "performance": {"ipc": 0.9, "branch_predictions": 100, "branch_correct": 95},
        "commit_log": [], "state_snapshots": [], "seed": 1,
    }


def _save_one_record(self, run_dir, results):
    """One identical record on both sides, so every log-gated stage is reached."""
    line = json.dumps(REC) + "\n"
    rtl, iss = run_dir / "rtl_commit.jsonl", run_dir / "iss_commit.jsonl"
    rtl.write_text(line)
    iss.write_text(line)
    return rtl, iss


@pytest.fixture(autouse=True)
def _stubs(monkeypatch):
    monkeypatch.setattr(SpikeISS, "_simulate_rtl", _good_sim)
    monkeypatch.setattr(AVA, "_save_commit_logs", _save_one_record)
    # The real contracts flag this one-record log, which would make every run
    # "failed" and hide the verdict under test; the stage is stubbed to a clean
    # result unless a case replaces it.
    if ava_patched._contract is not None:
        monkeypatch.setattr(ava_patched._contract, "ContractRunner",
                            lambda: _Contract(None, {"n": 0}))


def _run(tmp_path, label="r"):
    ava = AVA(enable_llm=False, timeout=30, target_coverage=90.0,
              run_base_dir=str(tmp_path / label), enable_database=False)
    return asyncio.run(ava.generate_suite(
        rtl_spec=RTL, microarch="in_order", seed=42, save_results=False))


def _exit_code(result):
    return ava_patched.exit_code_for_status(result["status"])


def _chain(exc):
    while exc is not None:
        yield exc
        exc = exc.__cause__ or exc.__context__


def _counting_module_fn(calls, rc=0):
    def fn(manifest_path, *a, **k):
        calls["n"] += 1
        return rc
    return fn


def _raiser(exc, calls):
    def raiser(*a, **k):
        calls["n"] += 1
        raise exc("injected by test")
    return raiser


class _Contract:
    """Stands in for ContractRunner."""
    def __init__(self, exc=None, calls=None):
        self.exc, self.calls = exc, calls

    def check(self, rtl_log, iss_log):
        self.calls["n"] += 1
        if self.exc:
            raise self.exc("injected by test")
        return {"total_violations": 0, "pass": True}


def _inject(monkeypatch, stage, exc):
    """Make `stage` raise `exc`; return (its call counter, the counter of the
    stage that runs after it)."""
    hit, after = {"n": 0}, {"n": 0}
    if stage == "agent_j":
        if ava_patched._agent_j is None or ava_patched._agent_k is None:
            pytest.skip("Agents J/K not importable")
        monkeypatch.setattr(ava_patched._agent_j, "run_from_manifest", _raiser(exc, hit))
        monkeypatch.setattr(ava_patched._agent_k, "run_from_manifest",
                            _counting_module_fn(after))
    elif stage == "contracts":
        if ava_patched._contract is None or ava_patched._temporal is None:
            pytest.skip("contract/temporal checkers not importable")
        monkeypatch.setattr(ava_patched._contract, "ContractRunner",
                            lambda: _Contract(exc, hit))
        monkeypatch.setattr(ava_patched._temporal, "TemporalChecker",
                            lambda *a, **k: type("T", (), {"run": lambda s: (
                                after.__setitem__("n", after["n"] + 1) or
                                {"total_violations": 0, "pass": True})})())
    elif stage == "power":
        if ava_patched._power is None or ava_patched._cdc is None:
            pytest.skip("power/cdc verifiers not importable")
        monkeypatch.setattr(ava_patched._power, "run_from_manifest", _raiser(exc, hit))
        monkeypatch.setattr(ava_patched._cdc, "run_from_manifest",
                            _counting_module_fn(after))
    else:                                                   # pragma: no cover
        raise AssertionError(stage)
    return hit, after


STAGES = ["agent_j", "contracts", "power"]


def test_clean_run_still_passes(tmp_path):
    """(c) The control: with the one-record logs every stage is reached and the
    run is still a pass, so a verdict that fails everything cannot satisfy (a)."""
    result = _run(tmp_path)
    assert result["status"] == "completed"
    assert _exit_code(result) == 0


@pytest.mark.parametrize("stage", STAGES)
def test_stage_that_works_still_passes(tmp_path, monkeypatch, stage):
    """(c) per stage: the same stub that will raise below, here returning cleanly."""
    hit, after = {"n": 0}, {"n": 0}
    if stage == "agent_j":
        monkeypatch.setattr(ava_patched._agent_j, "run_from_manifest",
                            _counting_module_fn(hit))
    elif stage == "contracts":
        monkeypatch.setattr(ava_patched._contract, "ContractRunner",
                            lambda: _Contract(None, hit))
    else:
        monkeypatch.setattr(ava_patched._power, "run_from_manifest",
                            _counting_module_fn(hit))
    result = _run(tmp_path)
    assert hit["n"] >= 1, "setup: the stage was never reached"
    assert result["status"] == "completed"
    assert _exit_code(result) == 0


@pytest.mark.parametrize("stage", STAGES)
def test_stage_failing_with_an_environment_error_makes_the_run_incomplete(
        tmp_path, monkeypatch, stage):
    hit, after = _inject(monkeypatch, stage, OSError)
    result = _run(tmp_path)
    assert hit["n"] >= 1, "setup: the failing stage was never reached"
    assert result["status"] == "incomplete", (
        f"stage {stage!r} raised OSError and the run still says "
        f"status={result['status']!r}")
    assert _exit_code(result) == 3
    assert after["n"] >= 1, "the stage after the failure did not run"


@pytest.mark.parametrize("stage", STAGES)
def test_stage_failing_with_a_programming_error_propagates(tmp_path, monkeypatch, stage):
    hit, _ = _inject(monkeypatch, stage, TypeError)
    with pytest.raises(Exception) as ei:
        _run(tmp_path)
    assert hit["n"] >= 1, "setup: the failing stage was never reached"
    assert any(isinstance(e, TypeError) and "injected by test" in str(e)
               for e in _chain(ei.value)), repr(ei.value)


# ── batch 1: the next eight stages ────────────────────────────────────────────
#
# Each is a class-invoked verifier: `Cls(rtl_log, iss_log).run()` returns a report
# dict.  The stage that runs after it (`after`) is stubbed to count its calls.

class _RunStub:
    """Stands in for a verifier class: `Cls(...).run()`."""
    def __init__(self, calls, exc=None):
        self.calls, self.exc = calls, exc

    def __call__(self, *a, **k):
        return self

    def run(self):
        self.calls["n"] += 1
        if self.exc:
            raise self.exc("injected by test")
        return {"total_violations": 0, "pass": True, "band": "CLEAN"}


BATCH1 = [  # (module attr, class, stage that runs after it)
    ("_intent",    "IntentChecker",     ("_temporal",  "TemporalChecker")),
    ("_temporal",  "TemporalChecker",   ("_atomics",   "AtomicsVerifier")),
    ("_atomics",   "AtomicsVerifier",   ("_csr",       "CSRVerifier")),
    ("_csr",       "CSRVerifier",       ("_rvc",       "RVCVerifier")),
    ("_rvc",       "RVCVerifier",       ("_fp",        "FPVerifier")),
    ("_fp",        "FPVerifier",        ("_bitmanip",  "BitmanipVerifier")),
    ("_bitmanip",  "BitmanipVerifier",  ("_privilege", "PrivilegeVerifier")),
    ("_privilege", "PrivilegeVerifier", ("_pipeline",  "PipelineVerifier")),
]
BATCH1_IDS = [b[0].lstrip("_") for b in BATCH1]


def _stub_pair(monkeypatch, spec, exc):
    attr, cls, (after_attr, after_cls) = spec
    mod, after_mod = getattr(ava_patched, attr), getattr(ava_patched, after_attr)
    if mod is None or after_mod is None:
        pytest.skip(f"{attr}/{after_attr} not importable")
    hit, after = {"n": 0}, {"n": 0}
    monkeypatch.setattr(mod, cls, _RunStub(hit, exc))
    monkeypatch.setattr(after_mod, after_cls, _RunStub(after))
    return hit, after


@pytest.mark.parametrize("spec", BATCH1, ids=BATCH1_IDS)
def test_batch1_stage_that_works_still_passes(tmp_path, monkeypatch, spec):
    hit, after = _stub_pair(monkeypatch, spec, None)
    result = _run(tmp_path)
    assert hit["n"] >= 1, "setup: the stage was never reached"
    assert result["status"] == "completed"
    assert _exit_code(result) == 0


@pytest.mark.parametrize("spec", BATCH1, ids=BATCH1_IDS)
def test_batch1_stage_failing_with_an_environment_error_makes_the_run_incomplete(
        tmp_path, monkeypatch, spec):
    hit, after = _stub_pair(monkeypatch, spec, OSError)
    result = _run(tmp_path)
    assert hit["n"] >= 1, "setup: the failing stage was never reached"
    assert result["status"] == "incomplete", (
        f"stage {spec[0]!r} raised OSError and the run still says "
        f"status={result['status']!r}")
    assert _exit_code(result) == 3
    assert after["n"] >= 1, "the stage after the failure did not run"


@pytest.mark.parametrize("spec", BATCH1, ids=BATCH1_IDS)
def test_batch1_stage_failing_with_a_programming_error_propagates(
        tmp_path, monkeypatch, spec):
    hit, _ = _stub_pair(monkeypatch, spec, TypeError)
    with pytest.raises(Exception) as ei:
        _run(tmp_path)
    assert hit["n"] >= 1, "setup: the failing stage was never reached"
    assert any(isinstance(e, TypeError) and "injected by test" in str(e)
               for e in _chain(ei.value)), repr(ei.value)


# ── batch 2: the next fourteen stages ─────────────────────────────────────────
#
# Eleven are class-invoked (`Cls(rtl_log, ...).run()`), three are modules driven
# through `run_from_manifest` (vaes, vsha, vsm3).  `after` is the stage that runs
# next; it is stubbed to count its calls.

def _stub_stage(monkeypatch, target, calls, exc):
    attr, kind, name = target
    mod = getattr(ava_patched, attr)
    if mod is None:
        pytest.skip(f"{attr} not importable")
    if kind == "cls":
        monkeypatch.setattr(mod, name, _RunStub(calls, exc))
    else:
        monkeypatch.setattr(mod, name, _raiser(exc, calls) if exc
                            else _counting_module_fn(calls))


BATCH2 = [  # (stage, the stage that runs after it)
    (("_vm",        "cls", "VMVerifier"),            ("_tlb",       "cls", "TLBVerifier")),
    (("_tlb",       "cls", "TLBVerifier"),           ("_pipeline",  "cls", "PipelineVerifier")),
    (("_pipeline",  "cls", "PipelineVerifier"),      ("_branchp",   "cls", "BranchPredictorVerifier")),
    (("_branchp",   "cls", "BranchPredictorVerifier"), ("_vector",  "cls", "VectorVerifier")),
    (("_vector",    "cls", "VectorVerifier"),        ("_perfcnt",   "cls", "PerfCounterVerifier")),
    (("_perfcnt",   "cls", "PerfCounterVerifier"),   ("_ooo",       "cls", "OOOVerifier")),
    (("_ooo",       "cls", "OOOVerifier"),           ("_lsq",       "cls", "LSQVerifier")),
    (("_lsq",       "cls", "LSQVerifier"),           ("_crypto",    "cls", "CryptoVerifier")),
    (("_crypto",    "cls", "CryptoVerifier"),        ("_aes",       "cls", "AESVerifier")),
    (("_aes",       "cls", "AESVerifier"),           ("_sm4",       "cls", "SM4Verifier")),
    (("_sm4",       "cls", "SM4Verifier"),           ("_vaes",      "fn", "run_from_manifest")),
    (("_vaes",      "fn",  "run_from_manifest"),     ("_vsha",      "fn", "run_from_manifest")),
    (("_vsha",      "fn",  "run_from_manifest"),     ("_vsm3",      "fn", "run_from_manifest")),
    (("_vsm3",      "fn",  "run_from_manifest"),     ("_vaeskf",    "fn", "run_from_manifest")),
]
BATCH2_IDS = [b[0][0].lstrip("_") for b in BATCH2]


def _stub_pair2(monkeypatch, spec, exc):
    stage, after_stage = spec
    hit, after = {"n": 0}, {"n": 0}
    _stub_stage(monkeypatch, stage, hit, exc)
    _stub_stage(monkeypatch, after_stage, after, None)
    return hit, after


@pytest.mark.parametrize("spec", BATCH2, ids=BATCH2_IDS)
def test_batch2_stage_that_works_still_passes(tmp_path, monkeypatch, spec):
    hit, after = _stub_pair2(monkeypatch, spec, None)
    result = _run(tmp_path)
    assert hit["n"] >= 1, "setup: the stage was never reached"
    assert result["status"] == "completed"
    assert _exit_code(result) == 0


@pytest.mark.parametrize("spec", BATCH2, ids=BATCH2_IDS)
def test_batch2_stage_failing_with_an_environment_error_makes_the_run_incomplete(
        tmp_path, monkeypatch, spec):
    hit, after = _stub_pair2(monkeypatch, spec, OSError)
    result = _run(tmp_path)
    assert hit["n"] >= 1, "setup: the failing stage was never reached"
    assert result["status"] == "incomplete", (
        f"stage {spec[0][0]!r} raised OSError and the run still says "
        f"status={result['status']!r}")
    assert _exit_code(result) == 3
    assert after["n"] >= 1, "the stage after the failure did not run"


@pytest.mark.parametrize("spec", BATCH2, ids=BATCH2_IDS)
def test_batch2_stage_failing_with_a_programming_error_propagates(
        tmp_path, monkeypatch, spec):
    hit, _ = _stub_pair2(monkeypatch, spec, TypeError)
    with pytest.raises(Exception) as ei:
        _run(tmp_path)
    assert hit["n"] >= 1, "setup: the failing stage was never reached"
    assert any(isinstance(e, TypeError) and "injected by test" in str(e)
               for e in _chain(ei.value)), repr(ei.value)
