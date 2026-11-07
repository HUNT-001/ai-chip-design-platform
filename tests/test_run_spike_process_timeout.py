"""#120: run_spike_process must enforce its timeout while Spike's pipes stay open.

It read Spike's output to EOF before it ever waited with a timeout, so a Spike
that hung (or left a child holding the output pipe) blocked the caller forever
instead of producing the timeout result.  The contract, from the issue and the
callers: after ``timeout_s`` the call returns ``-9`` (the timeout sentinel) with
whatever output was produced so far.

The fake Spike is a Python script run by this interpreter, so the same case
runs on every platform.  Its parent and a background child it spawns (inheriting
stdout/stderr) both sleep 30 s.  The call runs in a worker thread and the test
waits a bounded time for it, so a regression fails the test instead of hanging
the suite, and every process the script started is killed afterwards.
"""
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time

import run_iss

HANG = textwrap.dedent("""
    import os, subprocess, sys, time
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    with open(sys.argv[1], "w", encoding="utf-8") as f:
        f.write(f"{os.getpid()} {child.pid}")
    print("started", flush=True)
    time.sleep(30)
""")

TALK = textwrap.dedent("""
    import sys
    print("out line", flush=True)
    print("err line", file=sys.stderr, flush=True)
    sys.exit(3)
""")


def _kill(pid):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                           capture_output=True, check=False)
        else:
            os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _call(cmd, log_file, timeout_s, bound_s):
    box = {}

    def work():
        box["result"] = run_iss.run_spike_process(cmd, log_file, timeout_s)

    t = threading.Thread(target=work, daemon=True)
    start = time.monotonic()
    t.start()
    t.join(bound_s)
    return box.get("result"), time.monotonic() - start, t.is_alive()


def test_timeout_is_enforced_while_a_child_holds_the_output_pipe(tmp_path):
    script = tmp_path / "fake_spike.py"
    script.write_text(HANG, encoding="utf-8")
    pids = tmp_path / "pids.txt"
    try:
        result, elapsed, still_running = _call(
            [sys.executable, str(script), str(pids)],
            tmp_path / "iss.log", timeout_s=1, bound_s=10)
    finally:
        if pids.exists():
            for pid in pids.read_text(encoding="utf-8").split():
                _kill(int(pid))
    assert not still_running, "run_spike_process was still blocked after 10 s"
    rc, output = result
    assert rc == -9
    assert "started" in output
    assert elapsed < 10


def test_normal_output_and_nonzero_exit_come_back_unchanged(tmp_path):
    script = tmp_path / "fake_spike.py"
    script.write_text(TALK, encoding="utf-8")
    log_file = tmp_path / "iss.log"
    result, _elapsed, still_running = _call(
        [sys.executable, str(script)], log_file, timeout_s=30, bound_s=60)
    assert not still_running
    assert result == (3, "out line\nerr line\n")
    assert log_file.read_text(encoding="utf-8") == "out line\nerr line\n"
