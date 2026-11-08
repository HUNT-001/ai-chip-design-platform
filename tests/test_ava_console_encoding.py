"""#147: ava_patched's exit code is the verdict; the console cannot change it.

Exit-code contract: completed/0 verified, failed/1 a violation, incomplete/3
could not verify.  With no toolchain on PATH the run is incomplete/3.  On a
console that cannot encode the summary's characters (Windows cp1252, or ASCII)
the final print used to raise UnicodeEncodeError, which exited 1: "could not
verify" read as "violation".

The ASCII run must not be turned into a UTF-8 one by the test, which is what
hid the same bug in #144.
"""
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

RTL = """\
module demo_core (
    input  wire        clk, rst_n,
    input  wire [31:0] instr_i,
    output reg  [31:0] data_o
);
    reg [31:0] pc;
endmodule
"""


def _env(encoding):
    env = {"PATH": "/nonexistent", "PYTHONIOENCODING": encoding}
    # Windows cannot start Python without SYSTEMROOT; keep only that.
    if os.environ.get("SYSTEMROOT"):
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    return env


def _run(tmp_path, encoding):
    rtl = tmp_path / "demo_core.sv"
    rtl.write_text(RTL, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "ava_patched.py"), "--rtl", str(rtl),
         "--no-llm", "--seed", "42", "--run-dir", str(tmp_path / ("run_" + encoding))],
        cwd=REPO_ROOT, env=_env(encoding), capture_output=True,
        encoding="utf-8", errors="replace", timeout=600)


def test_the_probe_really_has_an_ascii_console():
    """Anti-vacuity: without this a UTF-8 child would pass for the wrong reason."""
    r = subprocess.run([sys.executable, "-c", "import sys; print(sys.stdout.encoding)"],
                       env=_env("ascii"), capture_output=True, text=True, timeout=60)
    assert r.stdout.strip().lower() in ("ascii", "us-ascii"), r.stdout


def test_a_run_that_could_not_verify_exits_3_on_any_console(tmp_path):
    utf8 = _run(tmp_path, "utf-8")
    assert utf8.returncode == 3, (
        f"control: the UTF-8 run must be incomplete/3, got {utf8.returncode}: "
        + utf8.stderr[-400:])
    ascii_ = _run(tmp_path, "ascii")
    assert ascii_.returncode == 3, (
        f"UTF-8 exits 3 but the ASCII console exits {ascii_.returncode}: "
        + (ascii_.stdout + ascii_.stderr)[-500:])
    assert "UnicodeEncodeError" not in ascii_.stderr, ascii_.stderr[-600:]
