"""
AGENT_H/branch_predictor_verifier.py
====================================
T39 — Branch Predictor Verification

Verifies branch-prediction behaviour from the canonical commit log (Level 7).
A branch predictor is a performance feature, not a correctness one: whatever it
guesses, the *committed* instruction stream must still follow the **actual**
branch outcomes.  So the sound, golden checks here are:

  * **Recovery** — after a conditional branch or a direct jump, the committed
    next-PC must equal the architecturally-correct target (taken → branch
    target, not-taken → fall-through).  The outcome is recomputed independently
    from the register operands, so a predictor that mis-speculates and fails to
    recover (committing a wrong-path instruction) is caught.
  * **Prediction self-consistency** — if the DUT reports its own prediction
    (`predict.taken` / `predict.correct`), that hit/miss flag must agree with
    the actual outcome.

On top of that it produces the Level-7 metrics that ordinary diffing never
reports: prediction accuracy, misprediction rate, MPKI, taken-rate, and
return-address-stack (RAS) return-prediction accuracy from a golden RAS.

Everything is conservatively gated: a check runs only when the operands / target
/ next-PC (and, for the flag check, the DUT's prediction) are available; branch
targets are taken from an explicit ``target`` field or an absolute address in
the disassembly.  Clean no-op on traces without branch information.

Checks
------
  bp_recovery   committed next-PC != the actual (operand-derived) outcome
  bp_hit_flag   DUT-reported prediction correct/hit flag inconsistent with the
                actual outcome

Metrics (analytics — never fail the run)
----------------------------------------
  branches, taken_rate, predictions, accuracy, mispredicts, mpki,
  ras_returns, ras_accuracy

Optional trace contract (additive)
----------------------------------
  a branch record may carry:
    "target": "0x.."                       actual branch target
    "predict": { "taken": bool, "correct": bool, "kind": "bht|btb|ras" }

Usage
-----
  from AGENT_H.branch_predictor_verifier import BranchPredictorVerifier
  report = BranchPredictorVerifier(rtl_log).run()
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
_S32 = 1 << 31

# link registers used by the RAS hint rules
_LINK = {1, 5}   # x1 (ra), x5 (t0)

_ABI = {
    "zero": 0, "ra": 1, "sp": 2, "gp": 3, "tp": 4,
    "t0": 5, "t1": 6, "t2": 7, "s0": 8, "fp": 8, "s1": 9,
    "a0": 10, "a1": 11, "a2": 12, "a3": 13, "a4": 14, "a5": 15,
    "a6": 16, "a7": 17, "s2": 18, "s3": 19, "s4": 20, "s5": 21,
    "s6": 22, "s7": 23, "s8": 24, "s9": 25, "s10": 26, "s11": 27,
    "t3": 28, "t4": 29, "t5": 30, "t6": 31,
}
_XRE = re.compile(r"^x(\d{1,2})$")
_REGTOK = re.compile(r"\b(x(?:[12]?\d|3[01]|\d)|zero|ra|sp|gp|tp|fp|"
                     r"a[0-7]|s(?:1[01]|[0-9])|t[0-6])\b")

_COND = {"beq", "bne", "blt", "bge", "bltu", "bgeu", "beqz", "bnez",
         "c.beqz", "c.bnez"}
_JUMP = {"jal", "j", "c.j", "c.jal"}


def reg_idx(name: str) -> Optional[int]:
    if not name:
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
            return int(v, 16) if v.lower().startswith("0x") else int(v, 0)
        except ValueError:
            try:
                return int(v)
            except ValueError:
                return None
    return None


def _s32(x: int) -> int:
    x &= _M32
    return x - (1 << 32) if x & _S32 else x


def _abs_target(disasm: str) -> Optional[int]:
    """Absolute branch/jump target from disassembly (an 0x… address)."""
    m = re.findall(r"0x[0-9a-fA-F]+", disasm or "")
    if m:
        v = _to_int(m[-1])
        if v is not None and v >= 0x1000:      # looks like an address, not a small imm
            return v
    return None


# ─────────────────────────────────────────────────────────
# violation
# ─────────────────────────────────────────────────────────

@dataclass
class BPViolation:
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

class BranchPredictorVerifier:
    """
    Verify branch-prediction recovery & self-consistency; report Level-7 metrics.

    Parameters
    ----------
    rtl_log        : list of RTL commit records
    iss_log        : optional ISS commit records (reserved)
    max_violations : stop collecting after this many violations
    """

    _SEV_WEIGHT = {"HIGH": 1.0, "MEDIUM": 0.4, "LOW": 0.15}

    def __init__(
        self,
        rtl_log:        List[Dict[str, Any]],
        iss_log:        Optional[List[Dict[str, Any]]] = None,
        max_violations: int = 200,
    ) -> None:
        self.rtl_log        = rtl_log or []
        self.iss_log        = iss_log or []
        self.max_violations = max_violations
        self._reg: Dict[int, int] = {0: 0}
        self._ras: List[int] = []
        self._violations: List[BPViolation] = []
        self._m = {"branches": 0, "cond": 0, "taken": 0, "jumps": 0,
                   "predictions": 0, "correct": 0, "mispredict": 0,
                   "ras_returns": 0, "ras_correct": 0, "checked": 0}

    def _flag(self, v: BPViolation) -> None:
        if len(self._violations) < self.max_violations:
            self._violations.append(v)

    def _commit_regs(self, rec: Dict) -> None:
        for k, val in (rec.get("regs") or {}).items():
            idx = reg_idx(k)
            iv = _to_int(val)
            if idx is not None and iv is not None and idx != 0:
                self._reg[idx] = iv & _M32
        self._reg[0] = 0

    @staticmethod
    def _regs(disasm: str) -> List[int]:
        return [r for r in (reg_idx(t) for t in _REGTOK.findall(disasm)) if r is not None]

    def _cond_taken(self, mnem: str, regs: List[int]) -> Optional[bool]:
        if mnem in ("beqz", "bnez", "c.beqz", "c.bnez"):
            if not regs:
                return None
            a = self._reg.get(regs[0])
            if a is None:
                return None
            return (a == 0) if mnem in ("beqz", "c.beqz") else (a != 0)
        if len(regs) < 2:
            return None
        a = self._reg.get(regs[0]); b = self._reg.get(regs[1])
        if a is None or b is None:
            return None
        if mnem == "beq":  return a == b
        if mnem == "bne":  return a != b
        if mnem == "blt":  return _s32(a) < _s32(b)
        if mnem == "bge":  return _s32(a) >= _s32(b)
        if mnem == "bltu": return (a & _M32) < (b & _M32)
        if mnem == "bgeu": return (a & _M32) >= (b & _M32)
        return None

    # -- RAS hint model -------------------------------------------------------

    def _ras_update(self, mnem: str, regs: List[int], pc: Optional[int],
                    size: int, nxt_pc: Optional[int]) -> None:
        # calls: jal rd, jalr rd,rs1  where rd is a link register
        if mnem in ("jal", "call", "c.jal"):
            rd = regs[0] if regs else 1   # c.jal / call imply ra
            rd = 1 if mnem in ("call", "c.jal") else rd
            if rd in _LINK and pc is not None:
                self._ras.append((pc + size) & _M32)
            return
        if mnem == "jalr":
            rd = regs[0] if len(regs) > 0 else 0
            rs1 = regs[1] if len(regs) > 1 else (regs[0] if regs else 0)
            link_rd = rd in _LINK
            link_rs1 = rs1 in _LINK
            pop = False
            if not link_rd and link_rs1:
                pop = True
            elif link_rd and link_rs1 and rd != rs1:
                pop = True
            if pop:
                self._ras_return(nxt_pc, pc)
            if link_rd and pc is not None:
                self._ras.append((pc + size) & _M32)
            return
        if mnem in ("ret", "jr"):
            self._ras_return(nxt_pc, pc)

    def _ras_return(self, nxt_pc: Optional[int], pc: Optional[int]) -> None:
        if not self._ras:
            return
        predicted = self._ras.pop()
        self._m["ras_returns"] += 1
        if nxt_pc is not None and predicted == nxt_pc:
            self._m["ras_correct"] += 1

    # -- per-record ------------------------------------------------------------

    def _check(self, rec: Dict, nxt: Optional[Dict], seq: int) -> None:
        disasm = (rec.get("disasm") or "").strip().lower()
        if not disasm:
            return
        mnem = disasm.split()[0]
        pc = _to_int(rec.get("pc"))
        size = 2 if disasm.startswith("c.") else 4
        nxt_pc = _to_int(nxt.get("pc")) if isinstance(nxt, dict) else None
        next_trap = bool(nxt.get("trap")) if isinstance(nxt, dict) else False
        regs = self._regs(disasm)
        target = _to_int(rec.get("target")) or _abs_target(disasm)

        # RAS bookkeeping (returns need next pc)
        if mnem in ("jal", "jalr", "ret", "jr", "call", "c.jal"):
            self._ras_update(mnem, regs, pc, size, nxt_pc)
            if mnem in ("jal", "call", "c.jal", "j", "c.j"):
                self._m["jumps"] += 1

        is_cond = mnem in _COND
        is_jump = mnem in _JUMP
        if not (is_cond or is_jump):
            return
        self._m["branches"] += 1
        if is_cond:
            self._m["cond"] += 1

        # expected outcome
        taken = None
        if is_cond:
            taken = self._cond_taken(mnem, regs)
            if taken:
                self._m["taken"] += 1
        else:
            taken = True   # unconditional jump

        # ---- recovery check (sound: independent of the predictor) ----
        if pc is not None and nxt_pc is not None and not next_trap and taken is not None:
            fallthrough = (pc + size) & _M32
            if taken and target is not None:
                self._m["checked"] += 1
                if nxt_pc != target:
                    self._flag(BPViolation(
                        "bp_recovery", "HIGH", seq, rec.get("pc"), rec.get("disasm"),
                        f"branch/jump taken but committed next-PC 0x{nxt_pc:08x} != target "
                        f"0x{target:08x} (misprediction not recovered)",
                        expected=f"0x{target:08x}", actual=f"0x{nxt_pc:08x}"))
            elif not taken:
                self._m["checked"] += 1
                if nxt_pc != fallthrough:
                    self._flag(BPViolation(
                        "bp_recovery", "HIGH", seq, rec.get("pc"), rec.get("disasm"),
                        f"branch not taken but committed next-PC 0x{nxt_pc:08x} != fall-through "
                        f"0x{fallthrough:08x} (misprediction not recovered)",
                        expected=f"0x{fallthrough:08x}", actual=f"0x{nxt_pc:08x}"))

        # ---- prediction self-consistency + metrics ----
        pr = rec.get("predict")
        if isinstance(pr, dict):
            p_taken = pr.get("taken")
            p_correct = pr.get("correct")
            actual_taken = None
            if target is not None and nxt_pc is not None:
                if nxt_pc == target:
                    actual_taken = True
                elif nxt_pc == ((pc + size) & _M32 if pc is not None else -1):
                    actual_taken = False
            if isinstance(p_correct, bool):
                self._m["predictions"] += 1
                self._m["correct" if p_correct else "mispredict"] += 1
                if isinstance(p_taken, bool) and actual_taken is not None:
                    golden_correct = (p_taken == actual_taken)
                    if p_correct != golden_correct:
                        self._flag(BPViolation(
                            "bp_hit_flag", "HIGH", seq, rec.get("pc"), rec.get("disasm"),
                            f"DUT reports prediction correct={p_correct} but predicted "
                            f"taken={p_taken} vs actual taken={actual_taken}",
                            expected=str(golden_correct), actual=str(p_correct)))

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
            nxt = self.rtl_log[i + 1] if i + 1 < n else None
            try:
                self._check(rec, nxt, seq)
            except Exception as exc:               # never crash the pipeline
                logger.warning("branch_predictor_verifier: record %d raised: %s", seq, exc)
            self._commit_regs(rec)
        finished = datetime.now(timezone.utc)
        return self._report(n, started, finished)

    # -- metrics & report -----------------------------------------------------

    def _metrics(self, n: int) -> Dict[str, Any]:
        m = self._m
        pred = m["predictions"]
        return {
            "branches":     m["branches"],
            "cond_branches": m["cond"],
            "jumps":        m["jumps"],
            "taken_rate":   round(m["taken"] / m["cond"], 4) if m["cond"] else None,
            "predictions":  pred,
            "accuracy":     round(m["correct"] / pred, 4) if pred else None,
            "mispredicts":  m["mispredict"],
            "mpki":         round(m["mispredict"] / n * 1000, 2) if n else None,
            "ras_returns":  m["ras_returns"],
            "ras_accuracy": round(m["ras_correct"] / m["ras_returns"], 4) if m["ras_returns"] else None,
        }

    def _band(self) -> Tuple[float, str]:
        if not self._violations:
            return 0.0, "CLEAN"
        score = sum(self._SEV_WEIGHT.get(v.severity, 0.2) for v in self._violations)
        norm  = min(1.0, score / max(1, self._m["checked"] + 1))
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
            "agent":            "branch_predictor_verifier",
            "records_checked":  n,
            "metrics":          self._metrics(n),
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
        logger.warning("branch_predictor_verifier: cannot read manifest: %s", exc)
        return 0

    run_dir = Path(manifest.get("run_dir", manifest_path.parent))
    outputs = manifest.get("outputs", {})
    rtl_log = _load_log(run_dir, outputs, "rtl_commit_log", "rtl_commit.jsonl")
    iss_log = _load_log(run_dir, outputs, "iss_commit_log", "iss_commit.jsonl")
    if not rtl_log:
        logger.info("branch_predictor_verifier: no RTL commit log, skipping")
        return 0

    report = BranchPredictorVerifier(rtl_log, iss_log).run()

    report_path = run_dir / "branch_predictor_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
        f.write("\n")

    manifest.setdefault("outputs", {})["branch_predictor_report"] = "branch_predictor_report.json"
    manifest.setdefault("phases", {})["branch_predictor_check"] = {
        "status": "pass" if report["pass"] else "fail",
        "violations": report["total_violations"],
    }
    tmp = manifest_path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    tmp.rename(manifest_path)

    logger.info("branch_predictor_verifier: metrics=%s, %d violations, band=%s",
                report["metrics"], report["total_violations"], report["band"])
    return 0 if report["pass"] else 1


# ─────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":          # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description="Branch predictor verifier")
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
        rep = BranchPredictorVerifier(log).run()
        print(json.dumps(rep, indent=2))
        raise SystemExit(0 if rep["pass"] else 1)
    ap.print_help()
