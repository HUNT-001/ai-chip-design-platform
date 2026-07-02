"""
AGENT_H/rv64_verifier.py
========================
T36 — RV64 Datapath Verification (the start of the XLEN-64 widening)

Verifies the **defining RV64 semantics** from the commit log: 64-bit register
arithmetic and the W-suffix *word* operations (``addw``/``subw``/``sllw``/
``srlw``/``sraw`` and the immediate forms) whose 32-bit result must be
**sign-extended to 64 bits**.  Forgetting that sign extension — leaving the
upper 32 bits zero — is the single most common RV64 implementation bug, and it
is invisible to an RV32 model.

This is the first agent of the RV64 widening.  It is deliberately self-contained
and **auto-detecting**: it runs only on traces that are actually RV64 (they
contain a W-op or a register value wider than 32 bits), so it is a clean no-op
on the existing RV32 suite and introduces no schema change.

The golden core is a 64-bit in-order ALU; the checker recomputes every modelled
result from the architectural register file and compares it to the committed
``rd``, with an explainable diagnosis for the sign-extension case.

Checks
------
  rv64_word_sext   W-op low 32 bits correct but upper 32 != sign-extension of
                   bit 31 (the classic "forgot to sign-extend" bug)
  rv64_word_op     W-op result wrong for another reason
  rv64_result      64-bit ALU result != golden
  rv64_shamt       W-shift immediate shamt > 31 (reserved encoding)

Metrics (analytics — never fail the run)
----------------------------------------
  ops_checked, word_ops

Usage
-----
  from AGENT_H.rv64_verifier import RV64Verifier
  report = RV64Verifier(rtl_log).run()

  from AGENT_H.rv64_verifier import run_from_manifest
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
_M64 = (1 << 64) - 1
_M32 = 0xFFFFFFFF
_S64 = 1 << 63
_S32 = 1 << 31


# ─────────────────────────────────────────────────────────
# 64-bit helpers
# ─────────────────────────────────────────────────────────

def _u64(x: int) -> int:
    return x & _M64


def _s64(x: int) -> int:
    x &= _M64
    return x - (1 << 64) if x & _S64 else x


def _s32(x: int) -> int:
    x &= _M32
    return x - (1 << 32) if x & _S32 else x


def sext32(v: int) -> int:
    """Sign-extend a 32-bit value to 64 bits (unsigned representation)."""
    v &= _M32
    return _u64(v | 0xFFFFFFFF00000000) if v & _S32 else v


def _hex64(x: int) -> str:
    return f"0x{x & _M64:016x}"


# ─────────────────────────────────────────────────────────
# register naming (xN + ABI -> index)
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
_REGTOK = re.compile(r"\b(x(?:[12]?\d|3[01]|\d)|zero|ra|sp|gp|tp|fp|"
                     r"a[0-7]|s(?:1[01]|[0-9])|t[0-6])\b")


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


# ─────────────────────────────────────────────────────────
# golden RV64 ALU
# ─────────────────────────────────────────────────────────

_R64 = {"add", "sub", "and", "or", "xor", "sll", "srl", "sra", "slt", "sltu"}
_I64 = {"addi", "andi", "ori", "xori", "slti", "sltiu", "slli", "srli", "srai"}
_WORD_R = {"addw", "subw", "sllw", "srlw", "sraw"}
_WORD_I = {"addiw", "slliw", "srliw", "sraiw"}
# M-extension: 64-bit (R-type) and the RV64-only W-suffix forms
_M64OPS = {"mul", "mulh", "mulhsu", "mulhu", "div", "divu", "rem", "remu"}
_MWORD  = {"mulw", "divw", "divuw", "remw", "remuw"}
_WORD_MNEMS = _WORD_R | _WORD_I | _MWORD


def _trunc_div(a: int, b: int) -> int:
    """Signed division truncated toward zero (RISC-V semantics)."""
    q = abs(a) // abs(b)
    return -q if (a < 0) != (b < 0) else q


def _trunc_rem(a: int, b: int) -> int:
    return a - _trunc_div(a, b) * b


def alu64(op: str, a: int, b: int) -> Optional[int]:
    """64-bit RV64 ALU. b is rs2 (R) or immediate (I)."""
    a = _u64(a)
    if op in ("add", "addi"):
        return _u64(a + b)
    if op == "sub":
        return _u64(a - b)
    if op in ("and", "andi"):
        return _u64(a & b)
    if op in ("or", "ori"):
        return _u64(a | b)
    if op in ("xor", "xori"):
        return _u64(a ^ b)
    if op in ("sll", "slli"):
        return _u64(a << (b & 63))
    if op in ("srl", "srli"):
        return _u64(a >> (b & 63))
    if op in ("sra", "srai"):
        return _u64(_s64(a) >> (b & 63))
    if op in ("slt", "slti"):
        return 1 if _s64(a) < _s64(b) else 0
    if op in ("sltu", "sltiu"):
        return 1 if _u64(a) < _u64(b) else 0
    # M-extension (64-bit)
    if op == "mul":
        return _u64(a * b)
    if op == "mulh":
        return _u64((_s64(a) * _s64(b)) >> 64)
    if op == "mulhu":
        return _u64((_u64(a) * _u64(b)) >> 64)
    if op == "mulhsu":
        return _u64((_s64(a) * _u64(b)) >> 64)
    if op == "div":
        sa, sb = _s64(a), _s64(b)
        if sb == 0:
            return _M64                                  # x / 0 -> -1
        if sa == -(1 << 63) and sb == -1:
            return _u64(sa)                              # overflow -> dividend
        return _u64(_trunc_div(sa, sb))
    if op == "divu":
        return _M64 if _u64(b) == 0 else _u64(a) // _u64(b)
    if op == "rem":
        sa, sb = _s64(a), _s64(b)
        if sb == 0:
            return _u64(sa)                             # x % 0 -> x
        if sa == -(1 << 63) and sb == -1:
            return 0                                     # overflow -> 0
        return _u64(_trunc_rem(sa, sb))
    if op == "remu":
        return _u64(a) if _u64(b) == 0 else _u64(a) % _u64(b)
    return None


def aluw(op: str, a: int, b: int) -> Optional[int]:
    """RV64 W-op: 32-bit operation, result sign-extended to 64 bits."""
    a32 = a & _M32
    if op in ("addw", "addiw"):
        return sext32((a32 + b) & _M32)
    if op == "subw":
        return sext32((a32 - b) & _M32)
    if op in ("sllw", "slliw"):
        return sext32((a32 << (b & 31)) & _M32)
    if op in ("srlw", "srliw"):
        return sext32((a32 & _M32) >> (b & 31))
    if op in ("sraw", "sraiw"):
        return sext32((_s32(a32) >> (b & 31)) & _M32)
    # M-extension W-ops: 32-bit operation, result sign-extended to 64 bits
    b32 = b & _M32
    if op == "mulw":
        return sext32((a32 * b32) & _M32)
    if op == "divw":
        sa, sb = _s32(a32), _s32(b32)
        if sb == 0:
            return sext32(_M32)                          # -1
        if sa == -(1 << 31) and sb == -1:
            return sext32(0x80000000)                    # overflow -> dividend
        return sext32(_trunc_div(sa, sb) & _M32)
    if op == "divuw":
        return sext32(_M32) if b32 == 0 else sext32((a32 // b32) & _M32)
    if op == "remw":
        sa, sb = _s32(a32), _s32(b32)
        if sb == 0:
            return sext32(a32)
        if sa == -(1 << 31) and sb == -1:
            return 0
        return sext32(_trunc_rem(sa, sb) & _M32)
    if op == "remuw":
        return sext32(a32) if b32 == 0 else sext32((a32 % b32) & _M32)
    return None


@dataclass
class Decoded:
    op:   str
    rd:   Optional[int]
    rs1:  Optional[int]
    rs2:  Optional[int]
    imm:  Optional[int]
    word: bool


def decode(disasm: str) -> Optional[Decoded]:
    if not disasm:
        return None
    d = disasm.strip().lower()
    mnem = d.split()[0]
    if mnem not in (_R64 | _I64 | _WORD_MNEMS | _M64OPS) and mnem != "mv":
        return None
    regs = [reg_idx(t) for t in _REGTOK.findall(d)]
    regs = [r for r in regs if r is not None]
    rd  = regs[0] if len(regs) > 0 else None
    rs1 = regs[1] if len(regs) > 1 else None

    if mnem in _R64 or mnem in _WORD_R or mnem in _M64OPS or mnem in _MWORD:
        rs2 = regs[2] if len(regs) > 2 else None
        return Decoded(mnem, rd, rs1, rs2, None, mnem in _WORD_R or mnem in _MWORD)
    if mnem == "mv":
        return Decoded("addi", rd, rs1, None, 0, False)
    # I-type / word-I
    return Decoded(mnem, rd, rs1, None, _last_imm(d), mnem in _WORD_I)


def _last_imm(d: str) -> Optional[int]:
    m = re.search(r"(-?(?:0x[0-9a-f]+|\d+))\s*\(", d)
    if m:
        return _to_int(m.group(1))
    body = d.split(None, 1)[1] if " " in d else ""
    cleaned = _REGTOK.sub(" ", body)
    nums = re.findall(r"-?(?:0x[0-9a-f]+|\d+)", cleaned)
    return _to_int(nums[-1]) if nums else None


# ─────────────────────────────────────────────────────────
# violation
# ─────────────────────────────────────────────────────────

@dataclass
class RV64Violation:
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

class RV64Verifier:
    """
    Verify RV64 datapath semantics (64-bit ALU + W-ops) from a commit log.

    Parameters
    ----------
    rtl_log        : list of RTL commit records
    iss_log        : optional ISS commit records (reserved)
    force          : run even if the trace does not look like RV64
    max_violations : stop collecting after this many violations
    """

    _SEV_WEIGHT = {"HIGH": 1.0, "MEDIUM": 0.4, "LOW": 0.15}

    def __init__(
        self,
        rtl_log:        List[Dict[str, Any]],
        iss_log:        Optional[List[Dict[str, Any]]] = None,
        force:          bool = False,
        max_violations: int = 200,
    ) -> None:
        self.rtl_log        = rtl_log or []
        self.iss_log        = iss_log or []
        self.max_violations = max_violations
        self._is_rv64       = force or self._detect_rv64()
        self._reg: Dict[int, int] = {0: 0}
        self._violations: List[RV64Violation] = []
        self._stats = {"ops_checked": 0, "word_ops": 0}

    def _detect_rv64(self) -> bool:
        for rec in self.rtl_log:
            if not isinstance(rec, dict):
                continue
            d = (rec.get("disasm") or "").strip().lower()
            if d and d.split()[0] in _WORD_MNEMS:
                return True
            for v in (rec.get("regs") or {}).values():
                iv = _to_int(v)
                if iv is not None and iv > _M32:
                    return True
        return False

    def _flag(self, v: RV64Violation) -> None:
        if len(self._violations) < self.max_violations:
            self._violations.append(v)

    @staticmethod
    def _committed(rec: Dict, rd: int) -> Optional[int]:
        for k, v in (rec.get("regs") or {}).items():
            if reg_idx(k) == rd:
                return _to_int(v)
        return None

    def _check(self, rec: Dict, dec: Decoded, seq: int) -> None:
        if dec.rd is None or dec.rs1 is None:
            return
        a = self._reg.get(dec.rs1)
        if a is None:
            return
        if dec.rs2 is not None:
            b = self._reg.get(dec.rs2)
            if b is None:
                return
        else:
            b = dec.imm
            if b is None:
                return

        committed = self._committed(rec, dec.rd)
        if committed is None:
            return
        committed = _u64(committed)

        if dec.word:
            # reserved W-shift shamt > 31
            if dec.op in ("slliw", "srliw", "sraiw") and dec.imm is not None and dec.imm > 31:
                self._flag(RV64Violation(
                    "rv64_shamt", "HIGH", seq, rec.get("pc"), rec.get("disasm"),
                    f"{dec.op} shamt {dec.imm} > 31 is a reserved RV64 encoding"))
            golden = aluw(dec.op, a, b)
            self._stats["word_ops"] += 1
        else:
            golden = alu64(dec.op, a, b)
        if golden is None:
            return
        self._stats["ops_checked"] += 1
        if dec.rd == 0 or committed == golden:
            return

        # explainable sign-extension diagnosis for W-ops
        if dec.word and (committed & _M32) == (golden & _M32) and \
                (committed >> 32) != (golden >> 32):
            self._flag(RV64Violation(
                "rv64_word_sext", "HIGH", seq, rec.get("pc"), rec.get("disasm"),
                f"{dec.op}: low 32 bits correct but upper 32 bits not sign-extended "
                f"from bit 31 (RV64 W-op must sign-extend to 64 bits)",
                expected=_hex64(golden), actual=_hex64(committed)))
        else:
            self._flag(RV64Violation(
                "rv64_word_op" if dec.word else "rv64_result", "HIGH", seq,
                rec.get("pc"), rec.get("disasm"),
                f"{dec.op}: committed result != golden RV64 result",
                expected=_hex64(golden), actual=_hex64(committed)))

    def _commit(self, rec: Dict, seq: int) -> None:
        for k, v in (rec.get("regs") or {}).items():
            idx = reg_idx(k)
            iv = _to_int(v)
            if idx is not None and iv is not None and idx != 0:
                self._reg[idx] = _u64(iv)
        self._reg[0] = 0

    # -- main loop ------------------------------------------------------------

    def run(self) -> Dict[str, Any]:
        started = datetime.now(timezone.utc)
        n = len(self.rtl_log)
        if self._is_rv64:
            for i, rec in enumerate(self.rtl_log):
                if len(self._violations) >= self.max_violations:
                    break
                if not isinstance(rec, dict):
                    continue
                seq = rec.get("seq", i)
                dec = decode((rec.get("disasm") or "").strip().lower())
                try:
                    if dec is not None:
                        self._check(rec, dec, seq)
                except Exception as exc:           # never crash the pipeline
                    logger.warning("rv64_verifier: record %d raised: %s", seq, exc)
                self._commit(rec, seq)
        finished = datetime.now(timezone.utc)
        return self._report(n, started, finished)

    def _band(self) -> Tuple[float, str]:
        if not self._violations:
            return 0.0, "CLEAN"
        score = sum(self._SEV_WEIGHT.get(v.severity, 0.2) for v in self._violations)
        norm  = min(1.0, score / max(1, self._stats["ops_checked"] + 1))
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
            "agent":            "rv64_verifier",
            "records_checked":  n,
            "rv64_detected":    self._is_rv64,
            "ops_checked":      self._stats["ops_checked"],
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
        logger.warning("rv64_verifier: cannot read manifest: %s", exc)
        return 0

    run_dir = Path(manifest.get("run_dir", manifest_path.parent))
    outputs = manifest.get("outputs", {})
    rtl_log = _load_log(run_dir, outputs, "rtl_commit_log", "rtl_commit.jsonl")
    iss_log = _load_log(run_dir, outputs, "iss_commit_log", "iss_commit.jsonl")
    if not rtl_log:
        logger.info("rv64_verifier: no RTL commit log, skipping")
        return 0

    report = RV64Verifier(rtl_log, iss_log).run()

    report_path = run_dir / "rv64_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
        f.write("\n")

    manifest.setdefault("outputs", {})["rv64_report"] = "rv64_report.json"
    manifest.setdefault("phases", {})["rv64_check"] = {
        "status": "pass" if report["pass"] else "fail",
        "violations": report["total_violations"],
    }
    tmp = manifest_path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    tmp.rename(manifest_path)

    logger.info("rv64_verifier: rv64=%s, %d ops, %d violations, band=%s",
                report["rv64_detected"], report["ops_checked"],
                report["total_violations"], report["band"])
    return 0 if report["pass"] else 1


# ─────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":          # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description="RV64 datapath verifier")
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
        rep = RV64Verifier(log).run()
        print(json.dumps(rep, indent=2))
        raise SystemExit(0 if rep["pass"] else 1)
    ap.print_help()
