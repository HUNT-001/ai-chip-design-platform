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
import os, re, shutil, subprocess, tempfile, hashlib
from dataclasses import dataclass, field

HERE = os.path.dirname(os.path.abspath(__file__))
RTL  = os.path.join(HERE, "rtl", "alu.sv")
TB   = os.path.join(HERE, "sim", "tb_alu.sv")
SBY  = os.path.join(HERE, "formal", "alu.sby")


def _have(tool: str) -> bool:
    return shutil.which(tool) is not None


def _stamp(path: str) -> str:
    """Make a witness tamper-evident: append a short content hash of the real
    artifact. The reputation layer (Phase 2) can re-verify provenance from this."""
    try:
        with open(path, "rb") as f:
            h = hashlib.sha256(f.read()).hexdigest()[:16]
        return f"{path}#sha256:{h}"
    except OSError:
        return path


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
    # Only these sby modes discharge risk UNBOUNDEDLY -> a full deductive proof.
    # A bounded check (mode bmc) that passes is strong evidence but NOT a proof;
    # labeling it "proved" would over-certify sequential logic (soundness bug).
    _UNBOUNDED_MODES = {"prove", "live"}

    def __init__(self, sby_file: str = SBY, mock: bool = False):
        self.sby_file = sby_file
        self.dir = os.path.dirname(sby_file)
        self.mock = mock or not _have("sby")
        self.mode = self._read_mode(sby_file)

    @staticmethod
    def _read_mode(sby_file: str) -> str:
        try:
            with open(sby_file) as f:
                m = re.search(r"^\s*mode\s+(\w+)", f.read(), re.M)
                return m.group(1) if m else "bmc"
        except OSError:
            return "bmc"

    def prove(self, task: str) -> Evidence:
        """Run one sby task. Returns proved (deductive) only for unbounded modes;
        a bounded pass is 'bounded_pass' (strong but not a proof)."""
        unbounded = self.mode in self._UNBOUNDED_MODES
        if self.mock:
            if task.startswith("good"):
                st = "proved" if unbounded else "bounded_pass"
                return Evidence("formal", st, witness=f"<mock>/{task}/PASS",
                                detail=f"mode={self.mode}: no cex ({'unbounded proof' if unbounded else 'bounded'})")
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
            status = "proved" if unbounded else "bounded_pass"
            detail = ("unbounded proof (k-induction)" if unbounded
                      else f"bounded pass to depth (mode={self.mode}) — NOT a full proof")
            return Evidence("formal", status, witness=_stamp(os.path.join(work, "status")),
                            detail=detail, raw=out)
        if re.search(r"DONE \(FAIL", out):
            trace = os.path.join(work, "engine_0", "trace.vcd")
            w = trace if os.path.exists(trace) else os.path.join(work, "status")
            return Evidence("formal", "counterexample", witness=_stamp(w),
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

    def build(self, inject_bug: bool = False, nvec: int = 20000):
        """Returns (binary_path, error_str). error_str is '' on success."""
        if self.mock:
            return "<mock-binary>", ""
        mdir = os.path.join(self.workroot, "buggy" if inject_bug else "good")
        os.makedirs(mdir, exist_ok=True)
        cmd = ["verilator", "--binary", "--timing", "-Wno-fatal",
               "--top-module", "tb_alu", "-Mdir", mdir,
               f"-DNVEC={nvec}"] + (["-DINJECT_BUG=1"] if inject_bug else []) + \
              [self.rtl, self.tb, "-o", "Vtb"]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            return None, str(e)
        blog = os.path.join(mdir, "build.log")
        with open(blog, "w") as f:
            f.write("$ " + " ".join(cmd) + "\n\n" + p.stdout + p.stderr)
        binp = os.path.join(mdir, "Vtb")
        if p.returncode != 0 or not os.path.exists(binp):
            tail = (p.stdout + p.stderr).strip().splitlines()[-4:]
            return None, "build failed (see %s): %s" % (blog, " | ".join(tail))
        self._built[inject_bug] = binp
        return binp, ""

    def run(self, inject_bug: bool = False, seed: int = 1, nvec: int = 20000) -> Evidence:
        """Run one simulation; returns inductive evidence with sample count n."""
        if self.mock:
            # random sim never hits the narrow DEAD_BEEF defect -> always PASS,
            # exactly why formal is needed (Sem-1).
            return Evidence("sim", "pass", witness=f"<mock>/sim_seed{seed}.log",
                            n=nvec, detail=f"{nvec} random vectors, 0 fails")
        binp = self._built.get(inject_bug)
        if not binp:
            binp, err = self.build(inject_bug, nvec)
            if not binp:
                return Evidence("sim", "error", witness="", detail=err)
        log = os.path.join(os.path.dirname(binp), f"run_seed{seed}.log")
        try:
            # Verilator seeds $urandom from this plusarg -> independent samples.
            p = subprocess.run([binp, f"+verilator+seed+{seed}"],
                               capture_output=True, text=True, timeout=300)
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
            return Evidence("sim", "pass", witness=_stamp(log), n=n, detail=f"{n} vectors, 0 fails")
        return Evidence("sim", "counterexample", witness=_stamp(log), n=n,
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
