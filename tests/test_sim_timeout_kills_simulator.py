"""A simulator that outlives the run's timeout (#156).

Written from the issue and from the exit-code contract at the top of
ava_patched.py (`EXIT_INCOMPLETE = 3`: a gating check could not verify).
A run that timed out did not verify anything, so the CLI must exit 3, not 1
(which means "violation found"), and the simulator it started must be gone
by the time the CLI returns.

The RTL simulator is a real child process. `_simulate_rtl` launches
`[sim_binary, "+seed=<n>", "+elf=...", "+coverage_file=..."]` with
`cwd=<run_dir>`; with `sim_binary` set to this Python, the interpreter runs
the script file named `+seed=<n>` in the run dir. That is one process, no
shell and no wrapper, so the PID it records is the process that must die,
on Linux and on Windows alike. Only Spike is stubbed (CI has none), and the
only orchestrator method wrapped is `_generate_tb_suite`, to say where the
DUT is: the CLI has no flag for that.
"""
import asyncio
import os
import signal
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import ava_patched
from ava_patched import AVA, EXIT_INCOMPLETE, SpikeISS

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

TIMEOUT_S = 3


def _alive(pid: int) -> bool:
    """True while `pid` is a running (or unreaped) process."""
    if sys.platform == "win32":
        import ctypes
        k32 = ctypes.windll.kernel32
        # SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION
        handle = k32.OpenProcess(0x00100000 | 0x1000, False, pid)
        if not handle:
            return False
        try:
            return k32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _kill(pid: int) -> None:
    try:
        os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    except OSError:
        pass


def test_a_timed_out_run_exits_incomplete_and_leaves_no_simulator(tmp_path, monkeypatch):
    pid_file = tmp_path / "dut.pid"
    dut = (
        "import os, time\n"
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    original = AVA._generate_tb_suite

    async def _tb_suite(self, *args, **kwargs):
        suite = await original(self, *args, **kwargs)
        suite["sim_binary"] = sys.executable
        run_dir = Path(self.spike_iss._run_dir)          # the cwd _simulate_rtl uses
        (run_dir / f"+seed={int(suite.get('seed', 1))}").write_text(dut, encoding="utf-8")
        return suite

    async def _iss(self, semantic_map, stimulus, tb_suite):
        return {"instructions": 0, "commit_log": [], "seed": 1}

    monkeypatch.setattr(AVA, "_generate_tb_suite", _tb_suite)
    monkeypatch.setattr(SpikeISS, "_simulate_iss", _iss)
    rtl = tmp_path / "core.sv"
    rtl.write_text(RTL, encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [
        "ava_patched.py", "--rtl", str(rtl), "--no-llm", "--seed", "42",
        "--timeout", str(TIMEOUT_S), "--run-dir", str(tmp_path / "runs")])

    pid = None
    try:
        started = time.monotonic()
        rc = asyncio.run(ava_patched._main_async())
        elapsed = time.monotonic() - started
        assert pid_file.exists(), "the stand-in simulator never started: nothing was tested"
        pid = int(pid_file.read_text(encoding="utf-8"))
        assert elapsed < 60, f"the run waited for the simulator to finish ({elapsed:.0f}s)"
        leaked = _alive(pid)
        assert (rc, leaked) == (EXIT_INCOMPLETE, False), (
            f"exit code {rc} (want {EXIT_INCOMPLETE}, incomplete); "
            f"simulator pid {pid} still running after the CLI returned: {leaked}")
    finally:
        if pid is not None and _alive(pid):
            _kill(pid)
