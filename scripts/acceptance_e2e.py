#!/usr/bin/env python3
"""End-to-end acceptance: does the verdict move when the evidence moves?

Runs ava_patched.py as a subprocess from the repo root on four cases and
prints one table.  Expectations come from the verdict contract, not from
observed output:

    completed / 0   verified
    failed    / 1   a violation was found
    incomplete/ 3   could not verify

Cases
  a  clean run                              -> completed / 0
  b  corrupted store (comparator mismatch)  -> failed    / 1, comparator label
  c  required tool missing (scrubbed PATH)  -> incomplete/ 3
  d  clean run again                        -> identical to a

How the evidence is supplied.  The CLI has no flag to feed commit logs, so
a, b and d enter through ava_patched.main() (the CLI's own entry point) in a
child interpreter that stubs only the two simulator methods, exactly as
tests/test_comparator_verdict.py does.  The sole difference between a and b
is one corrupted store in the RTL log (the docs/media/drivers/cmp_snippet.py
input: 0xbadbad00 where the ISS wrote 0x0000002a).  Case c is the plain,
unstubbed CLI with PATH scrubbed.

Local-only: needs the repo's Python and no EDA tools; CI has no toolchain
and cannot run the tool-dependent cases.  Exit 0 only if every expectation
and every anti-vacuity check holds.
"""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

RTL = """\
module demo_core (
    input  wire        clk, rst_n,
    input  wire [31:0] instr_i,
    output reg  [31:0] data_o
);
    reg [31:0] pc;
endmodule
"""

# Child: stub the simulators, then enter through the CLI's own main().
SHIM = r"""
import sys
sys.path.insert(0, sys.argv[1])
import ava_patched
from ava_patched import SpikeISS
CORRUPT = sys.argv[2] == "corrupt"
N = 20
def entry(i, data):
    return {"pc": "0x%08x" % (0x80000000 + 4 * i), "instr": "0x00a12023",
            "regs": {}, "csrs": {},
            "mem_writes": [{"addr": "0x80001000", "size": 4, "data": data}]}
iss_log = [entry(i, "0x0000002a") for i in range(N)]
rtl_log = [entry(i, "0xbadbad00" if (CORRUPT and i == 7) else "0x0000002a")
           for i in range(N)]
async def _rtl(self, tb_suite, semantic_map):
    return {"cycles": 1000, "instructions": N,
            "coverage_data": {"lines_hit": 196, "total_lines": 200,
                              "branches_hit": 94, "total_branches": 100},
            "performance": {"ipc": 0.9, "branch_predictions": 100,
                            "branch_correct": 95},
            "commit_log": [dict(e) for e in rtl_log],
            "state_snapshots": [], "seed": 1}
async def _iss(self, semantic_map, stimulus, tb_suite):
    return {"instructions": N, "commit_log": [dict(e) for e in iss_log],
            "seed": 1}
SpikeISS._simulate_rtl = _rtl
SpikeISS._simulate_iss = _iss
sys.argv = ["ava_patched.py"] + sys.argv[3:]
sys.exit(ava_patched.main())
"""

EXPECT = {  # from the contract
    "a": ("completed", 0),
    "b": ("failed", 1),
    "c": ("incomplete", 3),
    "d": ("completed", 0),
}


def parse(out):
    st = re.search(r"^\s*Status\s*:\s*(\S+)", out, re.M)
    vi = re.search(r"^\s*Violations in\s*:\s*(.+)$", out, re.M)
    return (st.group(1) if st else "?",
            vi.group(1).strip() if vi else "-")


def run(case, work, rtl):
    base_env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    common = ["--rtl", str(rtl), "--no-llm", "--seed", "42",
              "--run-dir", str(work / ("run_" + case))]
    if case == "c":
        # scrubbed PATH: no verilator, spike, yosys, gcc
        env = {"PATH": "/nonexistent"}
        cmd = [sys.executable, str(REPO / "ava_patched.py")] + common
    else:
        env = base_env
        mode = "corrupt" if case == "b" else "clean"
        cmd = [sys.executable, "-c", SHIM, str(REPO), mode] + common
    p = subprocess.run(cmd, cwd=REPO, env=env, capture_output=True,
                       text=True, timeout=600)
    status, labels = parse(p.stdout + p.stderr)
    return status, p.returncode, labels


def main():
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        rtl = work / "demo_core.sv"
        rtl.write_text(RTL, encoding="utf-8")
        res = {c: run(c, work, rtl) for c in "abcd"}

    print(f"{'case':<5}| {'status':<11}| {'exit':<5}| violation labels")
    print("-" * 60)
    for c in "abcd":
        s, rc, lab = res[c]
        print(f"{c:<5}| {s:<11}| {rc:<5}| {lab}")

    fails = []
    for c, (es, erc) in EXPECT.items():
        s, rc, lab = res[c]
        if (s, rc) != (es, erc):
            fails.append(f"case {c}: expected {es}/{erc}, got {s}/{rc}")
    if "comparator" not in res["b"][2]:
        fails.append("case b: no comparator label in violations")
    if res["d"][:2] != res["a"][:2]:
        fails.append("case d: differs from case a")
    for c in "bc":
        if res[c][:2] == res["a"][:2]:
            fails.append(f"VACUOUS: case {c} matches the clean run "
                         f"({res['a'][0]}/{res['a'][1]}) and proves nothing")

    print()
    if fails:
        print("FAIL")
        for f in fails:
            print("  - " + f)
        return 1
    print("OK: every expectation holds and b, c differ from a")
    return 0


if __name__ == "__main__":
    sys.exit(main())
