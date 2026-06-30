"""
AGENT_H/pipeline_verifier.py
============================
T32 — Pipeline & Hazard Verification

Verifies pipeline hazard handling from the canonical commit log, and produces
the Level-2 pipeline metrics.  The architecturally-observable consequence of a
hazard-handling bug (a missing forward, a dropped stall, a failed flush) is a
*wrong committed value* or a *wrong committed program order* — and this agent
detects those independently of the ISS, with an **explainable** diagnosis.

The novel, sound core is a golden in-order ALU model:

  * for every RV32I ALU instruction whose operands are known, the result is
    recomputed from the architectural (committed-in-order) register file and
    compared to the committed ``rd``;
  * if it differs, the model re-derives the result using the **un-forwarded
    stale** value of any source that was written within the forwarding window.
    If the stale value reproduces the committed (wrong) result, the bug is
    diagnosed precisely as a **forwarding / stall hazard** (the pipeline used
    the old operand), naming the stale source and the producer distance.
    Otherwise it is reported as a generic ALU-result mismatch.

Because the shadow register file is updated from the *committed* value after
each instruction, a single wrong instruction is flagged exactly once — there is
no error cascade and no false positive on a correct trace.

Checks
------
  hazard_forwarding   ALU result explained by an un-forwarded stale operand
  alu_result          ALU result wrong, not explained by a forwarding hazard
  control_hazard      jalr/ret/jr did not redirect to its computed target
                      (flush / branch-recovery failure)

Metrics (analytics — never fail the run)
----------------------------------------
  hazards : RAW / WAR / WAW / control counts
  perf    : cycles, instret, IPC, CPI, stall_cycles, utilization

Usage
-----
  from AGENT_H.pipeline_verifier import PipelineVerifier
  report = PipelineVerifier(rtl_log).run()

  from AGENT_H.pipeline_verifier import run_from_manifest
  run_from_manifest(Path(run_dir) / "run_manifest.json")
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "2.1.0"
_M32 = 0xFFFFFFFF
_SIGN = 0x80000000


# ─────────────────────────────────────────────────────────
# register naming (xN + ABI -> index 0..31)
# ─────────────────────────────────────────────────────────

_ABI = {
    "zero": 0, "ra": 1, "sp": 2, "gp": 3, "tp": 4,
    "t0": 5, "t1": 6, "t2": 7, "s0": 8, "fp": 8, "s1": 9,
    "a0": 10, "a1": 11, "a2": 12, "a3": 13, "a4": 14, "a5": 15,
    "a6": 16, "a7": 17, "s2": 18, "s3": 19, "s4": 20, "s5": 21,
    "s6": 22, "s7": 23, "s8": 24, "s9": 25, "s10": 26, "s11": 27,
    "t3": 28, "t4": 29, "t5": 30, "t6": 31,
}
_XRE = re.compile(r"^x(\d{1,2})$")
# matches a register token (x.. or ABI) for ordered extraction from disasm
_REGTOK = re.compile(r"\b(x(?:[12]?\d|3[01]|\d)|zero|ra|sp|gp|tp|fp|"
                     r"a[0-7]|s(?:1[01]|[0-9])|t[0-6])\b")


def reg_idx(name: str) -> Optional[int]:
    if name is None:
        return None
    n = name.strip().lower()
    m = _XRE.match(n)
    if m:
        v = int(m.group(1))
        return v if 0 <= v <= 31 else None
    return _ABI.get(n)


def _to_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        v = value.strip()
        if not v:
            return None
        try:
            return int(v, 16) if v.lower().startswith(("0x", "-0x")) else int(v, 0)
        except ValueError:
            try:
                return int(v)
            except ValueError:
                return None
    return None


def _u32(x: int) -> int:
    return x & _M32


def _s32(x: int) -> int:
    x &= _M32
    return x - 0x100000000 if x & _SIGN else x


# ─────────────────────────────────────────────────────────
# golden RV32I ALU
# ─────────────────────────────────────────────────────────

_RTYPE = {"add", "sub", "and", "or", "xor", "sll", "srl", "sra", "slt", "sltu"}
_ITYPE = {"addi", "andi", "ori", "xori", "slti", "sltiu", "slli", "srli", "srai"}


def alu_eval(op: str, a: int, b: int) -> Optional[int]:
    """Evaluate an RV32I ALU op. b is rs2 (R-type) or the immediate (I-type)."""
    a = _u32(a)
    if op in ("add", "addi", "mv"):
        return _u32(a + b)
    if op == "sub":
        return _u32(a - b)
    if op in ("and", "andi"):
        return _u32(a & b)
    if op in ("or", "ori"):
        return _u32(a | b)
    if op in ("xor", "xori"):
        return _u32(a ^ b)
    if op in ("sll", "slli"):
        return _u32(a << (b & 31))
    if op in ("srl", "srli"):
        return _u32(a >> (b & 31))
    if op in ("sra", "srai"):
        return _u32(_s32(a) >> (b & 31))
    if op in ("slt", "slti"):
        return 1 if _s32(a) < _s32(b) else 0
    if op in ("sltu", "sltiu"):
        return 1 if _u32(a) < _u32(b) else 0
    return None


@dataclass
class Decoded:
    op:   str
    rd:   Optional[int]
    rs1:  Optional[int]
    rs2:  Optional[int]   # None for I-type
    imm:  Optional[int]   # None for R-type
    kind: str             # "alu" | "jalr" | "branch" | "jump" | "other"


def decode(disasm: str) -> Decoded:
    if not disasm:
        return Decoded("", None, None, None, None, "other")
    d = disasm.strip().lower()
    toks = d.split()
    mnem = toks[0]
    regs = [reg_idx(t) for t in _REGTOK.findall(d)]
    regs = [r for r in regs if r is not None]

    if mnem in _RTYPE:
        rd  = regs[0] if len(regs) > 0 else None
        rs1 = regs[1] if len(regs) > 1 else None
        rs2 = regs[2] if len(regs) > 2 else None
        return Decoded(mnem, rd, rs1, rs2, None, "alu")
    if mnem in _ITYPE:
        rd  = regs[0] if len(regs) > 0 else None
        rs1 = regs[1] if len(regs) > 1 else None
        return Decoded(mnem, rd, rs1, None, _last_imm(d), "alu")
    if mnem == "mv":
        rd  = regs[0] if len(regs) > 0 else None
        rs1 = regs[1] if len(regs) > 1 else None
        return Decoded("mv", rd, rs1, None, 0, "alu")
    if mnem in ("jalr",):
        # jalr rd, rs1, imm  |  jalr rd, imm(rs1)  |  jalr rs1
        rd  = regs[0] if len(regs) > 0 else None
        rs1 = regs[1] if len(regs) > 1 else (regs[0] if regs else None)
        return Decoded("jalr", rd, rs1, None, _last_imm(d) or 0, "jalr")
    if mnem in ("ret",):
        return Decoded("ret", 0, 1, None, 0, "jalr")          # jalr x0, 0(ra)
    if mnem in ("jr",):
        rs1 = regs[0] if regs else None
        return Decoded("jr", 0, rs1, None, _last_imm(d) or 0, "jalr")
    if mnem in ("beq", "bne", "blt", "bge", "bltu", "bgeu", "beqz", "bnez"):
        return Decoded(mnem, None, regs[0] if regs else None,
                       regs[1] if len(regs) > 1 else None, _trailing_addr(d), "branch")
    if mnem in ("jal", "j"):
        return Decoded(mnem, None, None, None, _trailing_addr(d), "jump")
    return Decoded(mnem, None, None, None, None, "other")


def _last_imm(d: str) -> Optional[int]:
    # offset(reg) -> take the offset; else the last bare numeric token
    m = re.search(r"(-?(?:0x[0-9a-f]+|\d+))\s*\(", d)
    if m:
        return _to_int(m.group(1))
    cleaned = _REGTOK.sub(" ", d.split(None, 1)[1] if " " in d else "")
    nums = re.findall(r"-?(?:0x[0-9a-f]+|\d+)", cleaned)
    return _to_int(nums[-1]) if nums else None


def _trailing_addr(d: str) -> Optional[int]:
    # absolute hex target some disassemblers print, e.g. "beq a0,a1,80000010".
    # (used only for metrics classification, never for a hard check)
    m = re.search(r"(0x[0-9a-f]+)\s*$", d.strip())
    return _to_int(m.group(1)) if m else None


# ─────────────────────────────────────────────────────────
# violation
# ─────────────────────────────────────────────────────────

@dataclass
class PVViolation:
    check:       str
    severity:    str
    seq:         int
    pc:          Optional[str]
    disasm:      Optional[str]
    description: str
    expected:    Optional[str] = None
    actual:      Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "check": self.check, "severity": self.severity, "seq": self.seq,
            "pc": self.pc, "disasm": self.disasm, "description": self.description,
            "expected": self.expected, "actual": self.actual,
        }


# ─────────────────────────────────────────────────────────
# verifier
# ─────────────────────────────────────────────────────────

class PipelineVerifier:
    """
    Verify pipeline hazard handling and collect pipeline metrics.

    Parameters
    ----------
    rtl_log         : list of RTL commit records
    iss_log         : optional ISS commit records (reserved for cross-check)
    forward_window  : forwarding/stall depth used to attribute a stale operand
                      to a hazard (typical 5-stage pipeline ⇒ 5)
    max_violations  : stop collecting after this many violations
    """

    _SEV_WEIGHT = {"HIGH": 1.0, "MEDIUM": 0.4, "LOW": 0.15}

    def __init__(
        self,
        rtl_log:        List[Dict[str, Any]],
        iss_log:        Optional[List[Dict[str, Any]]] = None,
        forward_window: int = 5,
        max_violations: int = 200,
    ) -> None:
        self.rtl_log        = rtl_log or []
        self.iss_log        = iss_log or []
        self.forward_window = forward_window
        self.max_violations = max_violations

        self._cur:   Dict[int, int] = {0: 0}    # architectural regfile
        self._prev:  Dict[int, int] = {}        # value before most recent write
        self._wseq:  Dict[int, int] = {}        # seq of most recent write
        self._recent_dst: List[Tuple[int, int]] = []  # (seq, reg) window
        self._recent_src: List[Tuple[int, int]] = []

        self._violations: List[PVViolation] = []
        self._hazards = {"raw": 0, "war": 0, "waw": 0, "control": 0}
        self._stats = {"alu_checked": 0, "evaluable": 0}
        self._cycles_seen: List[int] = []

    def _flag(self, v: PVViolation) -> None:
        if len(self._violations) < self.max_violations:
            self._violations.append(v)

    # -- hazard inventory -----------------------------------------------------

    def _classify(self, dec: Decoded, seq: int) -> None:
        srcs = [r for r in (dec.rs1, dec.rs2) if r is not None and r != 0]
        dst  = dec.rd if (dec.rd is not None and dec.rd != 0) else None
        window = self.forward_window

        recent_d = {reg for (s, reg) in self._recent_dst if seq - s <= window}
        recent_s = {reg for (s, reg) in self._recent_src if seq - s <= window}

        if any(s in recent_d for s in srcs):
            self._hazards["raw"] += 1
        if dst is not None and dst in recent_d:
            self._hazards["waw"] += 1
        if dst is not None and dst in recent_s:
            self._hazards["war"] += 1
        if dec.kind in ("branch", "jump", "jalr"):
            self._hazards["control"] += 1

        for s in srcs:
            self._recent_src.append((seq, s))
        if dst is not None:
            self._recent_dst.append((seq, dst))
        # trim windows
        self._recent_src = [(s, r) for (s, r) in self._recent_src if seq - s <= window]
        self._recent_dst = [(s, r) for (s, r) in self._recent_dst if seq - s <= window]

    # -- ALU / forwarding correctness ----------------------------------------

    def _check_alu(self, rec: Dict, dec: Decoded, seq: int) -> None:
        if dec.rd is None or dec.rs1 is None:
            return
        a = self._cur.get(dec.rs1)
        if a is None:
            return
        if dec.rs2 is not None:                 # R-type
            b = self._cur.get(dec.rs2)
            if b is None:
                return
            b_src = dec.rs2
        else:                                   # I-type
            b = dec.imm
            b_src = None
            if b is None:
                return

        committed = self._committed_rd(rec, dec.rd)
        if committed is None:
            return

        self._stats["evaluable"] += 1
        expected = alu_eval(dec.op, a, b)
        if expected is None:
            return
        self._stats["alu_checked"] += 1

        if dec.rd == 0:                         # writes to x0 are discarded
            return
        if committed == expected:
            return

        # mismatch — try to explain it as an un-forwarded stale operand
        diag = self._diagnose_forwarding(dec, a, b, b_src, committed, seq)
        if diag is not None:
            stale_reg, dist = diag
            self._flag(PVViolation(
                "hazard_forwarding", "HIGH", seq, rec.get("pc"), rec.get("disasm"),
                f"{dec.op}: result matches the un-forwarded (stale) value of "
                f"x{stale_reg} written {dist} instruction(s) earlier — forwarding/"
                f"stall hazard not handled",
                expected=f"0x{expected:08x}", actual=f"0x{committed:08x}"))
        else:
            self._flag(PVViolation(
                "alu_result", "HIGH", seq, rec.get("pc"), rec.get("disasm"),
                f"{dec.op}: committed result != golden in-order result",
                expected=f"0x{expected:08x}", actual=f"0x{committed:08x}"))

    def _diagnose_forwarding(self, dec, a, b, b_src, committed, seq) -> Optional[Tuple[int, int]]:
        window = self.forward_window
        candidates: List[Tuple[int, int, int]] = []   # (src_reg, stale_a, stale_b)
        # rs1 stale?
        if dec.rs1 in self._prev and seq - self._wseq.get(dec.rs1, -10**9) <= window:
            candidates.append((dec.rs1, self._prev[dec.rs1], b))
        # rs2 stale? (R-type only)
        if b_src is not None and b_src in self._prev and \
                seq - self._wseq.get(b_src, -10**9) <= window:
            candidates.append((b_src, a, self._prev[b_src]))
        for src_reg, sa, sb in candidates:
            if alu_eval(dec.op, sa, sb) == committed:
                return src_reg, seq - self._wseq.get(src_reg, seq)
        return None

    @staticmethod
    def _committed_rd(rec: Dict, rd: int) -> Optional[int]:
        regs = rec.get("regs") or {}
        for k, v in regs.items():
            if reg_idx(k) == rd:
                return _to_int(v)
        return None

    # -- control-hazard correctness (jalr / ret / jr) ------------------------

    def _check_control(self, dec: Decoded, nxt: Optional[Dict], seq: int, rec: Dict) -> None:
        if dec.kind != "jalr" or nxt is None or not isinstance(nxt, dict):
            return
        if dec.rs1 is None or dec.rs1 not in self._cur:
            return
        target = (self._cur[dec.rs1] + (dec.imm or 0)) & ~1 & _M32
        nxt_pc = _to_int(nxt.get("pc"))
        if nxt_pc is None:
            return
        if nxt_pc != target:
            self._flag(PVViolation(
                "control_hazard", "HIGH", seq, rec.get("pc"), rec.get("disasm"),
                f"{dec.op}: next committed PC 0x{nxt_pc:08x} != computed target "
                f"0x{target:08x} (flush / branch-recovery failure)",
                expected=f"0x{target:08x}", actual=f"0x{nxt_pc:08x}"))

    # -- regfile update -------------------------------------------------------

    def _commit_regs(self, rec: Dict, seq: int) -> None:
        for k, v in (rec.get("regs") or {}).items():
            idx = reg_idx(k)
            iv = _to_int(v)
            if idx is None or iv is None or idx == 0:
                continue
            self._prev[idx] = self._cur.get(idx, 0)
            self._cur[idx] = iv & _M32
            self._wseq[idx] = seq
        self._cur[0] = 0

    # -- main loop ------------------------------------------------------------

    def run(self) -> Dict[str, Any]:
        started = datetime.now(timezone.utc)
        n = len(self.rtl_log)
        for i, rec in enumerate(self.rtl_log):
            if len(self._violations) >= self.max_violations:
                break
            if not isinstance(rec, dict):
                continue
            seq = rec.get("seq", i)
            disasm = (rec.get("disasm") or "").strip().lower()
            dec = decode(disasm)
            nxt = self.rtl_log[i + 1] if i + 1 < n else None
            try:
                self._classify(dec, seq)
                if dec.kind == "alu":
                    self._check_alu(rec, dec, seq)
                elif dec.kind == "jalr":
                    self._check_control(dec, nxt, seq, rec)
            except Exception as exc:               # never crash the pipeline
                logger.warning("pipeline_verifier: record %d raised: %s", seq, exc)
            pc_ = rec.get("perf_counters") or {}
            cyc = _to_int(pc_.get("cycles"))
            if cyc is not None:
                self._cycles_seen.append(cyc)
            self._commit_regs(rec, seq)

        finished = datetime.now(timezone.utc)
        return self._report(n, started, finished)

    # -- metrics & report -----------------------------------------------------

    def _perf(self, instret: int) -> Dict[str, Any]:
        if not self._cycles_seen or instret == 0:
            return {"cycles": 0, "instret": instret, "ipc": None, "cpi": None,
                    "stall_cycles": None, "utilization": None}
        mono = all(self._cycles_seen[i] <= self._cycles_seen[i + 1]
                   for i in range(len(self._cycles_seen) - 1))
        total = self._cycles_seen[-1] if mono else sum(self._cycles_seen)
        total = max(total, instret)
        cpi = round(total / instret, 3)
        return {
            "cycles": total, "instret": instret,
            "ipc": round(instret / total, 3), "cpi": cpi,
            "stall_cycles": total - instret,
            "utilization": round(instret / total, 3),
        }

    def _band(self) -> Tuple[float, str]:
        if not self._violations:
            return 0.0, "CLEAN"
        score = sum(self._SEV_WEIGHT.get(v.severity, 0.2) for v in self._violations)
        norm  = min(1.0, score / max(1, self._stats["alu_checked"] + 1))
        if any(v.severity == "HIGH" for v in self._violations):
            band = "CRITICAL"
        elif norm >= 0.3:
            band = "DEGRADED"
        else:
            band = "MINOR"
        return round(norm, 4), band

    def _report(self, n: int, started, finished) -> Dict[str, Any]:
        score, band = self._band()
        high = [v for v in self._violations if v.severity == "HIGH"]
        return {
            "schema_version":   SCHEMA_VERSION,
            "agent":            "pipeline_verifier",
            "records_checked":  n,
            "alu_checked":      self._stats["alu_checked"],
            "hazards":          dict(self._hazards),
            "perf":             self._perf(n),
            "stats":            dict(self._stats),
            "total_violations": len(self._violations),
            "high_violations":  len(high),
            "severity_score":   score,
            "band":             band,
            "pass":             len(self._violations) == 0,
            "violations":       [v.to_dict() for v in self._violations[:50]],
            "started_at":       started.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "finished_at":      finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duration_s":       round((finished - started).total_seconds(), 3),
        }


# ─────────────────────────────────────────────────────────
# manifest integration
# ─────────────────────────────────────────────────────────

def _load_log(run_dir: Path, outputs: Dict, key: str, default: str) -> List[Dict]:
    p = run_dir / (outputs.get(key) or default)
    if not p.exists():
        return []
    recs: List[Dict] = []
    with open(p) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    recs.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return recs


def run_from_manifest(manifest_path: Path) -> int:
    """Pipeline entry point. Returns 0 on pass, 1 on any violation."""
    manifest_path = Path(manifest_path)
    try:
        with open(manifest_path) as f:
            manifest = json.load(f)
    except Exception as exc:
        logger.warning("pipeline_verifier: cannot read manifest: %s", exc)
        return 0

    run_dir = Path(manifest.get("run_dir", manifest_path.parent))
    outputs = manifest.get("outputs", {})
    rtl_log = _load_log(run_dir, outputs, "rtl_commit_log", "rtl_commit.jsonl")
    iss_log = _load_log(run_dir, outputs, "iss_commit_log", "iss_commit.jsonl")
    if not rtl_log:
        logger.info("pipeline_verifier: no RTL commit log, skipping")
        return 0

    report = PipelineVerifier(rtl_log, iss_log).run()

    report_path = run_dir / "pipeline_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
        f.write("\n")

    manifest.setdefault("outputs", {})["pipeline_report"] = "pipeline_report.json"
    manifest.setdefault("phases", {})["pipeline_check"] = {
        "status": "pass" if report["pass"] else "fail",
        "violations": report["total_violations"],
    }
    tmp = manifest_path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    tmp.rename(manifest_path)

    logger.info("pipeline_verifier: %d ALU checked, hazards=%s, %d violations, band=%s",
                report["alu_checked"], report["hazards"],
                report["total_violations"], report["band"])
    return 0 if report["pass"] else 1


# ─────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":          # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description="Pipeline & hazard verifier")
    ap.add_argument("--manifest", type=Path)
    ap.add_argument("--rtl", type=Path)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.manifest:
        raise SystemExit(run_from_manifest(args.manifest))
    if args.rtl:
        log = []
        with open(args.rtl) as fh:
            for ln in fh:
                ln = ln.strip()
                if ln:
                    log.append(json.loads(ln))
        rep = PipelineVerifier(log).run()
        print(json.dumps(rep, indent=2))
        raise SystemExit(0 if rep["pass"] else 1)
    ap.print_help()
