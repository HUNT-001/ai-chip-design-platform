"""#147: a CLI's exit code is the verdict; the console encoding cannot change it.

Every script that has an `if __name__ == "__main__"` block is found by a static
scan of tracked files (never by running it).  The product CLIs (AGENT_* and the
repo root) are run in a temp cwd, once under PYTHONIOENCODING=utf-8 and once
under ascii, and must give the same exit code and no UnicodeEncodeError.  Most
are driven only to `--help`, whose output can already contain non-ASCII text;
the ones driven to a real final print are in DEEP.

Nothing here is trusted to be exhaustive on its own, so every discovered script
must be driven (DEEP or --help), listed in UNREACHED (the final print is not
reached; reason given), or listed in EXCLUDED (never executed; reason given).
A new CLI that is in none of those fails the test.
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_RE = re.compile(r"^if __name__\s*==\s*['\"]__main__['\"]\s*:", re.M)
SKIP_PREFIXES = ("corpus/", "_legacy/", "tests/")

#: Scripts found today under the rules above.  A smaller count means discovery broke.
DISCOVERED_FLOOR = 114

#: In-scope scripts driven to a real final print.  "{tmp}" is the temp cwd.
DEEP = {
    "AGENT_D/bug_hypothesis.py": ["--self-test"],
    "AGENT_D/compare_commitlogs.py": ["--self-test"],
    "AGENT_F/manifest_contract.py": ["{tmp}/m.json"],
    "AGENT_F/ava_coverage_patch.py": ["--dry-run", "{tmp}/ava_copy.py"],
    "AGENT_F/cold_path_ranker.py": ["--db", "{tmp}/cov.db", "--top", "3", "--constraints-only"],
    "AGENT_G/generate_tests.py": ["--directed", "1", "--random", "1", "--no-assemble",
                                  "--outdir", "{tmp}/g"],
}

#: In-scope scripts driven only to --help: the final print is NOT reached.
UNREACHED = {
    'AGENT_A/semantic_analyzer.py': '--help only; its final print needs inputs this test does not build',
    'AGENT_B/backends/run_rtl.py': 'needs a Verilator build and an ELF to reach the Done prints',
    'AGENT_B/testbench_generator.py': '--help only; its final print needs inputs this test does not build',
    'AGENT_C/iss_efficiency.py': '--help only; its final print needs inputs this test does not build',
    'AGENT_C/run_iss.py': 'needs Spike and an ELF to reach the DONE print',
    'AGENT_E/run_compliance.py': '--help only; its final print needs inputs this test does not build',
    'AGENT_E/run_rtl_adapter.py': '--help only; its final print needs inputs this test does not build',
    'AGENT_F/coverage_database.py': '--help only; its final print needs inputs this test does not build',
    'AGENT_F/coverage_pipeline.py': 'needs a coverage.dat run to reach the plateau warning',
    'AGENT_H/advanced_link_verifier.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/aes_verifier.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/aia_verifier.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/atomics_verifier.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/bitmanip_verifier.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/branch_predictor_verifier.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/bug_intelligence.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/bus_verifier.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/cache_verifier.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/cas_verifier.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/cdc_verifier.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/coherence_verifier.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/coverage_collector.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/crypto_verifier.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/csr_verifier.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/dashboard.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/debug_verifier.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/demo_traces.py': 'writes trace files and treats --help as an output dir; only --help (in a temp cwd) is driven',
    'AGENT_H/equivalence_verifier.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/failure_analytics.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/fault_injector.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/fp_verifier.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/hypervisor_verifier.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/interconnect_verifier.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/interrupt_verifier.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/lsq_verifier.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/memory_model_verifier.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/ooo_verifier.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/perf_counter_verifier.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/peripheral_verifier.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/pipeline_verifier.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/power_verifier.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/privilege_verifier.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/regression_intelligence.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/reset_verifier.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/rtl_basics_verifier.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/rtl_graph.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/rv64_atomics_verifier.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/rv64_verifier.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/rvc_verifier.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/self_evolving_engine.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/sm4_verifier.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/soc_peripheral_verifier.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/stimulus_generator.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/sv_mmu_verifier.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/tlb_verifier.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/vaes_verifier.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/vaeskf_verifier.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/vector_verifier.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/verification_twin.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/vghash_verifier.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/vm_verifier.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/vsha_verifier.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/vsm3_verifier.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_H/vsm4_verifier.py': 'verifier/analysis CLI: --help only; its final verdict print needs a run manifest or trace set',
    'AGENT_I/agent_i_litmus.py': '--help only; its final print needs inputs this test does not build',
    'AGENT_J/agent_j_cdc.py': '--help only; its final print needs inputs this test does not build',
    'AGENT_K/agent_k_perf.py': '--help only; its final print needs inputs this test does not build',
    'AGENT_L/agent_l_equiv.py': '--help only; its final print needs inputs this test does not build',
    'ava_patched.py': '--help only here; the verdict print is driven by tests/test_ava_console_encoding.py',
}

#: Never executed (static scan only).  Research track: see #149.
EXCLUDED = {
    'phase3/engineer.py': 'research track, VOE on hold (#149); never executed',
    'phase3/evidence_channels.py': 'research track, VOE on hold (#149); never executed',
    'scripts/acceptance_e2e.py': 'repo tooling: runs four full pipeline runs on any invocation; not driven',
    'voe/run_voe.py': 'research track, VOE on hold (#149); never executed',
    'voe/scan_corpus.py': 'ignores --help and runs its experiment (#149); never executed',
    'voe_bench/record_decision.py': 'research track, VOE on hold (#149); never executed',
    'voe_bench/run_ablation.py': 'research track, VOE on hold (#149); never executed',
    'voe_bench/run_benchmark.py': 'research track, VOE on hold (#149); never executed',
    'voe_bench/run_controlled.py': 'research track, VOE on hold (#149); never executed',
    'voe_bench/run_coupled_noise.py': 'research track, VOE on hold (#149); never executed',
    'voe_bench/run_coupled_real.py': 'research track, VOE on hold (#149); never executed',
    'voe_bench/run_coupled_two.py': 'research track, VOE on hold (#149); never executed',
    'voe_bench/run_default.py': 'research track, VOE on hold (#149); never executed',
    'voe_bench/run_default_cached.py': 'research track, VOE on hold (#149); never executed',
    'voe_bench/run_multistep.py': 'research track, VOE on hold (#149); never executed',
    'voe_bench/run_prereg.py': 'research track, VOE on hold (#149); never executed',
    'voe_bench/run_stoch_transfer.py': 'research track, VOE on hold (#149); never executed',
    'voe_coupled/run_coupled.py': 'research track, VOE on hold (#149); never executed',
    'voe_fifo/run_voe_fifo.py': 'research track, VOE on hold (#149); never executed',
    'voe_heldout/run_heldout_experiment.py': 'research track, VOE on hold (#149); never executed',
    'voe_heldout/run_multidesign_experiment.py': 'research track, VOE on hold (#149); never executed',
    'voe_hetero/run_experiment3.py': 'research track, VOE on hold (#149); never executed',
    'voe_hetero/run_experiment4a.py': 'research track, VOE on hold (#149); never executed',
    'voe_horizon/run_horizon.py': 'research track, VOE on hold (#149); never executed',
    'voe_hostile/run_hostile_experiment.py': 'research track, VOE on hold (#149); never executed',
    'voe_ibex/formal/show_cex.py': 'research track, VOE on hold (#149); never executed',
    'voe_ibex/run_impact_demo.py': 'research track, VOE on hold (#149); never executed',
    'voe_ibex/run_policy_experiment.py': 'research track, VOE on hold (#149); never executed',
    'voe_ibex/run_voe_generated.py': 'research track, VOE on hold (#149); never executed',
    'voe_ibex/run_voe_ibex.py': 'research track, VOE on hold (#149); never executed',
    'voe_ibex/run_voe_specialists.py': 'research track, VOE on hold (#149); never executed',
    'voe_stoch/characterise.py': 'research track, VOE on hold (#149); never executed',
    'voe_stoch2/characterise.py': 'ignores --help and runs its experiment (#149); never executed',
    'voe_stoch3/characterise.py': 'research track, VOE on hold (#149); never executed',
    'voe_stoch3/commit_s2_shape.py': 'research track, VOE on hold (#149); never executed',
    'voe_stoch3/power_s2.py': 'ignores --help and runs its experiment (#149); never executed',
    'voe_stoch3/record_s2_shape.py': 'research track, VOE on hold (#149); never executed',
    'voe_stoch3/run_s2_shape.py': 'research track, VOE on hold (#149); never executed',
    'voe_stoch3/theory.py': 'research track, VOE on hold (#149); never executed',
}


def discover():
    out = subprocess.run(["git", "ls-files", "*.py"], cwd=REPO_ROOT, capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-300:]
    found = []
    for rel in out.stdout.splitlines():
        name = rel.rsplit("/", 1)[-1]
        if rel.startswith(SKIP_PREFIXES) or name.startswith("test_"):
            continue
        try:
            text = (REPO_ROOT / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if MAIN_RE.search(text):
            found.append(rel)
    return sorted(found)


DISCOVERED = discover()


def in_scope(rel):
    return rel.startswith("AGENT_") or "/" not in rel


IN_SCOPE = [p for p in DISCOVERED if in_scope(p)]


def _env(encoding):
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    env["PYTHONIOENCODING"] = encoding
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _build_coverage_db(path):
    """One never-hit coverage point, so the ranker has a cold path to print."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from AGENT_F.coverage_database import CoverageDatabase
    with CoverageDatabase(path) as db:
        db._conn.execute(
            "INSERT INTO coverage_points (run_id, module, line, col, hier, kind, description,"
            " hit_count, total_executions) VALUES (?,?,?,?,?,?,?,?,?)",
            ("r1", "alu.sv", 10, 0, "top.alu", "branch", "branch@alu.sv:10", 0, 0))
        db._conn.commit()


def _workdir(tmp_path):
    (tmp_path / "m.json").write_text("{}", encoding="utf-8")
    shutil.copy(REPO_ROOT / "ava_patched.py", tmp_path / "ava_copy.py")
    if not (tmp_path / "cov.db").exists():
        _build_coverage_db(str(tmp_path / "cov.db"))
    return tmp_path


def _probe(rel, tmp_path, encoding):
    wd = _workdir(tmp_path)
    args = [a.replace("{tmp}", str(wd)) for a in DEEP.get(rel, ["--help"])]
    try:
        p = subprocess.run([sys.executable, str(REPO_ROOT / rel), *args], cwd=wd,
                           env=_env(encoding), capture_output=True, stdin=subprocess.DEVNULL,
                           timeout=120)
    except subprocess.TimeoutExpired:
        pytest.fail(f"{rel} did not return on {args}: it must be listed in EXCLUDED, "
                    "not driven by this test")
    out = p.stdout + p.stderr
    return p.returncode, out


def test_discovery_found_what_it_found_before():
    assert len(DISCOVERED) >= DISCOVERED_FLOOR, (
        f"discovery found {len(DISCOVERED)}, expected at least {DISCOVERED_FLOOR}")


def test_every_discovered_script_is_accounted_for():
    problems = []
    for rel in DISCOVERED:
        if in_scope(rel):
            if rel not in DEEP and rel not in UNREACHED:
                problems.append(f"{rel}: in scope but neither DEEP nor UNREACHED")
        elif rel not in EXCLUDED:
            problems.append(f"{rel}: not an AGENT_*/root script and not in EXCLUDED")
    for table, name in ((DEEP, "DEEP"), (UNREACHED, "UNREACHED"), (EXCLUDED, "EXCLUDED")):
        for rel in table:
            if rel not in DISCOVERED:
                problems.append(f"{rel}: listed in {name} but no longer has a __main__ block")
    for rel in set(DEEP) & set(UNREACHED):
        problems.append(f"{rel}: in both DEEP and UNREACHED")
    assert not problems, "\n".join(problems)


def test_the_probe_really_has_an_ascii_console():
    """Anti-vacuity: a UTF-8 child would pass every case below for the wrong reason."""
    r = subprocess.run([sys.executable, "-c", "import sys; print(sys.stdout.encoding)"],
                       env=_env("ascii"), capture_output=True, text=True, timeout=60)
    assert r.stdout.strip().lower() in ("ascii", "us-ascii"), r.stdout


@pytest.mark.parametrize("rel", sorted(DEEP))
def test_the_deep_drives_really_print_non_ascii(rel, tmp_path):
    """Anti-vacuity: a DEEP drive that never prints a non-ASCII character proves nothing."""
    _, out = _probe(rel, tmp_path, "utf-8")
    assert any(b > 127 for b in out), f"{rel}: the utf-8 run printed only ASCII"


@pytest.mark.parametrize("rel", IN_SCOPE)
def test_exit_code_does_not_depend_on_console_encoding(rel, tmp_path):
    (tmp_path / "u").mkdir()
    (tmp_path / "a").mkdir()
    rc_utf8, _ = _probe(rel, tmp_path / "u", "utf-8")
    rc_ascii, out = _probe(rel, tmp_path / "a", "ascii")
    assert b"UnicodeEncodeError" not in out, (
        f"{rel}: the ascii console raised UnicodeEncodeError: " + out[-400:].decode("utf-8", "replace"))
    assert rc_ascii == rc_utf8, (
        f"{rel}: exit {rc_utf8} under utf-8 but {rc_ascii} under ascii: "
        + out[-400:].decode("utf-8", "replace"))
