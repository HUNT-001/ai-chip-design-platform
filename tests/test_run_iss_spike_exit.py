"""#104: a nonzero Spike exit must not be recorded as phases.iss.status "passed".

The Spike here is a script that prints valid commit-log lines and exits with a
chosen code, executed by this interpreter, so run_iss starts a real process and
reads its real exit status.  Expected values are written by hand from AGENT_A's
lifecycle_status: a partial log is not evidence of a DUT bug, so a failed Spike
is "infra_error" and the exit code is reported in the error text.
"""
import json
import os
import stat
import sys

import pytest

import run_iss

# Spike -l (FORMAT A) lines: five distinct (pc, instr) pairs.
RECORDS = (
    "core   0: 0x80000000 (0x00000297) auipc   t0, 0x0\n"
    "core   0: 0x80000004 (0x02028593) addi    a1, t0, 32\n"
    "core   0: 0x80000008 (0x00002223) sw      zero, 4(zero)\n"
    "core   0: 0x8000000c (0x30200073) mret\n"
    "core   0: 0x80000010 (0x00000013) nop\n"
)

pytestmark = pytest.mark.skipif(os.name == "nt", reason="stub is a shebang script")


def _stub_spike(tmp_path, exit_code):
    """An executable that, run as `spike --help`, prints help, and otherwise
    prints RECORDS and exits `exit_code`."""
    path = tmp_path / "stub_spike.py"
    path.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "if '--help' in sys.argv:\n"
        "    print('Spike RISC-V ISA Simulator 1.1.1 (stub)')\n"
        "    sys.exit(0)\n"
        f"sys.stderr.write({RECORDS!r})\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _run(tmp_path, exit_code):
    elf = tmp_path / "test.elf"
    elf.write_bytes(b"\x7fELF" + b"\x00" * 60)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "rundir": str(tmp_path / "run"), "binary": str(elf), "isa": "rv32im",
        "spikebin": str(_stub_spike(tmp_path, exit_code)),
    }), encoding="utf-8")
    rc = run_iss.run_iss_manifest(manifest)
    return rc, json.loads(manifest.read_text(encoding="utf-8"))["phases"]["iss"]


def test_control_a_stub_that_exits_zero_is_passed(tmp_path):
    rc, iss = _run(tmp_path, 0)
    assert iss["status"] == "passed"
    assert iss["commit_count"] == 5
    assert rc == run_iss.EXIT_PASS


@pytest.mark.parametrize("code", [1, 2])
def test_a_nonzero_spike_exit_is_infra_error_not_passed(tmp_path, code):
    rc, iss = _run(tmp_path, code)
    assert iss["status"] == "infra_error"
    assert f"exit={code}" in iss["error"]
    assert rc == run_iss.EXIT_INFRA
