"""`industrial_grade` must grade measured coverage, not a derived number (#42).

`VerificationResult.__post_init__` grades on `line >= 95 and functional >= 90`.
`functional` is not measured: the real backend computes it as a weighted composite
of line/branch/toggle/expression blended with instruction-mix coverage, and the
orchestrator's own fallback computes `(line + branch) / 2` with toggle and
expression hardcoded 0. Either way the grade reads the value and never asks where
it came from -- the backend records provenance in `source_file`, and
`to_ava_dict()` drops it before the orchestrator sees it.

AGENT_F's own `is_industrial_grade` (`coverage_pipeline.py:245`) requires
`line >= 95`, `branch >= 90` **and `toggle >= 85`** -- measured toggle. That is the
authoritative definition, and it demands exactly what the orchestrator's version
fakes. Two definitions of one metric, the #34 shape.

Measured against the three realities that matter, before and after:

    A  synthesised (coverage_data dict, no .dat)  toggle never measured
    B  measured (real .dat), strong               toggle 95
    C  measured (real .dat), genuinely weak       toggle 50

The rule this file pins: a run grades industrial only when its coverage was
*measured* and clears AGENT_F's thresholds. A grades NO because nothing measured
toggle, C grades NO because toggle is genuinely weak, and B -- which today's rule
also rejects, because a 20-instruction commit log drags the blended `functional`
to 67.58 -- grades YES.

**Why subprocesses.** Same trap as #41: `pyproject.toml`'s
`[tool.pytest.ini_options] pythonpath` lists `AGENT_F` (`:53`), so under pytest the
backend resolves and the grade is computed from the real backend's numbers, while a
run from the repo root is what the orchestrator actually does. An in-process
assertion cannot tell the two worlds apart, so each test here launches a fresh
interpreter with cwd = the repo root, no `AGENT_F` on `sys.path` and no inherited
`PYTHONPATH`, and asserts the probe really had no `AGENT_F` on its path so it
cannot pass vacuously.
"""

import inspect
import json
import os
import pathlib
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

# A .dat the Verilator parser accepts: comment '' = line, 'b' = branch,
# 's' = toggle, 'e' = expression (coverage_pipeline.py:130 from_comment).
_DAT_WRITER = r"""
def _write_dat(path, n_line, line_hit, n_br, br_hit, n_tog, tog_hit, n_ex, ex_hit):
    rows = ["# SystemC::Coverage-3", "# verilator 5.020"]
    def emit(n, hit, comment):
        for i in range(n):
            cnt = 7 if i < hit else 0
            rows.append(
                "C '%d' 'riscv_core.sv' '%d' '0' 'top.riscv_core' '%s'"
                % (cnt, i + 1, comment))
    emit(n_line, line_hit, "")
    emit(n_br,   br_hit,   "b0")
    emit(n_tog,  tog_hit,  "s0")
    emit(n_ex,   ex_hit,   "e0")
    path.write_text("\n".join(rows) + "\n")
"""

_PROBE = _DAT_WRITER + r"""
import asyncio, json, pathlib, sys
MODE = sys.argv[1]
import ava_patched as av

RTL = '''
module riscv_core (input wire clk, input wire rst,
                   input wire [31:0] instr_in, output reg [31:0] data_out);
    reg [31:0] fetch_pc;
    reg [31:0] decode_instr;
endmodule
'''
PCS = ["0x%08x" % (0x80000000 + 4 * i) for i in range(20)]
def _log():
    return [{"pc": p, "instr": "0x00000013", "regs": {}} for p in PCS]

# toggle 95% / expression 90% when measured strongly; toggle 50% when weak.
_TOG = {"measured_strong": (40, 38), "measured_weak": (40, 20)}

async def _rtl(self, tb_suite, semantic_map):
    out = {"cycles": 1000, "instructions": 20,
           "performance": {"ipc": 0.9, "branch_predictions": 100,
                           "branch_correct": 95},
           "commit_log": _log(), "state_snapshots": [], "seed": 1}
    if MODE == "synthesised":
        # No .dat anywhere -- the backend falls back to this dict, which is
        # exactly the "nothing measured toggle" case.
        out["coverage_data"] = {"lines_hit": 196, "total_lines": 200,
                                "branches_hit": 94, "total_branches": 100}
    else:
        n_tog, tog_hit = _TOG[MODE]
        _write_dat(pathlib.Path(self._run_dir) / "coverage.dat",
                   100, 98, 50, 47, n_tog, tog_hit, 20, 18)
    return out

async def _iss(self, semantic_map, stimulus, tb_suite):
    return {"instructions": 20, "commit_log": _log(), "seed": 1}

av.SpikeISS._simulate_rtl = _rtl
av.SpikeISS._simulate_iss = _iss

async def main():
    ava = av.AVA(enable_llm=False, timeout=60, target_coverage=90.0,
                 run_base_dir="/tmp/ava_grade_" + MODE, enable_database=False)
    r = await ava.generate_suite(rtl_spec=RTL, microarch="in_order", seed=42,
                                 save_results=False)
    init = r["initial_results"]
    print("PROBE" + json.dumps({
        "backend_live": bool(av.COVERAGE_PIPELINE_AVAILABLE),
        "industrial_grade": r["industrial_grade"],
        "coverage": init["coverage"],
        "metadata": {k: v for k, v in (init.get("metadata") or {}).items()
                     if "coverage" in k or "measur" in k or "source" in k},
        "agent_f_on_path": [p for p in sys.path if p.endswith("AGENT_F")],
    }))

asyncio.run(main())
"""


def _run_from_repo_root(mode):
    """A real run, from the repo root, with nothing injected onto sys.path."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, mode],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, (
        f"the {mode} run failed outright:\n{proc.stdout}\n{proc.stderr}")
    line = [l for l in proc.stdout.splitlines() if l.startswith("PROBE")]
    assert line, f"probe printed nothing usable:\n{proc.stdout}\n{proc.stderr}"
    info = json.loads(line[-1][len("PROBE"):])
    assert info["agent_f_on_path"] == [], (
        "this probe must run without AGENT_F on sys.path, or it proves nothing "
        f"about a real run; it leaked in as {info['agent_f_on_path']}")
    assert info["backend_live"] is True, (
        "the real coverage backend did not resolve from the repo root, so this "
        "run is not the one being reasoned about (#41)")
    return info


def test_synthesised_coverage_does_not_grade_industrial():
    """Reality A. Nothing measured toggle, so the run must not be graded -- and
    it must say so, rather than declining for an unrelated arithmetic reason."""
    info = _run_from_repo_root("synthesised")
    assert info["coverage"]["toggle"] == 0.0, (
        "precondition: this run has no measured toggle coverage")
    assert info["industrial_grade"] is False, (
        "a run whose coverage was synthesised from a dict was graded industrial")
    assert info["metadata"], (
        "the result records nothing about where its coverage came from, so the "
        "grade cannot be asking -- provenance (source_file) is dropped by "
        "to_ava_dict() before the orchestrator sees it (#42)")
    assert info["metadata"].get("coverage_measured") is False, (
        f"expected coverage_measured False for synthesised coverage, got "
        f"{info['metadata']!r}")


def test_measured_and_strong_coverage_does_grade_industrial():
    """Reality B, and the assertion that makes this file discriminating.

    Without it, "synthesised does not grade" is satisfied by a rule that never
    grades anything -- which is what today's rule does, since the blended
    `functional` cannot clear 90 on a short commit log however good the
    structural coverage is.
    """
    info = _run_from_repo_root("measured_strong")
    cov = info["coverage"]
    assert cov["toggle"] >= 85.0 and cov["line"] >= 95.0 and cov["branch"] >= 90.0, (
        f"precondition: this run's measured coverage should clear AGENT_F's "
        f"thresholds; got {cov}")
    assert info["metadata"].get("coverage_measured") is True, (
        f"this run's coverage came from a real .dat; metadata says "
        f"{info['metadata']!r}")
    assert info["industrial_grade"] is True, (
        f"measured coverage clearing every AGENT_F threshold "
        f"(line {cov['line']}, branch {cov['branch']}, toggle {cov['toggle']}) "
        f"was still not graded industrial, because the grade reads the derived "
        f"`functional` ({cov['functional']}) instead (#42)")


def test_measured_but_weak_toggle_does_not_grade_industrial():
    """Reality C. The other control: grading honestly must still mean grading."""
    info = _run_from_repo_root("measured_weak")
    cov = info["coverage"]
    assert 0.0 < cov["toggle"] < 85.0, (
        f"precondition: toggle measured but below AGENT_F's threshold; got {cov}")
    assert info["metadata"].get("coverage_measured") is True
    assert info["industrial_grade"] is False, (
        f"toggle was measured at {cov['toggle']}%, below AGENT_F's 85 threshold, "
        f"and the run was graded industrial anyway")


def test_the_two_definitions_of_industrial_grade_agree():
    """The #34 guard. AGENT_F's is_industrial_grade is authoritative; the
    orchestrator must not carry a second set of numbers that can drift."""
    from AGENT_F.coverage_pipeline import CoverageMetrics
    import ava_patched as av

    sig = inspect.signature(CoverageMetrics.is_industrial_grade)
    authoritative = {
        "line":   sig.parameters["line_threshold"].default,
        "branch": sig.parameters["branch_threshold"].default,
        "toggle": sig.parameters["toggle_threshold"].default,
    }
    assert hasattr(av, "INDUSTRIAL_GRADE_THRESHOLDS"), (
        "ava_patched grades industrial with thresholds written inline; there is "
        "nothing to compare against AGENT_F's authoritative definition (#42/#34)")
    assert av.INDUSTRIAL_GRADE_THRESHOLDS == authoritative, (
        f"the orchestrator grades on {av.INDUSTRIAL_GRADE_THRESHOLDS} while "
        f"AGENT_F's is_industrial_grade defaults to {authoritative}")
