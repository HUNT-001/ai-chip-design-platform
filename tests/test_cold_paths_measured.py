"""top_cold is one definition, and it is measured (#64).

`AGENT_F/coverage_pipeline.py` chose its `top_cold` list through a bare-name
`from coverage_database import ...` inside `except Exception`. Which source won
depended on `sys.path`: with `AGENT_F` on it (pytest's `pythonpath`, or running
`python AGENT_F/coverage_pipeline.py`) the external DB won and returned `[]`,
because the pipeline's own `CoverageDatabase` never fills the table that reader
queries; from the repo root the import failed and the zero-hit points parsed from
the run's own `.dat` were used. Two answers for one metric.

The decision: `top_cold` is the zero-hit points parsed from the run's `.dat`, and
nothing else. These cases hold it to that on every path that writes
`coveragesummary.json`, so the answer cannot depend on how the module was launched.

The fixture below is written by hand from the Verilator `.dat` field layout
(`'count' 'file' 'line' 'col' 'hier' 'comment'`; empty comment = line point,
`b<n>` = branch point). Expected values are worked out on paper from its counts.

Fixture, one point per row (count, file:line:col, kind):

    0  alu.v:30:5  line      cold
    0  alu.v:12:3  line      cold
    7  alu.v:20:1  line      HIT   -> excluded
    0  dec.v:8:2   line      cold
    0  alu.v:15:9  branch    cold
    4  dec.v:9:1   branch    HIT   -> excluded

Zero-count points: three lines and one branch. Lines sort by (file, line, col):
alu.v:12, alu.v:30, dec.v:8. Branches follow the lines: alu.v:15. So

    top_cold == [alu.v:12, alu.v:30, dec.v:8, alu.v:15]

The all-hit fixture has no zero count, so its answer is `[]`.

The subprocess cases launch a fresh interpreter with cwd = repo root and no
inherited PYTHONPATH, which is how a real run starts; `pyproject.toml`'s
`pythonpath` (it lists `AGENT_F`) would otherwise hide the root-run behaviour,
as in #41. The in-process case is the pytest world.
"""
import json
import os
import pathlib
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

DAT_COLD = """\
# verilator 5.020 coverage
'0' 'alu.v' '30' '5' 'top.alu' ''
'0' 'alu.v' '12' '3' 'top.alu' ''
'7' 'alu.v' '20' '1' 'top.alu' ''
'0' 'dec.v' '8' '2' 'top.dec' ''
'0' 'alu.v' '15' '9' 'top.alu' 'b0'
'4' 'dec.v' '9' '1' 'top.dec' 'b1'
"""

DAT_ALL_HIT = """\
# verilator 5.020 coverage
'3' 'alu.v' '12' '3' 'top.alu' ''
'7' 'alu.v' '20' '1' 'top.alu' ''
'2' 'alu.v' '15' '9' 'top.alu' 'b0'
"""

EXPECTED_COLD = [("alu.v", 12), ("alu.v", 30), ("dec.v", 8), ("alu.v", 15)]


def _clean_env():
    return {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}


def _make_run(tmp_path, dat_text):
    rundir = tmp_path / "run"
    raw = rundir / "outputs" / "coverageraw"
    raw.mkdir(parents=True)
    (raw / "coverage.dat").write_text(dat_text, encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"rundir": str(rundir), "run_id": "t"}))
    return rundir, manifest, raw / "coverage.dat"


def _cold_of(summary_path):
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    # Anti-vacuity: the dat was really parsed, so [] is a measurement.
    total = summary["raw_counts"]["line"]["total"] + summary["raw_counts"]["branch"]["total"]
    assert total > 0, f"nothing was parsed from the fixture: {summary}"
    return [(p["file"], p["line"]) for p in summary["top_cold_paths"]]


def _run(cmd, cwd, ok_rcs=(0,)):
    proc = subprocess.run(cmd, cwd=str(cwd), env=_clean_env(),
                          capture_output=True, text=True, timeout=180)
    assert proc.returncode in ok_rcs, f"{cmd} rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}"


def _manifest_mode_from_root(tmp_path, dat_text):
    rundir, manifest, _ = _make_run(tmp_path, dat_text)
    _run([sys.executable, "-m", "AGENT_F.coverage_pipeline", "--manifest", str(manifest)], REPO_ROOT)
    return _cold_of(rundir / "coveragesummary.json")


def _manifest_mode_as_script(tmp_path, dat_text):
    # Launched as a script: AGENT_F is sys.path[0], like pytest's pythonpath.
    rundir, manifest, _ = _make_run(tmp_path, dat_text)
    _run([sys.executable, str(REPO_ROOT / "AGENT_F" / "coverage_pipeline.py"),
          "--manifest", str(manifest)], REPO_ROOT)
    return _cold_of(rundir / "coveragesummary.json")


def _direct_mode_from_root(tmp_path, dat_text):
    _, _, dat = _make_run(tmp_path, dat_text)
    out = tmp_path / "direct_out"
    _run([sys.executable, "-m", "AGENT_F.coverage_pipeline",
          "--dat", str(dat), "--out", str(out)], REPO_ROOT,
         ok_rcs=(0, 1))  # direct mode exits 1 for a below-threshold grade, not an error
    return _cold_of(out / "coveragesummary.json")


def _manifest_mode_in_process(tmp_path, dat_text):
    # The pytest world: pyproject's pythonpath has AGENT_F on sys.path.
    assert any(p.endswith("AGENT_F") for p in sys.path), "pytest pythonpath changed; test is vacuous"
    import coverage_pipeline as cp
    rundir, manifest, _ = _make_run(tmp_path, dat_text)
    assert cp.main(["--manifest", str(manifest)]) == 0
    return _cold_of(rundir / "coveragesummary.json")


ROUTES = [
    pytest.param(_manifest_mode_from_root, id="manifest-from-repo-root"),
    pytest.param(_manifest_mode_as_script, id="manifest-as-script"),
    pytest.param(_manifest_mode_in_process, id="manifest-in-pytest"),
    pytest.param(_direct_mode_from_root, id="direct-from-repo-root"),
]


@pytest.mark.parametrize("route", ROUTES)
def test_top_cold_is_the_zero_hit_points_of_the_dat(route, tmp_path):
    got = route(tmp_path, DAT_COLD)
    assert got == EXPECTED_COLD, (
        "top_cold must be exactly the zero-count line points (sorted by file, "
        "line, column) followed by the zero-count branch points; the hit points "
        "alu.v:20 and dec.v:9 must not appear"
    )


@pytest.mark.parametrize("route", ROUTES)
def test_top_cold_is_empty_when_every_point_is_hit(route, tmp_path):
    assert route(tmp_path, DAT_ALL_HIT) == []


def test_every_route_gives_the_same_answer(tmp_path):
    answers = {}
    for i, route in enumerate(p.values[0] for p in ROUTES):
        d = tmp_path / str(i)
        d.mkdir()
        answers[route.__name__] = route(d, DAT_COLD)
    assert len({tuple(v) for v in answers.values()}) == 1, answers


def test_no_bare_coverage_database_import_in_the_pipeline():
    # A repo module must never be reached by bare name inside a swallowing
    # handler (#41, #65); the second definition of top_cold is gone.
    src = (REPO_ROOT / "AGENT_F" / "coverage_pipeline.py").read_text(encoding="utf-8")
    assert "from coverage_database import" not in src
