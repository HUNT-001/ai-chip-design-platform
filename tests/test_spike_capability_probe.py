"""A Spike without --signature is a missing tool, not a crash (#35).

The compliance runner needs ``spike --signature=<file>`` for every golden run.
A Spike build that lacks the option can do none of the runner's work, so the
run must stop before building anything and exit EXIT_TOOL (3) with a tool error
that names the problem. Before this was fixed, such a Spike passed the probe
(it exists and answers --version), every test was built and then failed, and
the run exited EXIT_CRASH (2) -- an infrastructure crash rather than "the tool
you gave me cannot do this".

These tests use stub executables, so they need no real Spike or toolchain and
run in CI -- unlike TestFullPipeline, which CI skips for lack of tools.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
RUNNER = REPO / "AGENT_E" / "run_compliance.py"

EXIT_TOOL = 3

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="stub tools are POSIX shell scripts; the probe logic is platform-independent",
)

# Answers --version and --help; rejects --signature the way Spike 1.1.1-dev does.
SPIKE_WITHOUT_SIGNATURE = """#!/bin/sh
for a in "$@"; do
  case "$a" in
    --version) echo "Spike RISC-V ISA Simulator 1.1.1-dev"; exit 0 ;;
    --signature*) echo "spike: unrecognized option $a" >&2
                  echo "Try 'spike --help' for more information." >&2; exit 1 ;;
    --help|-h) echo "usage: spike [host options] <target program> [target options]"
               echo "  --isa=<name>          RISC-V ISA string"
               echo "  --log-commits         Generate a log of commits info"
               exit 0 ;;
  esac
done
exit 0
"""

# Same, but supports --signature (and says so in --help).
SPIKE_WITH_SIGNATURE = """#!/bin/sh
for a in "$@"; do
  case "$a" in
    --version) echo "Spike RISC-V ISA Simulator 1.1.1"; exit 0 ;;
    --help|-h) echo "usage: spike [host options] <target program> [target options]"
               echo "  --isa=<name>          RISC-V ISA string"
               echo "  --signature=<file>    Auto-detect the signature region and write it"
               exit 0 ;;
  esac
done
exit 0
"""

# Records that it was invoked, then fails: any call means the run built something.
TOOLCHAIN_STUB = """#!/bin/sh
echo "$0 $*" >> "{marker}"
exit 1
"""


def _write_exe(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run(tmp_path: Path, spike_script: str):
    """Run the compliance CLI with stub tools. Returns (proc, marker, reports)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    marker = tmp_path / "toolchain_invoked.log"
    spike = bindir / "spike"
    _write_exe(spike, spike_script)
    # Cover every prefix the runner probes, so no real compiler on PATH is used.
    for pfx in ("riscv32-unknown-elf-", "riscv64-unknown-elf-",
                "riscv-none-elf-", "riscv-none-embed-"):
        for tool in ("gcc", "objdump"):
            _write_exe(bindir / f"{pfx}{tool}", TOOLCHAIN_STUB.format(marker=marker))

    out_dir = tmp_path / "out"
    env = dict(os.environ, PATH=f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    proc = subprocess.run(
        [sys.executable, str(RUNNER), "--isa", "RV32IM",
         "--spike", str(spike), "--out-dir", str(out_dir)],
        capture_output=True, text=True, timeout=300, env=env,
    )

    reports = []
    if out_dir.exists():
        for p in sorted(out_dir.rglob("*.json")):
            try:
                data = json.loads(p.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if isinstance(data, dict) and "tool_errors" in data.get("summary", {}):
                reports.append(data)
    return proc, marker, reports


def test_spike_without_signature_is_a_tool_error(tmp_path):
    proc, marker, reports = _run(tmp_path, SPIKE_WITHOUT_SIGNATURE)
    detail = f"stdout:\n{proc.stdout[-1500:]}\nstderr:\n{proc.stderr[-1500:]}"

    assert proc.returncode == EXIT_TOOL, \
        f"exit {proc.returncode}, expected EXIT_TOOL ({EXIT_TOOL})\n{detail}"
    assert not marker.exists(), (
        "the run invoked the toolchain although Spike cannot produce signatures:\n"
        + marker.read_text()[:500])
    assert reports, f"no JSON report with summary.tool_errors was written\n{detail}"
    errors = reports[0]["summary"]["tool_errors"]
    assert any("signature" in e for e in errors), \
        f"tool_errors do not name --signature: {errors}"


def test_spike_with_signature_is_not_a_tool_error(tmp_path):
    """Control: the probe must not reject a Spike that does support --signature.

    Without this, a probe that always reported "no signature support" would
    satisfy the test above.
    """
    proc, marker, reports = _run(tmp_path, SPIKE_WITH_SIGNATURE)
    for r in reports:
        assert not any("signature" in e for e in r["summary"]["tool_errors"]), \
            f"a capable Spike was reported as a tool error: {r['summary']['tool_errors']}"
    # With a usable Spike the run gets as far as building (the stub compiler fails).
    assert marker.exists(), "the run never reached the build step"
    assert proc.returncode != EXIT_TOOL
