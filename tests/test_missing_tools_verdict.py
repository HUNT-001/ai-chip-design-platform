"""#138: a run that could not observe anything must not say it verified.

The contract: completed/0 verified, failed/1 a violation, incomplete/3 could
not verify.  With the RTL binary and Spike unavailable the simulators return
stubs with empty commit logs, so nothing was compared.  That is "could not
verify", not "verified".

The real CLI is run as a subprocess from the repo root, with a PATH that
cannot resolve any tool, so tool resolution is not stubbed.
"""
import asyncio
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import ava_patched
from ava_patched import AVA, SpikeISS

RTL = """\
module demo_core (
    input  wire        clk, rst_n,
    input  wire [31:0] instr_i,
    output reg  [31:0] data_o
);
    reg [31:0] pc;
endmodule
"""


def _cli(tmp_path, extra=()):
    rtl = tmp_path / "demo_core.sv"
    rtl.write_text(RTL, encoding="utf-8")
    env = {"PATH": "/nonexistent"}
    # Windows cannot start Python without SYSTEMROOT (hash randomisation needs
    # it); keep only that, never anything that could resolve a tool.
    if os.environ.get("SYSTEMROOT"):
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    # anti-vacuity: the probe really cannot see the tools it claims are missing
    assert shutil.which("spike", path=env["PATH"]) is None
    assert shutil.which("yosys", path=env["PATH"]) is None
    p = subprocess.run(
        [sys.executable, str(REPO_ROOT / "ava_patched.py"), "--rtl", str(rtl),
         "--no-llm", "--seed", "42", "--run-dir", str(tmp_path / "run"), *extra],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=600)
    out = p.stdout + p.stderr
    m = re.search(r"^\s*Status\s*:\s*(\S+)", out, re.M)
    return (m.group(1) if m else None), p.returncode, out


def test_cli_with_no_tools_is_incomplete(tmp_path):
    status, rc, out = _cli(tmp_path)
    assert "returning stub" in out or "returning ISS stub" in out, (
        "the run did not hit the missing-tool path; this test would be vacuous")
    assert (status, rc) == ("incomplete", 3), (
        f"nothing was simulated or compared, yet status={status!r} exit={rc}")


def test_cli_with_no_tools_is_incomplete_without_extended_agents(tmp_path):
    status, rc, _ = _cli(tmp_path, ["--no-extended"])
    assert (status, rc) == ("incomplete", 3)


def test_a_stubbed_simulator_makes_the_run_incomplete(tmp_path, monkeypatch):
    """Sensitivity at the source: only the `stub` flag differs between the runs."""
    def run(stub):
        async def _rtl(self, tb_suite, semantic_map):
            return {"cycles": 0, "instructions": 0, "coverage_data": {},
                    "performance": {"ipc": 0.0, "branch_predictions": 0,
                                    "branch_correct": 0},
                    "commit_log": [], "state_snapshots": [], "seed": 1,
                    "stub": stub}
        async def _iss(self, semantic_map, stimulus, tb_suite):
            return {"instructions": 0, "commit_log": [], "seed": 1,
                    "stub": stub}
        monkeypatch.setattr(SpikeISS, "_simulate_rtl", _rtl)
        monkeypatch.setattr(SpikeISS, "_simulate_iss", _iss)
        ava = AVA(enable_llm=False, timeout=30, target_coverage=90.0,
                  run_base_dir=str(tmp_path / f"s{stub}"), enable_database=False)
        return asyncio.run(ava.generate_suite(
            rtl_spec=RTL, microarch="in_order", seed=42, save_results=False))

    assert run(True)["status"] == "incomplete"
