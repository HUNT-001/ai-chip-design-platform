"""#144: AGENT_D's exit code is the verdict; the console cannot change it.

Exit-code contract (compare_commitlogs.py): 0 clean, 1 mismatch.  On a console
that cannot encode the summary's characters (Windows cp1252, or ASCII) the
CLI used to raise UnicodeEncodeError after the comparison finished, which
exited 1: a clean pair read as a mismatch.

The environment here forces an ASCII console and must not pin UTF-8, which is
exactly what hid this.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = str(REPO_ROOT / "AGENT_D" / "compare_commitlogs.py")


def _env():
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    env["PYTHONIOENCODING"] = "ascii"
    return env


@pytest.fixture(scope="module")
def samples(tmp_path_factory):
    d = tmp_path_factory.mktemp("samples")
    # Setup only: the inputs are made on a UTF-8 console so the red/green below
    # comes from the comparison, never from fixture generation.  The subject
    # under test (`_run`) always runs on ASCII.
    setup_env = {**_env(), "PYTHONIOENCODING": "utf-8"}
    r = subprocess.run([sys.executable, CLI, "--generate-sample-logs",
                        "--sample-dir", str(d)],
                       cwd=REPO_ROOT, env=setup_env, capture_output=True, text=True,
                       timeout=120)
    assert (d / "rtl_pass.commitlog.jsonl").exists(), r.stderr[-500:]
    return d


def _run(samples, rtl, iss):
    return subprocess.run(
        [sys.executable, CLI, str(samples / rtl), str(samples / iss)],
        cwd=REPO_ROOT, env=_env(), capture_output=True, text=True, timeout=120)


def test_the_probe_really_has_an_ascii_console():
    """Anti-vacuity: without this, a UTF-8 child would pass for the wrong reason."""
    r = subprocess.run([sys.executable, "-c", "import sys; print(sys.stdout.encoding)"],
                       env=_env(), capture_output=True, text=True, timeout=60)
    assert r.stdout.strip().lower() in ("ascii", "us-ascii"), r.stdout


def test_a_clean_comparison_exits_0_on_an_ascii_console(samples):
    r = _run(samples, "rtl_pass.commitlog.jsonl", "iss_pass.commitlog.jsonl")
    assert "Traceback" not in r.stderr, r.stderr[-600:]
    assert r.returncode == 0, f"clean pair exited {r.returncode}: {r.stderr[-300:]}"


def test_a_mismatch_exits_1_and_reports_it_on_an_ascii_console(samples):
    r = _run(samples, "rtl_fail_mem.commitlog.jsonl", "iss_fail_mem.commitlog.jsonl")
    assert "Traceback" not in r.stderr, (
        "the exit code is 1 either way here; a crash must not stand in for the report: "
        + r.stderr[-600:])
    assert r.returncode == 1
    assert "MEMMISMATCH" in r.stdout


def test_the_two_verdicts_differ(samples):
    """The pair of controls above only means something if they disagree."""
    clean = _run(samples, "rtl_pass.commitlog.jsonl", "iss_pass.commitlog.jsonl")
    bad = _run(samples, "rtl_fail_mem.commitlog.jsonl", "iss_fail_mem.commitlog.jsonl")
    assert clean.returncode != bad.returncode
