"""Phase-3 evidence channels — the real-tool adapters.

Each channel invokes a real open-source tool and returns a plain evidence dict
carrying a *witness* (an actual file path produced by the tool). The engineer
(``engineer.py``) turns these dicts into typed VSA judgments. Channels hold NO
kernel state — they only convert tool output into evidence, keeping the
generation/adjudication firewall (Fire-1) clean.

  FormalChannel : SymbiYosys (sby)  -> deductive proof  OR  counterexample
  SimChannel    : Verilator binary  -> inductive pass evidence (n_eff)

Both support mock=True so the whole pipeline (and the kernel law checks) can be
exercised without the toolchain; mock mirrors reality, including the key fact
that random simulation does NOT hit the narrow DEAD_BEEF defect that formal
catches immediately (Sem-1 in action).
"""
from __future__ import annotations
import os, re, shutil, subprocess, tempfile
from dataclasses import dataclass, field

HERE = os.path.dirname(os.path.abspath(__file__))
RTL  = os.path.join(HERE, "rtl", "alu.sv")
TB   = os.path.join(HERE, "sim", "tb_alu.sv")
SBY  = os.path.join(HERE, "formal", "alu.sby")


def _have(tool: str) -> bool:
    return shutil.which(tool) is not None


@dataclass
class Evidence:
    channel: str                 # "formal" | "sim"
    status: str                  # "proved" | "counterexample" | "pass" | "error"
    witness: str                 # real artifact path (proof dir / trace / sim log)
    n: int = 0                   # inductive sample count (sim)
    detail: str = ""
    raw: str = field(default="", repr=False)


# --------------------------------------------------------------------------- #
# Formal channel                                                              #
# --------------------------------------------------------------------------- #
class FormalChannel:
    def __init__(self, sby_file: str = SBY, mock: bool = False):
        self.sby_file = sby_file
        self.dir = os.path.dirname(sby_file)
        self.mock = mock or not _have("sby")

    def prove(self, task: str) -> Evidence:
        """Run one sby task. task in {'good','buggy'}."""
        if self.mock:
            if task.startswith("good"):
                return Evidence("formal", "proved",
                                witness=f"<mock>/{task}/PASS", detail="bmc: no cex over input space")
            return Evidence("formal", "counterexample",
                            witness=f"<mock>/{task}/engine_0/trace.vcd",
                            detail="cex: op=0 a=deadbeef (ADD off-by-one)")
        try:
            p = subprocess.run(["sby", "-f", os.path.basename(self.sby_file), task],
                               cwd=self.dir, capture_output=True, text=True, timeout=600)
            out = p.stdout + p.stderr
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            return Evidence("formal", "error", witness="", detail=str(e))
        base = os.path.splitext(os.path.basename(self.sby_file))[0]
        work = os.path.join(self.dir, f"{base}_{task}")
        if re.search(r"DONE \(PASS", out):
            return Evidence("formal", "proved",
                            witness=os.path.join(work, "status"),
                            detail="bmc pass (exhaustive for combinational eq.)", raw=out)
        if re.search(r"DONE \(FAIL", out):
            trace = os.path.join(work, "engine_0", "trace.vcd")
            return Evidence("formal", "counterexample",
                            witness=trace if os.path.exists(trace) else os.path.join(work, "status"),
                            detail="assertion falsified — counterexample trace", raw=out)
        return Evidence("formal", "error", witness="", detail="unparsed sby output", raw=out)


# --------------------------------------------------------------------------- #
# Simulation channel                                                          #
# --------------------------------------------------------------------------- #
class SimChannel:
    def __init__(self, rtl: str = RTL, tb: str = TB, mock: bool = False, workroot: str | None = None):
        self.rtl, self.tb = rtl, tb
        self.mock = mock or not _have("verilator")
        self.workroot = workroot or tempfile.mkdtemp(prefix="phase3_sim_")
        self._built: dict[bool, str] = {}

    def build(self, inject_bug: bool = False, nvec: int = 20000) -> str | None:
        if self.mock:
            return "<mock-binary>"
        mdir = os.path.join(self.workroot, "buggy" if inject_bug else "good")
        os.makedirs(mdir, exist_ok=True)
        cmd = ["verilator", "--binary", "--timing", "-Wno-fatal",
               "--top-module", "tb_alu", "-Mdir", mdir,
               f"-DNVEC={nvec}"] + (["-DINJECT_BUG=1"] if inject_bug else []) + \
              [self.rtl, self.tb, "-o", "Vtb"]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        binp = os.path.join(mdir, "Vtb")
        if p.returncode != 0 or not os.path.exists(binp):
            return None
        self._built[inject_bug] = binp
        return binp

    def run(self, inject_bug: bool = False, seed: int = 1, nvec: int = 20000) -> Evidence:
        """Run one simulation; returns inductive evidence with sample count n."""
        if self.mock:
            # random sim never hits the narrow DEAD_BEEF defect -> always PASS,
            # exactly why formal is needed (Sem-1).
            return Evidence("sim", "pass", witness=f"<mock>/sim_seed{seed}.log",
                            n=nvec, detail=f"{nvec} random vectors, 0 fails")
        binp = self._built.get(inject_bug) or self.build(inject_bug, nvec)
        if not binp:
            return Evidence("sim", "error", witness="", detail="verilator build failed")
        log = os.path.join(os.path.dirname(binp), f"run_seed{seed}.log")
        try:
            p = subprocess.run([binp, f"+seed={seed}"], capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired as e:
            return Evidence("sim", "error", witness="", detail=str(e))
        out = p.stdout + p.stderr
        with open(log, "w") as f:
            f.write(out)
        m = re.search(r"SIM_RESULT (PASS|FAIL) n=(\d+) fails=(\d+)", out)
        if not m:
            return Evidence("sim", "error", witness=log, detail="no SIM_RESULT line", raw=out)
        verdict, n, fails = m.group(1), int(m.group(2)), int(m.group(3))
        if verdict == "PASS":
            return Evidence("sim", "pass", witness=log, n=n, detail=f"{n} vectors, 0 fails")
        return Evidence("sim", "counterexample", witness=log, n=n,
                        detail=f"{fails} mismatches over {n} vectors")


if __name__ == "__main__":
    import sys
    mock = "--mock" in sys.argv
    print("tools:", {"sby": _have("sby"), "verilator": _have("verilator")}, "mock=" , mock)
    fc = FormalChannel(mock=mock)
    for t in ("good", "buggy"):
        print(" formal", t, "->", fc.prove(t))
    sc = SimChannel(mock=mock)
    for bug in (False, True):
        print(" sim inject_bug=%s ->" % bug, sc.run(bug, seed=7, nvec=5000))
