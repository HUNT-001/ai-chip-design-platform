"""The pipeline's verdict must reflect its verifiers' verdicts (#40).

Written from the issue: a gating AGENT_H verifier that reports a violation, or
that could not verify at all, must make the run's verdict non-passing -- both the
`status` field and the process exit code. Today `final["status"]` is the literal
"completed" and the CLI returns `0 if status == "completed" else 1`, so every run
passes however its verifiers ended.

Two outcomes, following the exit-code convention the rest of the tree already
uses (`EXIT_MISMATCH`/`EXIT_FAIL` = 1, `EXIT_CONFIG`/`EXIT_TOOL` = 3):

    a violation (pass False / rc 1)  -> status "failed"     -> exit 1
    could not verify (rc 3, #36)     -> status "incomplete" -> exit 3

Only the simulator is stubbed. The verdict path under test is the real one.
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

# Coverage high enough that the run is a pass on its own merits, so a
# non-passing verdict can only come from the verifier being injected.
_COVERAGE_OK = {"lines_hit": 196, "total_lines": 200,
                "branches_hit": 94, "total_branches": 100}


async def _good_sim(self, tb_suite, semantic_map):
    """Stands in for a Verilator run that hit coverage."""
    return {
        "cycles": 1000, "instructions": 900,
        "coverage_data": dict(_COVERAGE_OK),
        "performance": {"ipc": 0.9, "branch_predictions": 100, "branch_correct": 95},
        "commit_log": [], "state_snapshots": [], "seed": 1,
    }


def _violating_cas(manifest_path):
    """A gating verifier that found a violation: the report it would write, and
    1, the exit code its contract documents for a violation."""
    mp = pathlib.Path(manifest_path)
    run_dir = pathlib.Path(json.loads(mp.read_text()).get("run_dir", mp.parent))
    (run_dir / "cas_report.json").write_text(json.dumps({
        "status": "done", "pass": False, "total_violations": 7,
        "band": "HIGH", "metrics": {"cas_ops": 12},
    }))
    return 1


def _unverifiable_cas(manifest_path):
    """A gating verifier that could not verify: exit 3 (#36), no report."""
    return 3


def _violating_agent(manifest_path, *args, **kwargs):
    """Agents I-L return a bare int exit code; 1 means a violation."""
    return 1


def _run(tmp_path, label):
    ava = AVA(enable_llm=False, timeout=30, target_coverage=90.0,
              run_base_dir=str(tmp_path / label), enable_database=False)
    return asyncio.run(ava.generate_suite(
        rtl_spec=RTL, microarch="in_order", seed=42, save_results=False))


@pytest.fixture(autouse=True)
def _stub_simulator(monkeypatch):
    monkeypatch.setattr(SpikeISS, "_simulate_rtl", _good_sim)


def _exit_code(result):
    """The exit code the CLI yields for this result, per ava_patched's own rule."""
    return ava_patched.exit_code_for_status(result["status"])


def test_clean_run_still_passes(tmp_path):
    """The control. Without this, a verdict that fails everything would satisfy
    every other test here."""
    result = _run(tmp_path, "clean")
    assert result["status"] == "completed"
    assert _exit_code(result) == 0


def test_verifier_violation_fails_the_run(tmp_path, monkeypatch):
    monkeypatch.setattr(ava_patched._cas, "run_from_manifest", _violating_cas)
    result = _run(tmp_path, "violation")
    assert result["status"] == "failed", (
        "a gating verifier reported 7 HIGH violations and the run still says "
        f"status={result['status']!r}"
    )
    assert _exit_code(result) == 1


def test_verifier_that_could_not_verify_makes_the_run_incomplete(tmp_path, monkeypatch):
    monkeypatch.setattr(ava_patched._cas, "run_from_manifest", _unverifiable_cas)
    result = _run(tmp_path, "incomplete")
    assert result["status"] == "incomplete", (
        "a gating verifier exited 3 (could not verify) and the run still says "
        f"status={result['status']!r}"
    )
    assert _exit_code(result) == 3


@pytest.mark.skipif(ava_patched._agent_j is None, reason="Agent J not importable")
def test_agent_exit_code_is_not_relabelled_ok(tmp_path, monkeypatch):
    """Agents I-L return a bare int through _call, which today records
    {"status": "ok"} whatever the code was."""
    monkeypatch.setattr(ava_patched._agent_j, "run_from_manifest", _violating_agent)
    result = _run(tmp_path, "agent_j")
    assert result["status"] == "failed", (
        "Agent J exited 1 (violation) and the run still says "
        f"status={result['status']!r}"
    )
    assert _exit_code(result) == 1


def test_verdict_does_not_depend_on_the_readiness_aggregator(tmp_path):
    """verification_twin reports pass=False on any immature run -- its `pass` is
    a tapeout-readiness judgement derived from the other reports, not a
    violation. It must not drag the verdict down on its own."""
    result = _run(tmp_path, "twin")
    twin = result["extended_reports"].get("verification_twin")
    if twin is None or twin.get("pass") is not False:
        pytest.skip("this run's verification_twin did not report pass=False")
    assert result["status"] == "completed"
