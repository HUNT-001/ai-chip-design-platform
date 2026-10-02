"""A comparator mismatch must reach the run's verdict (#44).

Written from the issue. `CommitLogComparator.compare` finds real RTL/ISS
divergences and returns `BugReport`s into `VerificationResult.bugs`, and from
there they gate **only further analysis** (`ava_patched.py:3062` root cause,
`:3079` causal, `:3106` knowledge graph, `:3120` explainer). Nothing reads them
for the verdict, so a run that found a critical PC divergence reports exactly
what a clean run reports: `status "completed"`, exit 0.

`AGENT_D/compare_commitlogs.py` already states the project's position for the
same finding: `passed = len(results) == 0` (`:1838`) and
`return EXIT_PASS if result.passed else EXIT_MISMATCH` (`:2187`), with
`EXIT_MISMATCH = 1` documented as "at least one logical divergence found"
(`:114`). So the orchestrator must not disagree with its own comparator CLI.

Only the two simulators are stubbed, so the RTL and ISS commit logs differ in
exactly one place. The comparator and the verdict path under test are real.

Deliberately out of scope: `industrial_grade`, which is computed from coverage
alone and is #42's subject. `test_industrial_grade_is_not_affected_by_bugs` pins that
boundary so a fix here cannot quietly widen into it.
"""

import asyncio
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import ava_patched
from ava_patched import AVA, SpikeISS
from AGENT_D.compare_commitlogs import EXIT_MISMATCH, EXIT_PASS

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

# Coverage high enough that the run passes on its own merits, so a non-passing
# verdict can only have come from the divergence being injected.
_COVERAGE_OK = {"lines_hit": 196, "total_lines": 200,
                "branches_hit": 94, "total_branches": 100}

_N = 20
_PCS = [f"0x{0x80000000 + 4 * i:08x}" for i in range(_N)]


def _log(pcs, regs=None):
    return [{"pc": p, "instr": "0x00000013", "regs": dict(regs or {})}
            for p in pcs]


def _stub_sims(monkeypatch, rtl_log):
    """RTL sees `rtl_log`; the ISS sees a clean log of the same length."""
    iss_log = _log(_PCS)

    async def _rtl(self, tb_suite, semantic_map):
        return {
            "cycles": 1000, "instructions": len(rtl_log),
            "coverage_data": dict(_COVERAGE_OK),
            "performance": {"ipc": 0.9, "branch_predictions": 100,
                            "branch_correct": 95},
            "commit_log": [dict(e) for e in rtl_log],
            "state_snapshots": [], "seed": 1,
        }

    async def _iss(self, semantic_map, stimulus, tb_suite):
        return {"instructions": len(iss_log),
                "commit_log": [dict(e) for e in iss_log], "seed": 1}

    monkeypatch.setattr(SpikeISS, "_simulate_rtl", _rtl)
    monkeypatch.setattr(SpikeISS, "_simulate_iss", _iss)


def _run(tmp_path, label):
    ava = AVA(enable_llm=False, timeout=30, target_coverage=90.0,
              run_base_dir=str(tmp_path / label), enable_database=False)
    return asyncio.run(ava.generate_suite(
        rtl_spec=RTL, microarch="in_order", seed=42, save_results=False))


def _exit_code(result):
    """The exit code the CLI yields for this result, per ava_patched's own rule."""
    return ava_patched.exit_code_for_status(result["status"])


def _bugs(result):
    return result["initial_results"]["bugs"]


def _diverging_pc_log():
    pcs = list(_PCS)
    pcs[7] = "0xdeadbeef"
    return _log(pcs)


def test_clean_run_still_passes(tmp_path, monkeypatch):
    """The control. Without it, a verdict that fails every run would satisfy
    every other test in this file."""
    _stub_sims(monkeypatch, _log(_PCS))
    result = _run(tmp_path, "clean")
    assert _bugs(result) == [], "the stubbed logs agree; nothing should be found"
    assert result["status"] == "completed"
    assert _exit_code(result) == EXIT_PASS


def test_critical_pc_mismatch_fails_the_run(tmp_path, monkeypatch):
    """The issue's headline case: one wrong PC, severity critical."""
    _stub_sims(monkeypatch, _diverging_pc_log())
    result = _run(tmp_path, "pc_mismatch")

    bugs = _bugs(result)
    assert [b["kind"] for b in bugs][:1] == ["pc_mismatch"], bugs
    assert bugs[0]["severity"] == "critical", bugs[0]
    assert result["status"] == "failed", (
        f"the comparator reported {len(bugs)} bug(s), the first a CRITICAL "
        f"{bugs[0]['kind']}, and the run still says "
        f"status={result['status']!r}"
    )
    assert _exit_code(result) == 1


def test_the_orchestrator_agrees_with_the_comparator_cli(tmp_path, monkeypatch):
    """Same finding, two components, one verdict. AGENT_D's CLI returns
    EXIT_MISMATCH for this; the orchestrator must not exit 0."""
    _stub_sims(monkeypatch, _diverging_pc_log())
    result = _run(tmp_path, "cli_agreement")
    assert _bugs(result), "precondition: the comparator found the divergence"
    assert _exit_code(result) == EXIT_MISMATCH


def test_high_severity_register_mismatch_fails_the_run(tmp_path, monkeypatch):
    """The comparator's other severity: a register divergence on a register
    that is not sp/ra/gp is "high". A divergence is a divergence -- AGENT_D
    fails the process on any of them."""
    rtl_log = _log(_PCS, regs={"t0": "0x00000001"})
    rtl_log[5]["regs"] = {"t0": "0xffffffff"}
    iss_log_regs = {"t0": "0x00000001"}

    async def _iss(self, semantic_map, stimulus, tb_suite):
        return {"instructions": _N,
                "commit_log": _log(_PCS, regs=iss_log_regs), "seed": 1}

    _stub_sims(monkeypatch, rtl_log)
    monkeypatch.setattr(SpikeISS, "_simulate_iss", _iss)

    result = _run(tmp_path, "reg_mismatch")
    bugs = _bugs(result)
    assert bugs, "precondition: the comparator found the register divergence"
    assert bugs[0]["kind"] == "register_mismatch", bugs[0]
    assert bugs[0]["severity"] == "high", bugs[0]
    assert result["status"] == "failed", (
        f"a HIGH register divergence left status={result['status']!r}"
    )
    assert _exit_code(result) == 1


@pytest.mark.parametrize("severity", ["catastrophic", ""])
def test_a_severity_the_verdict_does_not_recognise_still_fails(
    tmp_path, monkeypatch, severity,
):
    """The policy must fail by default. A bug carrying a severity nobody
    enumerated -- a new level, a typo, an empty string -- is still a divergence
    between RTL and ISS, and must not be the one that slips through as a pass.
    This is the shape of #35 and of this whole class: a control that reports
    success because it did not recognise what it was looking at.

    Deliberately there is no companion case asserting that some low severity
    *passes*. No producer emits one, so such a test would bake in a pass-path
    nobody has decided on. When an advisory finding is first added to
    results.bugs, the policy gets decided at ADVISORY_BUG_SEVERITIES and both
    ends get a test then.
    """
    real_compare = ava_patched.CommitLogComparator.compare

    def _odd_severity(self, rtl_log, iss_log, context_before=5):
        bugs = real_compare(self, rtl_log, iss_log, context_before)
        for b in bugs:
            b.severity = severity          # not in any enumerated set
        return bugs

    _stub_sims(monkeypatch, _diverging_pc_log())
    monkeypatch.setattr(ava_patched.CommitLogComparator, "compare", _odd_severity)

    result = _run(tmp_path, f"odd_severity_{severity or 'empty'}")
    bugs = _bugs(result)
    assert bugs and bugs[0]["severity"] == severity, bugs
    assert result["status"] == "failed", (
        f"a divergence with severity {severity!r} was treated as a pass: "
        f"status={result['status']!r}"
    )
    assert _exit_code(result) == 1


def test_industrial_grade_is_not_affected_by_bugs(tmp_path, monkeypatch):
    """Scope boundary, not an endorsement. `industrial_grade` is computed from
    coverage alone (`ava_patched.py:293-296`), so it reads the same whether or
    not the comparator found a critical divergence. That is wrong and it is
    #42's subject; this change must not silently widen into it.

    Asserted as "identical across the two runs" rather than as a literal True,
    because its value is environment-dependent: under pytest `coverage_pipeline`
    imports and synthesises `functional` conservatively, while a run from the
    repo root takes the fallback path and gets a higher number (#41/#42). The
    invariant this change owns is that the *bugs* do not move it.
    """
    _stub_sims(monkeypatch, _log(_PCS))
    clean = _run(tmp_path, "grade_clean")

    _stub_sims(monkeypatch, _diverging_pc_log())
    dirty = _run(tmp_path, "grade_dirty")

    assert not _bugs(clean) and _bugs(dirty), "precondition: one run diverges"
    assert dirty["industrial_grade"] == clean["industrial_grade"], (
        "industrial_grade moved with the bug list -- if that was deliberate, "
        "this is #42 and the test needs rewriting, not deleting"
    )
