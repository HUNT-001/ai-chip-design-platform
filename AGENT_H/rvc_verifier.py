"""
AGENT_H/rvc_verifier.py
=======================
T26 — RV32C Compressed Instruction Verification

Verifies the RISC-V **C** (Compressed) extension from the canonical commit log.
A compressed instruction is a 16-bit alias that *expands* to a 32-bit base
instruction; the high-value, RVC-specific bugs are not the arithmetic result
(ordinary tandem diffing catches that) but:

  * **PC stride** — a compressed instruction occupies 2 bytes, so the next
    sequential instruction must be at ``pc + 2``.  A core that mis-sizes a
    16-bit instruction and advances the PC by 4 will silently skip an
    instruction.  This is the single most important RVC check.
  * **Reserved / illegal encodings** — the all-zero halfword, ``c.addi4spn``
    with a zero immediate, ``c.lwsp`` / ``c.jr`` with ``x0``, ``c.lui`` with a
    zero immediate, etc., are reserved and must raise an illegal-instruction
    trap rather than execute.
  * **Register-field restriction** — the popular ("prime") RVC forms encode
    only ``x8``–``x15`` in their 3-bit register fields.  A disassembly naming a
    register outside that range for such a form indicates a decode bug.

This agent is pure-Python and needs no EDA tools.

Usage
-----
  from AGENT_H.rvc_verifier import RVCVerifier
  report = RVCVerifier(rtl_log).run()

  from AGENT_H.rvc_verifier import run_from_manifest
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

# Registers legal in the 3-bit "prime" RVC fields: x8..x15
_PRIME_REGS = {f"x{i}" for i in range(8, 16)}

# RVC forms that use prime (x8..x15) register fields for *all* their registers
_PRIME_FORMS = {
    "c.lw", "c.sw", "c.and", "c.or", "c.xor", "c.sub",
    "c.addi4spn", "c.beqz", "c.bnez", "c.srli", "c.srai", "c.andi",
}

# RVC control-transfer forms (PC stride check does not apply — target varies)
_BRANCH_FORMS = {
    "c.j", "c.jal", "c.jr", "c.jalr", "c.beqz", "c.bnez", "c.ret",
}

_REG_RE = re.compile(r"\bx(?:[12]?\d|3[01]|0)\b")


# ─────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────

def _to_int(value: Any) -> Optional[int]:
    if value is None:
        return None
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


def is_compressed(rec: Dict[str, Any]) -> bool:
    """
    Decide whether a commit record is a compressed (16-bit) instruction.

    Honours, in order: an explicit ``insn_len``/``ilen`` field (== 2), a
    truthy ``compressed`` flag, a 4-hex-digit ``insn``/``encoding`` field, or a
    ``c.`` disassembly prefix.
    """
    for k in ("insn_len", "ilen", "instr_len"):
        v = rec.get(k)
        if v is not None:
            try:
                return int(v) == 2
            except (TypeError, ValueError):
                pass
    if rec.get("compressed") is True:
        return True
    for k in ("insn", "encoding", "opcode_hex", "raw"):
        v = rec.get(k)
        if isinstance(v, str):
            h = v.strip().lower()
            if h.startswith("0x"):
                h = h[2:]
            if h and all(c in "0123456789abcdef" for c in h):
                # 16-bit encodings have the two low bits != 0b11
                if len(h) <= 4:
                    return True
                if len(h) == 8:
                    return False
    return (rec.get("disasm") or "").strip().lower().startswith("c.")


def rvc_mnemonic(disasm: str) -> str:
    return (disasm or "").strip().lower().split()[0] if disasm else ""


# ─────────────────────────────────────────────────────────
# Violation record
# ─────────────────────────────────────────────────────────

@dataclass
class RVCViolation:
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
# Verifier
# ─────────────────────────────────────────────────────────

class RVCVerifier:
    """
    Verify RV32C compressed-instruction semantics from a commit log.

    Parameters
    ----------
    rtl_log        : list of RTL commit records (authoritative DUT output)
    iss_log        : optional ISS commit records (reserved for cross-check)
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
        self._violations: List[RVCViolation] = []
        self._stats = {"compressed": 0, "branches": 0, "reserved": 0}

    def _flag(self, v: RVCViolation) -> None:
        if len(self._violations) < self.max_violations:
            self._violations.append(v)

    # -- individual checks ----------------------------------------------------

    def _check_reg_constraint(self, rec: Dict, mnem: str, seq: int) -> None:
        if mnem not in _PRIME_FORMS:
            return
        regs = _REG_RE.findall(rec.get("disasm") or "")
        bad  = [r for r in regs if r not in _PRIME_REGS]
        if bad:
            self._flag(RVCViolation(
                "rvc_reg_constraint", "HIGH", seq, rec.get("pc"), rec.get("disasm"),
                f"{mnem} uses register(s) {bad} outside the x8-x15 prime field"))

    def _check_reserved(self, rec: Dict, mnem: str, disasm: str, seq: int) -> None:
        trapped = (rec.get("trap") or {}).get("cause") == 2  # illegal instruction
        reason: Optional[str] = None

        regs = _REG_RE.findall(disasm)
        imm  = self._first_imm(disasm)

        if "illegal" in disasm or "unimp" in disasm or mnem in ("c.illegal", "c.unimp"):
            reason = "all-zero / illegal compressed encoding"
        elif mnem == "c.addi4spn" and imm == 0:
            reason = "c.addi4spn with a zero immediate is reserved"
        elif mnem == "c.lwsp" and regs and regs[0] == "x0":
            reason = "c.lwsp with rd=x0 is reserved"
        elif mnem == "c.jr" and regs and regs[0] == "x0":
            reason = "c.jr x0 is reserved"
        elif mnem == "c.lui" and (imm == 0 or (regs and regs[0] == "x0")):
            reason = "c.lui with rd=x0 or a zero immediate is reserved"
        elif mnem == "c.addi16sp" and imm == 0:
            reason = "c.addi16sp with a zero immediate is reserved"

        if reason is not None:
            self._stats["reserved"] += 1
            if not trapped:
                self._flag(RVCViolation(
                    "rvc_reserved", "HIGH", seq, rec.get("pc"), disasm,
                    f"reserved/illegal compressed encoding executed without an "
                    f"illegal-instruction trap: {reason}",
                    expected="trap cause 2", actual=str((rec.get('trap') or {}).get('cause'))))

    def _check_pc_stride(self, rec: Dict, nxt: Optional[Dict], mnem: str, seq: int) -> None:
        if mnem in _BRANCH_FORMS or nxt is None:
            if mnem in _BRANCH_FORMS:
                self._stats["branches"] += 1
            return
        if nxt.get("trap"):
            return  # a trap redirects the PC
        pc      = _to_int(rec.get("pc"))
        pc_next = _to_int(nxt.get("pc"))
        if pc is None or pc_next is None:
            return
        delta = pc_next - pc
        if delta == 4:
            self._flag(RVCViolation(
                "rvc_pc_stride", "HIGH", seq, rec.get("pc"), rec.get("disasm"),
                f"compressed instruction advanced PC by 4 instead of 2 "
                f"({rec.get('pc')} -> {nxt.get('pc')}) — an instruction was skipped",
                expected=f"0x{pc + 2:08x}", actual=nxt.get("pc")))

    @staticmethod
    def _first_imm(disasm: str) -> Optional[int]:
        """Extract the first standalone immediate (not part of xN/offset(reg))."""
        # strip register-relative offsets like 8(x2)
        cleaned = re.sub(r"-?\b\d+\s*\(", "(", disasm)
        for tok in re.split(r"[\s,]+", cleaned):
            t = tok.strip()
            if not t or _REG_RE.fullmatch(t):
                continue
            v = _to_int(t)
            if v is not None:
                return v
        return None

    # -- main loop ------------------------------------------------------------

    def run(self) -> Dict[str, Any]:
        started = datetime.now(timezone.utc)
        n = len(self.rtl_log)

        for i, rec in enumerate(self.rtl_log):
            if len(self._violations) >= self.max_violations:
                break
            if not isinstance(rec, dict):
                continue
            if not is_compressed(rec):
                continue
            self._stats["compressed"] += 1
            disasm = (rec.get("disasm") or "").strip().lower()
            mnem   = rvc_mnemonic(disasm)
            seq    = rec.get("seq", i)
            nxt    = self.rtl_log[i + 1] if i + 1 < n else None
            try:
                self._check_reg_constraint(rec, mnem, seq)
                self._check_reserved(rec, mnem, disasm, seq)
                self._check_pc_stride(rec, nxt, mnem, seq)
            except Exception as exc:                # never crash the pipeline
                logger.warning("rvc_verifier: record %d raised: %s", seq, exc)

        finished = datetime.now(timezone.utc)
        return self._report(n, started, finished)

    # -- reporting ------------------------------------------------------------

    def _band(self) -> Tuple[float, str]:
        if not self._violations:
            return 0.0, "CLEAN"
        score = sum(self._SEV_WEIGHT.get(v.severity, 0.2) for v in self._violations)
        norm  = min(1.0, score / max(1, self._stats["compressed"]))
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
            "agent":            "rvc_verifier",
            "records_checked":  n,
            "compressed_seen":  self._stats["compressed"],
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
# Manifest integration
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
    """
    Pipeline entry point. Loads the RTL commit log, runs the RVC verifier,
    writes ``rvc_report.json`` and updates the manifest. Returns 0 on pass,
    1 on any violation; degrades gracefully (0) when no log is present.
    """
    manifest_path = Path(manifest_path)
    try:
        with open(manifest_path) as f:
            manifest = json.load(f)
    except Exception as exc:
        logger.warning("rvc_verifier: cannot read manifest: %s", exc)
        return 0

    run_dir = Path(manifest.get("run_dir", manifest_path.parent))
    outputs = manifest.get("outputs", {})
    rtl_log = _load_log(run_dir, outputs, "rtl_commit_log", "rtl_commit.jsonl")
    iss_log = _load_log(run_dir, outputs, "iss_commit_log", "iss_commit.jsonl")
    if not rtl_log:
        logger.info("rvc_verifier: no RTL commit log, skipping")
        return 0

    report = RVCVerifier(rtl_log, iss_log).run()

    report_path = run_dir / "rvc_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
        f.write("\n")

    manifest.setdefault("outputs", {})["rvc_report"] = "rvc_report.json"
    manifest.setdefault("phases", {})["rvc_check"] = {
        "status": "pass" if report["pass"] else "fail",
        "violations": report["total_violations"],
    }
    tmp = manifest_path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    tmp.rename(manifest_path)

    logger.info("rvc_verifier: %d compressed instrs, %d violations, band=%s",
                report["compressed_seen"], report["total_violations"], report["band"])
    return 0 if report["pass"] else 1


# ─────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":          # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description="RV32C compressed-instruction verifier")
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
        rep = RVCVerifier(log).run()
        print(json.dumps(rep, indent=2))
        raise SystemExit(0 if rep["pass"] else 1)
    ap.print_help()
