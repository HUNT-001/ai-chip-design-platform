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
import os, re, sys, shutil, subprocess, tempfile, hashlib
from dataclasses import dataclass, field

HERE = os.path.dirname(os.path.abspath(__file__))
RTL  = os.path.join(HERE, "rtl", "alu.sv")
TB   = os.path.join(HERE, "sim", "tb_alu.sv")
SBY  = os.path.join(HERE, "formal", "alu.sby")


def _have(tool: str) -> bool:
    return shutil.which(tool) is not None


def _hash_file(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except OSError:
        return None


def _stamp(path: str) -> str:
    """Make a witness tamper-evident: append a short content hash of the real
    artifact. Verified later by verify_witness() — a stamp nothing checks is
    decorative, so provenance is only real once it is re-validated."""
    h = _hash_file(path)
    return f"{path}#sha256:{h}" if h else path


def verify_witness(witness: str):
    """Re-hash a stamped witness and compare. Returns (ok, reason).

    ok=True   artifact exists and its content still matches the recorded hash
    ok=False  artifact missing, altered, or (for a real run) never stamped
    Mock witnesses ("<mock>/...") are reported as unverifiable, not as valid.
    """
    if not witness:
        return False, "no witness"
    if witness.startswith("<mock>"):
        return False, "mock witness (not a real artifact)"
    if "#sha256:" not in witness:
        return False, "unstamped witness — provenance cannot be checked"
    path, recorded = witness.rsplit("#sha256:", 1)
    if not os.path.exists(path):
        return False, f"artifact missing: {path}"
    actual = _hash_file(path)
    if actual is None:
        return False, f"artifact unreadable: {path}"
    if actual != recorded:
        return False, f"CONTENT CHANGED since the judgment was recorded: {path}"
    return True, "verified"


def audit_knowledge(ks):
    """Verify every witness in a KnowledgeState. Returns (all_ok, [(phi, ok, reason)]).

    This is what makes the evidence chain auditable: a claim is only as good as
    the artifact it cites still being the artifact that was cited.
    """
    rows = []
    for phi, j in ks.K.items():
        ok, why = verify_witness(j.witness)
        rows.append((phi, ok, why))
    return all(r[1] for r in rows), rows


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

    def __init__(self, sby_file: str = SBY, mock: bool = False, combinational: bool = False,
                 negative_control: str | None = None):
        # combinational=True declares the DUT has no state, so a bmc pass at
        # depth>=1 is COMPLETE (exhaustive over inputs) and counts as a full
        # deductive proof. For sequential DUTs leave it False (bmc stays bounded).
        #
        # negative_control names an sby task that is KNOWN-BAD and MUST fail. It
        # is the vacuity gate (see _gate_ok). Without a passing gate this channel
        # will not issue a deductive proof, because a green formal result means
        # nothing if the assertions are not actually binding.
        self.sby_file = sby_file
        self.dir = os.path.dirname(sby_file)
        self.mock = mock or not _have("sby")
        self.mode = self._read_mode(sby_file)
        self.combinational = combinational
        self.negative_control = negative_control
        self._gate_state = None          # None=unchecked, True=armed, False=broken
        self._gate_reason = ""

    @staticmethod
    def _read_mode(sby_file: str) -> str:
        try:
            with open(sby_file) as f:
                m = re.search(r"^\s*mode\s+(\w+)", f.read(), re.M)
                return m.group(1) if m else "bmc"
        except OSError:
            return "bmc"

    # ---------------------------------------------------------------- gate ---
    def _gate_ok(self):
        """The vacuity gate. Runs the declared known-bad task ONCE and requires it
        to FAIL. If it passes (or none is declared) the assertions are not
        binding — every 'proof' from this setup would be vacuous — so no
        deductive warrant may be issued.

        This is the automated form of the discipline that caught three real
        vacuity incidents during bring-up (sv2v stripping assertions; clocked
        assertions never evaluated at depth 1; a misspelled class define leaves
        a harness with zero assertions). Those were caught by a human choosing
        to look. Here the system refuses to certify instead.
        """
        if self._gate_state is not None:
            return self._gate_state
        if not self.negative_control:
            self._gate_state, self._gate_reason = False, \
                "no negative_control declared — cannot show assertions are binding"
            return False
        ev = self._run_task(self.negative_control)
        if ev.status == "counterexample":
            self._gate_state, self._gate_reason = True, \
                f"negative control '{self.negative_control}' failed as required"
        elif ev.status == "error":
            self._gate_state, self._gate_reason = False, \
                f"negative control '{self.negative_control}' errored: {ev.detail}"
        else:
            self._gate_state, self._gate_reason = False, \
                (f"negative control '{self.negative_control}' PASSED but must fail — "
                 f"assertions are not binding; proofs from this setup are vacuous")
        return self._gate_state

    def gate_status(self):
        """(armed: bool, reason: str) — for reporting/audit."""
        ok = self._gate_ok()
        return ok, self._gate_reason

    def prove(self, task: str) -> Evidence:
        """Run one sby task, then apply the vacuity gate: a formal PASS is only
        recorded as a deductive proof if the declared negative control fails."""
        ev = self._run_task(task)
        if ev.status in ("proved", "bounded_pass") and not self._gate_ok():
            # Downgrade: the run passed, but we cannot show the check was real.
            return Evidence("formal", "gate_failed", witness=ev.witness,
                            detail=f"PASS not certifiable — {self._gate_reason}",
                            raw=ev.raw)
        return ev

    def _run_task(self, task: str) -> Evidence:
        """Raw sby invocation + verdict parsing (no gate applied)."""
        # A pass is a full proof if the mode is unbounded (k-induction/live) OR
        # the design is combinational (bmc depth>=1 is then exhaustive).
        unbounded = (self.mode in self._UNBOUNDED_MODES) or \
                    (self.mode == "bmc" and self.combinational)
        is_bug_task = any(s in task.lower() for s in ("bug", "buggy", "mut"))
        if self.mock:
            if not is_bug_task:
                st = "proved" if unbounded else "bounded_pass"
                return Evidence("formal", st, witness=f"<mock>/{task}/PASS",
                                detail=f"mode={self.mode}: no cex ({'unbounded proof' if unbounded else 'bounded'})")
            return Evidence("formal", "counterexample",
                            witness=f"<mock>/{task}/engine_0/trace.vcd",
                            detail="cex: mutated logic op on a narrow input")
        try:
            p = subprocess.run(["sby", "-f", os.path.basename(self.sby_file), task],
                               cwd=self.dir, capture_output=True, text=True, timeout=600)
            out = p.stdout + p.stderr
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            return Evidence("formal", "error", witness="", detail=str(e))
        base = os.path.splitext(os.path.basename(self.sby_file))[0]
        work = os.path.join(self.dir, f"{base}_{task}")
        flog = os.path.join(self.dir, f"sby_{task}.log")
        try:
            with open(flog, "w") as f:
                f.write("$ sby -f %s %s\n\n%s" % (os.path.basename(self.sby_file), task, out))
        except OSError:
            pass
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
        errln = [l for l in out.splitlines() if "ERROR" in l or "syntax error" in l.lower()]
        tail = " | ".join((errln or out.strip().splitlines())[-4:])
        return Evidence("formal", "error", witness=flog,
                        detail=f"sby did not reach a verdict (see {flog}): {tail}", raw=out)


# --------------------------------------------------------------------------- #
# Simulation channel                                                          #
# --------------------------------------------------------------------------- #
class SimChannel:
    def __init__(self, rtl: str = RTL, tb: str = TB, mock: bool = False, workroot: str | None = None,
                 sources=None, top: str = "tb_alu", defines_for=None):
        # Backward compatible: default = toy ALU (rtl, tb). For multi-file DUTs
        # (e.g. real ibex_alu: pkg + core + mutant + tb) pass `sources`, `top`,
        # and `defines_for(inject_bug)->[defines]`.
        self.sources = sources if sources is not None else [rtl, tb]
        self.top = top
        self.defines_for = defines_for or (lambda bug: (["INJECT_BUG=1"] if bug else []))
        self.mock = mock or not _have("verilator")
        self.workroot = workroot or tempfile.mkdtemp(prefix="phase3_sim_")
        self._built: dict[bool, str] = {}

    def build(self, inject_bug: bool = False, nvec: int = 20000):
        """Returns (binary_path, error_str). error_str is '' on success."""
        if self.mock:
            return "<mock-binary>", ""
        mdir = os.path.join(self.workroot, "buggy" if inject_bug else "good")
        os.makedirs(mdir, exist_ok=True)
        defs = [f"-D{d}" for d in self.defines_for(inject_bug)]
        cmd = ["verilator", "--binary", "--timing", "-Wno-fatal",
               "--top-module", self.top, "-Mdir", mdir,
               f"-DNVEC={nvec}"] + defs + list(self.sources) + ["-o", "Vtb"]
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


# --------------------------------------------------------------------------- #
# Static channel — structural evidence from the RTL graph                     #
# --------------------------------------------------------------------------- #
class StaticChannel:
    """Third evidence channel: sound structural analysis of the parsed RTL.

    Uses AGENT_H.rtl_graph (hardened against a 9-repo corpus) to decide
    structural properties — currently combinational-loop freedom. For the
    property it checks this is a DEDUCTIVE result: the analysis is exhaustive
    over the parsed netlist graph, not sampled.

    Honest boundary: the warrant is deductive *with respect to the parsed
    structure*. It inherits parser fidelity, so it is evidence about the design
    as parsed, not about post-synthesis silicon. That limit is recorded in the
    witness report rather than glossed.

    Note this channel required NO kernel change — it emits the same typed
    judgments as simulation and formal, which is the extensibility claim of the
    frozen kernel being exercised a third time.
    """

    def __init__(self, rtl_path: str, workdir: str | None = None, mock: bool = False):
        self.rtl_path = rtl_path
        self.mock = mock
        self.workdir = workdir or tempfile.mkdtemp(prefix="phase3_static_")

    def _report(self, name: str, body: str) -> str:
        os.makedirs(self.workdir, exist_ok=True)
        p = os.path.join(self.workdir, f"static_{name}.txt")
        with open(p, "w") as f:
            f.write(body)
        return _stamp(p)

    def check(self, prop: str = "comb_loops") -> Evidence:
        if self.mock:
            return Evidence("static", "proved",
                            witness=f"<mock>/static_{prop}.txt",
                            detail="mock: no combinational loops")
        try:
            sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            from AGENT_H import rtl_graph as rg
        except Exception as e:                       # graceful degradation
            return Evidence("static", "error", witness="", detail=f"rtl_graph unavailable: {e}")
        try:
            with open(self.rtl_path) as f:
                src = f.read()
            mods = rg.parse_module(src, self.rtl_path)
            if not mods:
                return Evidence("static", "error", witness="", detail="no module parsed")
            m = mods[0]
            if prop != "comb_loops":
                return Evidence("static", "unsupported", witness="",
                                detail=f"no static analysis for '{prop}'")
            loops = rg.find_comb_loops(m.comb_graph())
        except Exception as e:
            return Evidence("static", "error", witness="", detail=f"analysis failed: {e}")

        head = (f"module: {m.name}\nsource: {self.rtl_path}\n"
                f"signals: {len(m.signals)}  assigns: {len(m.assigns)}\n"
                f"analysis: combinational-loop detection over the parsed netlist\n"
                f"scope: deductive w.r.t. the PARSED structure (inherits parser fidelity)\n")
        if loops:
            body = head + f"result: {len(loops)} loop(s)\n" + \
                   "\n".join(" -> ".join(c) for c in loops[:10])
            return Evidence("static", "counterexample",
                            witness=self._report(f"{m.name}_{prop}", body),
                            detail=f"{len(loops)} combinational loop(s); first: "
                                   f"{' -> '.join(loops[0][:4])}")
        return Evidence("static", "proved",
                        witness=self._report(f"{m.name}_{prop}", head + "result: no loops\n"),
                        detail="no combinational loops over the parsed netlist")


if __name__ == "__main__":
    mock = "--mock" in sys.argv
    print("tools:", {"sby": _have("sby"), "verilator": _have("verilator")}, "mock=" , mock)
    fc = FormalChannel(mock=mock)
    for t in ("good", "buggy"):
        print(" formal", t, "->", fc.prove(t))
    sc = SimChannel(mock=mock)
    for bug in (False, True):
        print(" sim inject_bug=%s ->" % bug, sc.run(bug, seed=7, nvec=5000))
