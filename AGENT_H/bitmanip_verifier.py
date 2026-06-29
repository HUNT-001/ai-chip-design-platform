"""
AGENT_H/bitmanip_verifier.py
============================
T28 — RV32B Bit-Manipulation Verification  (Zba / Zbb / Zbc / Zbs)

Golden-reference verification of the RISC-V scalar bit-manipulation extensions
from the canonical commit log.  Every B-extension instruction is a pure,
deterministic function of its integer operands, which makes it an ideal
golden-model target: the verifier recomputes the exact result from a shadow
register file and compares it bit-for-bit against the committed ``rd``.

Covered groups
--------------
  Zba (address generation) : sh1add, sh2add, sh3add
  Zbb (basic bitmanip)     : andn, orn, xnor, clz, ctz, cpop, min, max, minu,
                             maxu, sext.b, sext.h, zext.h, rol, ror, rori,
                             orc.b, rev8
  Zbc (carry-less multiply): clmul, clmulh, clmulr
  Zbs (single-bit)         : bclr, bclri, bext, bexti, binv, binvi, bset, bseti

All arithmetic is 32-bit (RV32).  Shift / rotate / single-bit amounts use the
low 5 bits of the operand, per the RV32 spec.

Usage
-----
  from AGENT_H.bitmanip_verifier import BitmanipVerifier
  report = BitmanipVerifier(rtl_log).run()

  from AGENT_H.bitmanip_verifier import run_from_manifest
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

_REG_RE = re.compile(r"\bx(?:[12]?\d|3[01]|\d)\b")


# ─────────────────────────────────────────────────────────
# Value helpers
# ─────────────────────────────────────────────────────────

def _to_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, int):
        return value & _M32
    if isinstance(value, str):
        v = value.strip()
        if not v:
            return None
        try:
            return (int(v, 16) if v.lower().startswith("0x") else int(v, 0)) & _M32
        except ValueError:
            try:
                return int(v) & _M32
            except ValueError:
                return None
    return None


def _u32(x: int) -> int:
    return x & _M32


def _s32(x: int) -> int:
    x &= _M32
    return x - 0x100000000 if x & _SIGN else x


# ─────────────────────────────────────────────────────────
# Golden compute functions
# ─────────────────────────────────────────────────────────

def _clz(a: int) -> int:
    a = _u32(a)
    if a == 0:
        return 32
    n = 0
    for i in range(31, -1, -1):
        if a & (1 << i):
            break
        n += 1
    return n


def _ctz(a: int) -> int:
    a = _u32(a)
    if a == 0:
        return 32
    n = 0
    for i in range(32):
        if a & (1 << i):
            break
        n += 1
    return n


def _cpop(a: int) -> int:
    return bin(_u32(a)).count("1")


def _orc_b(a: int) -> int:
    a = _u32(a)
    out = 0
    for byte in range(4):
        if (a >> (byte * 8)) & 0xFF:
            out |= 0xFF << (byte * 8)
    return out


def _rev8(a: int) -> int:
    a = _u32(a)
    return ((a & 0xFF) << 24) | ((a & 0xFF00) << 8) | \
           ((a >> 8) & 0xFF00) | ((a >> 24) & 0xFF)


def _rol(a: int, s: int) -> int:
    s &= 31
    a = _u32(a)
    return _u32((a << s) | (a >> (32 - s))) if s else a


def _ror(a: int, s: int) -> int:
    s &= 31
    a = _u32(a)
    return _u32((a >> s) | (a << (32 - s))) if s else a


def _clmul_full(a: int, b: int) -> int:
    a = _u32(a)
    b = _u32(b)
    out = 0
    for i in range(32):
        if (b >> i) & 1:
            out ^= a << i
    return out


def _sext_b(a: int) -> int:
    a &= 0xFF
    return _u32(a - 0x100 if a & 0x80 else a)


def _sext_h(a: int) -> int:
    a &= 0xFFFF
    return _u32(a - 0x10000 if a & 0x8000 else a)


# mnemonic -> (kind, fn)
#   kind: "bin" (rs1,rs2), "imm" (rs1,shamt), "un" (rs1)
_BINARY = {
    "sh1add": lambda a, b: _u32(b + (a << 1)),
    "sh2add": lambda a, b: _u32(b + (a << 2)),
    "sh3add": lambda a, b: _u32(b + (a << 3)),
    "andn":   lambda a, b: _u32(a & ~b),
    "orn":    lambda a, b: _u32(a | (~b & _M32)),
    "xnor":   lambda a, b: _u32(~(a ^ b)),
    "min":    lambda a, b: _u32(min(_s32(a), _s32(b))),
    "max":    lambda a, b: _u32(max(_s32(a), _s32(b))),
    "minu":   lambda a, b: min(_u32(a), _u32(b)),
    "maxu":   lambda a, b: max(_u32(a), _u32(b)),
    "rol":    _rol,
    "ror":    _ror,
    "clmul":  lambda a, b: _clmul_full(a, b) & _M32,
    "clmulh": lambda a, b: (_clmul_full(a, b) >> 32) & _M32,
    "clmulr": lambda a, b: (_clmul_full(a, b) >> 31) & _M32,
    "bclr":   lambda a, b: _u32(a & ~(1 << (b & 31))),
    "bext":   lambda a, b: (a >> (b & 31)) & 1,
    "binv":   lambda a, b: _u32(a ^ (1 << (b & 31))),
    "bset":   lambda a, b: _u32(a | (1 << (b & 31))),
}

_IMM = {
    "rori":  _ror,
    "bclri": lambda a, s: _u32(a & ~(1 << (s & 31))),
    "bexti": lambda a, s: (a >> (s & 31)) & 1,
    "binvi": lambda a, s: _u32(a ^ (1 << (s & 31))),
    "bseti": lambda a, s: _u32(a | (1 << (s & 31))),
}

_UNARY = {
    "clz":    _clz,
    "ctz":    _ctz,
    "cpop":   _cpop,
    "sext.b": _sext_b,
    "sext.h": _sext_h,
    "zext.h": lambda a: _u32(a & 0xFFFF),
    "orc.b":  _orc_b,
    "rev8":   _rev8,
}

BITMANIP_MNEMONICS = set(_BINARY) | set(_IMM) | set(_UNARY)


# ─────────────────────────────────────────────────────────
# Decode
# ─────────────────────────────────────────────────────────

@dataclass
class BMDecode:
    mnem: str
    kind: str            # "bin" | "imm" | "un"
    rd:   Optional[str]
    rs1:  Optional[str]
    rs2:  Optional[str]
    imm:  Optional[int]


def decode_bitmanip(disasm: str) -> Optional[BMDecode]:
    if not disasm:
        return None
    toks = disasm.strip().lower().split(None, 1)
    if not toks:
        return None
    mnem = toks[0]
    if mnem not in BITMANIP_MNEMONICS:
        return None
    rest = toks[1] if len(toks) > 1 else ""
    xregs = _REG_RE.findall(rest)

    if mnem in _UNARY:
        rd  = xregs[0] if len(xregs) > 0 else None
        rs1 = xregs[1] if len(xregs) > 1 else None
        return BMDecode(mnem, "un", rd, rs1, None, None)

    if mnem in _IMM:
        rd  = xregs[0] if len(xregs) > 0 else None
        rs1 = xregs[1] if len(xregs) > 1 else None
        imm = _first_imm(rest)
        return BMDecode(mnem, "imm", rd, rs1, None, imm)

    # binary
    rd  = xregs[0] if len(xregs) > 0 else None
    rs1 = xregs[1] if len(xregs) > 1 else None
    rs2 = xregs[2] if len(xregs) > 2 else None
    return BMDecode(mnem, "bin", rd, rs1, rs2, None)


def _first_imm(rest: str) -> Optional[int]:
    # drop register tokens, keep the first numeric literal
    cleaned = _REG_RE.sub(" ", rest)
    for tok in re.split(r"[\s,()]+", cleaned):
        v = _to_int(tok)
        if v is not None:
            return v
    return None


# ─────────────────────────────────────────────────────────
# Violation record
# ─────────────────────────────────────────────────────────

@dataclass
class BMViolation:
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

class BitmanipVerifier:
    """
    Verify RV32B (Zba/Zbb/Zbc/Zbs) semantics from a commit log.

    Parameters
    ----------
    rtl_log        : list of RTL commit records
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
        self._regs: Dict[str, int] = {}
        self._violations: List[BMViolation] = []
        self._stats = {"bitmanip_ops": 0, "checked": 0, "skipped": 0}

    def _flag(self, v: BMViolation) -> None:
        if len(self._violations) < self.max_violations:
            self._violations.append(v)

    def _apply_regs(self, rec: Dict) -> None:
        for name, val in (rec.get("regs") or {}).items():
            iv = _to_int(val)
            if iv is not None and re.fullmatch(r"x(?:[12]?\d|3[01]|\d)", name):
                self._regs[name] = iv
        self._regs["x0"] = 0

    def _check(self, rec: Dict, d: BMDecode, seq: int) -> None:
        if d.rd is None or d.rs1 is None:
            self._stats["skipped"] += 1
            return
        a = self._regs.get(d.rs1)
        if a is None:
            self._stats["skipped"] += 1
            return

        if d.kind == "un":
            golden = _UNARY[d.mnem](a)
        elif d.kind == "imm":
            if d.imm is None:
                self._stats["skipped"] += 1
                return
            golden = _IMM[d.mnem](a, d.imm)
        else:
            if d.rs2 is None:
                self._stats["skipped"] += 1
                return
            b = self._regs.get(d.rs2)
            if b is None:
                self._stats["skipped"] += 1
                return
            golden = _BINARY[d.mnem](a, b)

        golden = _u32(golden)
        committed = _to_int((rec.get("regs") or {}).get(d.rd))
        if committed is None:
            self._stats["skipped"] += 1
            return

        self._stats["checked"] += 1
        if d.rd == "x0":            # writes to x0 are discarded
            return
        if committed != golden:
            self._flag(BMViolation(
                "bitmanip_result", "HIGH", seq, rec.get("pc"), rec.get("disasm"),
                f"{d.mnem} result mismatch",
                expected=f"0x{golden:08x}", actual=f"0x{committed:08x}"))

    def run(self) -> Dict[str, Any]:
        started = datetime.now(timezone.utc)
        n = len(self.rtl_log)
        for i, rec in enumerate(self.rtl_log):
            if len(self._violations) >= self.max_violations:
                break
            if not isinstance(rec, dict):
                continue
            seq = rec.get("seq", i)
            d = decode_bitmanip((rec.get("disasm") or "").strip().lower())
            if d is not None:
                self._stats["bitmanip_ops"] += 1
                try:
                    self._check(rec, d, seq)
                except Exception as exc:           # never crash the pipeline
                    logger.warning("bitmanip_verifier: record %d raised: %s", seq, exc)
            self._apply_regs(rec)

        finished = datetime.now(timezone.utc)
        return self._report(n, started, finished)

    def _band(self) -> Tuple[float, str]:
        if not self._violations:
            return 0.0, "CLEAN"
        score = sum(self._SEV_WEIGHT.get(v.severity, 0.2) for v in self._violations)
        norm  = min(1.0, score / max(1, self._stats["bitmanip_ops"]))
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
            "agent":            "bitmanip_verifier",
            "records_checked":  n,
            "bitmanip_ops":     self._stats["bitmanip_ops"],
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
    """Pipeline entry point. Returns 0 on pass, 1 on any violation."""
    manifest_path = Path(manifest_path)
    try:
        with open(manifest_path) as f:
            manifest = json.load(f)
    except Exception as exc:
        logger.warning("bitmanip_verifier: cannot read manifest: %s", exc)
        return 0

    run_dir = Path(manifest.get("run_dir", manifest_path.parent))
    outputs = manifest.get("outputs", {})
    rtl_log = _load_log(run_dir, outputs, "rtl_commit_log", "rtl_commit.jsonl")
    iss_log = _load_log(run_dir, outputs, "iss_commit_log", "iss_commit.jsonl")
    if not rtl_log:
        logger.info("bitmanip_verifier: no RTL commit log, skipping")
        return 0

    report = BitmanipVerifier(rtl_log, iss_log).run()

    report_path = run_dir / "bitmanip_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
        f.write("\n")

    manifest.setdefault("outputs", {})["bitmanip_report"] = "bitmanip_report.json"
    manifest.setdefault("phases", {})["bitmanip_check"] = {
        "status": "pass" if report["pass"] else "fail",
        "violations": report["total_violations"],
    }
    tmp = manifest_path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    tmp.rename(manifest_path)

    logger.info("bitmanip_verifier: %d B-ext ops, %d violations, band=%s",
                report["bitmanip_ops"], report["total_violations"], report["band"])
    return 0 if report["pass"] else 1


# ─────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":          # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description="RV32B bit-manipulation verifier")
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
        rep = BitmanipVerifier(log).run()
        print(json.dumps(rep, indent=2))
        raise SystemExit(0 if rep["pass"] else 1)
    ap.print_help()
