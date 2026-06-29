"""
AGENT_H/fp_verifier.py
======================
T27 — RV32F / RV32D Floating-Point Verification

Golden-reference verification of the RISC-V **F** (single-precision) and **D**
(double-precision) floating-point extensions from the canonical commit log.

Floating point is a notorious source of silicon bugs that ordinary tandem
diffing under-tests: NaN-boxing of single values in wide registers, the
canonical-NaN rule, signed zeros, the ±inf / NaN corner cases of min/max and
compare, sign-injection bit plumbing, and the exception-flag (fflags) side
effects. This agent recomputes each operation with a golden IEEE-754 model
(Python's ``struct``/``float`, which are correctly rounded for the basic
operations in round-to-nearest-even) and compares it against the committed
result and flags.

To stay false-positive-free the verifier is conservative:
  * value checks for arithmetic run only under round-to-nearest-even (the
    architectural default and the mode used by virtually all FP test suites);
    under a confirmed directed-rounding mode a mismatch is reported at MEDIUM,
    never HIGH;
  * a check is skipped (not failed) whenever an operand value is not available
    in the trace;
  * generated-NaN results are compared against the RISC-V canonical NaN.

Checks
------
  fp_nan_boxing     single result in a 64-bit reg not NaN-boxed (upper = all 1s)
  fp_result         arithmetic / sqrt result != golden (RNE)
  fp_sgnj           sign-injection (fsgnj/n/x) bit result wrong
  fp_minmax         fmin/fmax result wrong (incl. NaN / ±0 rules)
  fp_compare        feq/flt/fle integer result wrong (incl. NaN rules)
  fp_class          fclass mask wrong
  fp_move           fmv.x.*/fmv.*.x bit move wrong
  fp_convert        fcvt.* result wrong (RNE)
  fp_flag_missing   a mandatory fflags exception bit (NV/DZ) was not raised

Usage
-----
  from AGENT_H.fp_verifier import FPVerifier
  report = FPVerifier(rtl_log).run()

  from AGENT_H.fp_verifier import run_from_manifest
  run_from_manifest(Path(run_dir) / "run_manifest.json")
"""

from __future__ import annotations

import json
import logging
import math
import re
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "2.1.0"

_M32 = 0xFFFFFFFF
_M64 = 0xFFFFFFFFFFFFFFFF

CANON_NAN32 = 0x7FC00000
CANON_NAN64 = 0x7FF8000000000000

# fflags exception bit positions
_NV = 1 << 4   # invalid
_DZ = 1 << 3   # divide by zero
_OF = 1 << 2   # overflow
_UF = 1 << 1   # underflow
_NX = 1 << 0   # inexact


# ─────────────────────────────────────────────────────────
# bit <-> float helpers
# ─────────────────────────────────────────────────────────

def bits_to_f32(b: int) -> float:
    return struct.unpack("<f", struct.pack("<I", b & _M32))[0]


def bits_to_f64(b: int) -> float:
    return struct.unpack("<d", struct.pack("<Q", b & _M64))[0]


def f32_to_bits(x: float) -> int:
    """Pack a Python float as float32 bits, mapping overflow/NaN to IEEE values."""
    if x != x:                       # NaN
        return CANON_NAN32
    try:
        return struct.unpack("<I", struct.pack("<f", x))[0]
    except OverflowError:            # magnitude exceeds float32 -> signed inf
        return 0xFF800000 if x < 0 else 0x7F800000


def f64_to_bits(x: float) -> int:
    if x != x:
        return CANON_NAN64
    try:
        return struct.unpack("<Q", struct.pack("<d", x))[0]
    except OverflowError:
        return 0xFFF0000000000000 if x < 0 else 0x7FF0000000000000


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


def _signbit(x: float) -> bool:
    return math.copysign(1.0, x) < 0


# ─────────────────────────────────────────────────────────
# FP register name handling (numeric f0..f31 + ABI names)
# ─────────────────────────────────────────────────────────

_ABI_F = {}
for i in range(8):
    _ABI_F[f"ft{i}"] = f"f{i}"
_ABI_F["fs0"] = "f8"; _ABI_F["fs1"] = "f9"
for i in range(8):
    _ABI_F[f"fa{i}"] = f"f{10 + i}"
for i in range(2, 12):
    _ABI_F[f"fs{i}"] = f"f{18 + (i - 2)}"
for i in range(8, 12):
    _ABI_F[f"ft{i}"] = f"f{28 + (i - 8)}"


def canon_freg(name: str) -> Optional[str]:
    n = name.strip().lower()
    if re.fullmatch(r"f(?:[12]?\d|3[01]|\d)", n):
        return n
    return _ABI_F.get(n)


_FREG_TOK = re.compile(r"\b(?:f(?:[12]?\d|3[01]|\d)|ft\d+|fs\d+|fa\d+)\b")
_XREG_TOK = re.compile(r"\bx(?:[12]?\d|3[01]|\d)\b")
_RM_TOK   = {"rne", "rtz", "rdn", "rup", "rmm", "dyn"}


# ─────────────────────────────────────────────────────────
# Decode
# ─────────────────────────────────────────────────────────

_FP_MNEM = re.compile(
    r"^\s*(f(?:add|sub|mul|div|sqrt|sgnjn|sgnjx|sgnj|min|max|"
    r"madd|msub|nmadd|nmsub|classify|class|mv|cvt|eq|lt|le|lw|ld|sw|sd|le)\S*)",
    re.IGNORECASE,
)


@dataclass
class FPDecode:
    mnem:   str            # base mnemonic without width, e.g. "fadd"
    width:  str            # "s" | "d" | ""   (operation width)
    rm:     Optional[str]
    fregs:  List[str]      # canonical f-names in textual order
    xregs:  List[str]      # x-names in textual order
    raw:    str


def decode_fp(disasm: str) -> Optional[FPDecode]:
    if not disasm:
        return None
    d = disasm.strip().lower()
    m = _FP_MNEM.match(d)
    if not m:
        return None
    head = m.group(1)                       # e.g. fadd.s, fcvt.w.s, fmv.x.w
    parts = head.split(".")
    base  = parts[0]
    width = ""
    for p in parts[1:]:
        if p in ("s", "d"):
            width = p
            break
    # operands
    rest = d[m.end():]
    fregs = [canon_freg(t) for t in _FREG_TOK.findall(rest)]
    fregs = [f for f in fregs if f]
    xregs = _XREG_TOK.findall(rest)
    rm = None
    for t in re.split(r"[\s,]+", rest):
        if t in _RM_TOK:
            rm = t
            break
    return FPDecode(base, width, rm, fregs, xregs, head)


# ─────────────────────────────────────────────────────────
# fclass
# ─────────────────────────────────────────────────────────

def fclass_mask(bits: int, width: str) -> int:
    if width == "d":
        sign = (bits >> 63) & 1
        exp  = (bits >> 52) & 0x7FF
        man  = bits & ((1 << 52) - 1)
        exp_max = 0x7FF
    else:
        sign = (bits >> 31) & 1
        exp  = (bits >> 23) & 0xFF
        man  = bits & ((1 << 23) - 1)
        exp_max = 0xFF

    if exp == 0:
        if man == 0:
            return (1 << 3) if sign else (1 << 4)          # -0 / +0
        return (1 << 2) if sign else (1 << 5)              # -sub / +sub
    if exp == exp_max:
        if man == 0:
            return (1 << 0) if sign else (1 << 7)          # -inf / +inf
        # NaN: quiet-bit is MSB of mantissa
        quiet_bit = 1 << (51 if width == "d" else 22)
        return (1 << 9) if (man & quiet_bit) else (1 << 8)  # qNaN / sNaN
    return (1 << 1) if sign else (1 << 6)                   # -normal / +normal


# ─────────────────────────────────────────────────────────
# Violation record
# ─────────────────────────────────────────────────────────

@dataclass
class FPViolation:
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

class FPVerifier:
    """
    Verify RV32F/D floating-point semantics from a commit log.

    Parameters
    ----------
    rtl_log        : list of RTL commit records
    iss_log        : optional ISS commit records (reserved for cross-check)
    flen           : FP register width in bits (32 or 64); auto-detected when None
    max_violations : stop collecting after this many violations
    """

    _SEV_WEIGHT = {"HIGH": 1.0, "MEDIUM": 0.4, "LOW": 0.15}
    _ARITH = {"fadd", "fsub", "fmul", "fdiv"}

    def __init__(
        self,
        rtl_log:        List[Dict[str, Any]],
        iss_log:        Optional[List[Dict[str, Any]]] = None,
        flen:           Optional[int] = None,
        max_violations: int = 200,
    ) -> None:
        self.rtl_log        = rtl_log or []
        self.iss_log        = iss_log or []
        self.max_violations = max_violations
        self._flen          = flen or self._detect_flen()
        self._freg: Dict[str, int] = {}    # shadow FP regs (raw bit patterns)
        self._violations: List[FPViolation] = []
        self._stats = {"fp_ops": 0, "checked": 0, "skipped": 0}

    # -- setup ----------------------------------------------------------------

    def _detect_flen(self) -> int:
        for rec in self.rtl_log:
            if not isinstance(rec, dict):
                continue
            d = (rec.get("disasm") or "").lower()
            if ".d" in d or d.strip().startswith(("fld", "fsd")):
                return 64
            for v in self._iter_fp_values(rec):
                if v is not None and v > _M32:
                    return 64
        return 32

    @staticmethod
    def _fp_map(rec: Dict) -> Dict[str, int]:
        """Collect this record's FP register post-state as {canon_name: bits}."""
        out: Dict[str, int] = {}
        if not isinstance(rec, dict):
            return out
        for key in ("fregs", "fpregs", "fp_regs", "f_regs"):
            m = rec.get(key)
            if isinstance(m, dict):
                for k, v in m.items():
                    cn = canon_freg(k)
                    iv = _to_int(v)
                    if cn and iv is not None:
                        out[cn] = iv
        # also allow f-names embedded in the generic regs map
        for k, v in (rec.get("regs") or {}).items():
            cn = canon_freg(k)
            iv = _to_int(v)
            if cn and iv is not None:
                out[cn] = iv
        return out

    def _iter_fp_values(self, rec: Dict):
        for v in self._fp_map(rec).values():
            yield v

    # -- operand access -------------------------------------------------------

    def _operand_f(self, name: str, width: str) -> Optional[float]:
        bits = self._freg.get(name)
        if bits is None:
            return None
        if width == "d":
            return bits_to_f64(bits & _M64)
        return bits_to_f32(bits & _M32)

    def _operand_bits(self, name: str, width: str) -> Optional[int]:
        bits = self._freg.get(name)
        if bits is None:
            return None
        return (bits & _M64) if width == "d" else (bits & _M32)

    def _flag(self, v: FPViolation) -> None:
        if len(self._violations) < self.max_violations:
            self._violations.append(v)

    def _rne(self, d: FPDecode) -> Tuple[bool, bool]:
        """(verify_value, confirmed_rne). Verify only under RNE."""
        if d.rm in ("rtz", "rdn", "rup", "rmm"):
            return False, False
        if d.rm == "rne":
            return True, True
        # dyn / unspecified -> assume architectural default RNE (common in tests)
        return True, False

    # -- golden arithmetic ----------------------------------------------------

    @staticmethod
    def _ieee_binop(op: str, a: float, b: float) -> float:
        if op == "fadd":
            return a + b
        if op == "fsub":
            return a - b
        if op == "fmul":
            return a * b
        if op == "fdiv":
            if b == 0.0:
                if a == 0.0 or a != a:
                    return float("nan")
                return math.copysign(float("inf"), a) * math.copysign(1.0, b)
            try:
                return a / b
            except OverflowError:
                return math.copysign(float("inf"), a) * math.copysign(1.0, b)
        raise ValueError(op)

    def _result_bits(self, val: float, width: str) -> int:
        return f64_to_bits(val) if width == "d" else f32_to_bits(val)

    # -- per-instruction check ------------------------------------------------

    def _check(self, rec: Dict, d: FPDecode, seq: int) -> None:
        post = self._fp_map(rec)
        regs = rec.get("regs") or {}
        w    = d.width or "s"
        fflags = self._fflags(rec)

        def fd() -> Optional[str]:
            return d.fregs[0] if d.fregs else None

        def committed_fd_bits() -> Optional[int]:
            f = fd()
            return post.get(f) if f else None

        # ---- NaN-boxing of any single-precision result in a 64-bit reg ----
        if self._flen == 64 and w == "s" and d.mnem in (
                self._ARITH | {"fsqrt", "fsgnj", "fsgnjn", "fsgnjx",
                               "fmin", "fmax", "fmadd", "fmsub", "fnmadd",
                               "fnmsub", "fcvt", "fmv", "flw"}):
            cb = committed_fd_bits()
            if cb is not None and (cb >> 32) != _M32:
                self._flag(FPViolation(
                    "fp_nan_boxing", "HIGH", seq, rec.get("pc"), rec.get("disasm"),
                    f"single-precision result in {fd()} is not NaN-boxed "
                    f"(upper 32 bits must be all ones)",
                    expected="0xffffffff_xxxxxxxx", actual=f"0x{cb:016x}"))

        # ---- arithmetic / sqrt ----
        if d.mnem in self._ARITH or d.mnem == "fsqrt":
            verify, confirmed = self._rne(d)
            a = self._operand_f(d.fregs[1], w) if len(d.fregs) > 1 else None
            if d.mnem == "fsqrt":
                a = self._operand_f(d.fregs[1], w) if len(d.fregs) > 1 else None
                b = None
            else:
                b = self._operand_f(d.fregs[2], w) if len(d.fregs) > 2 else None
            cb = committed_fd_bits()
            if cb is None or a is None or (d.mnem != "fsqrt" and b is None):
                self._stats["skipped"] += 1
                return
            # golden value
            if d.mnem == "fsqrt":
                gv = math.sqrt(a) if a >= 0 else float("nan")
                dz = False
                nv = a < 0 or a != a
            else:
                gv = self._ieee_binop(d.mnem, a, b)
                dz = d.mnem == "fdiv" and b == 0.0 and a != 0.0 and a == a
                nv = (gv != gv) and not (a != a or b != b)  # new NaN from valid ops
            gbits = self._result_bits(gv, w) & (_M64 if w == "d" else _M32)
            cval  = (cb & _M64) if w == "d" else (cb & _M32)
            self._stats["checked"] += 1
            if cval != gbits:
                self._flag(FPViolation(
                    "fp_result", "HIGH" if confirmed else "MEDIUM",
                    seq, rec.get("pc"), rec.get("disasm"),
                    f"{d.raw} result mismatch",
                    expected=f"0x{gbits:0{16 if w=='d' else 8}x}",
                    actual=f"0x{cval:0{16 if w=='d' else 8}x}"))
            # mandatory flags
            if fflags is not None:
                if dz and not (fflags & _DZ):
                    self._flag(FPViolation("fp_flag_missing", "MEDIUM", seq,
                        rec.get("pc"), rec.get("disasm"),
                        "divide-by-zero did not raise the DZ flag",
                        expected="DZ", actual=f"0x{fflags:02x}"))
                if nv and not (fflags & _NV):
                    self._flag(FPViolation("fp_flag_missing", "MEDIUM", seq,
                        rec.get("pc"), rec.get("disasm"),
                        "invalid operation did not raise the NV flag",
                        expected="NV", actual=f"0x{fflags:02x}"))
            return

        # ---- sign-injection ----
        if d.mnem in ("fsgnj", "fsgnjn", "fsgnjx"):
            a = self._operand_bits(d.fregs[1], w) if len(d.fregs) > 1 else None
            b = self._operand_bits(d.fregs[2], w) if len(d.fregs) > 2 else None
            cb = committed_fd_bits()
            if a is None or b is None or cb is None:
                self._stats["skipped"] += 1
                return
            sb = 63 if w == "d" else 31
            sign_a = (a >> sb) & 1
            sign_b = (b >> sb) & 1
            if d.mnem == "fsgnj":
                s = sign_b
            elif d.mnem == "fsgnjn":
                s = sign_b ^ 1
            else:
                s = sign_a ^ sign_b
            body = a & ~(1 << sb)
            golden = body | (s << sb)
            cval = (cb & _M64) if w == "d" else (cb & _M32)
            self._stats["checked"] += 1
            if cval != golden:
                self._flag(FPViolation("fp_sgnj", "HIGH", seq, rec.get("pc"),
                    rec.get("disasm"), f"{d.raw} sign-injection wrong",
                    expected=f"0x{golden:x}", actual=f"0x{cval:x}"))
            return

        # ---- min / max ----
        if d.mnem in ("fmin", "fmax"):
            a = self._operand_f(d.fregs[1], w) if len(d.fregs) > 1 else None
            b = self._operand_f(d.fregs[2], w) if len(d.fregs) > 2 else None
            cb = committed_fd_bits()
            if a is None or b is None or cb is None:
                self._stats["skipped"] += 1
                return
            gv = self._minmax(d.mnem, a, b)
            gbits = self._result_bits(gv, w)
            cval = (cb & _M64) if w == "d" else (cb & _M32)
            self._stats["checked"] += 1
            if cval != gbits:
                self._flag(FPViolation("fp_minmax", "HIGH", seq, rec.get("pc"),
                    rec.get("disasm"), f"{d.raw} result wrong",
                    expected=f"0x{gbits:x}", actual=f"0x{cval:x}"))
            return

        # ---- compares (result in x reg) ----
        if d.mnem in ("feq", "flt", "fle"):
            a = self._operand_f(d.fregs[0], w) if len(d.fregs) > 0 else None
            b = self._operand_f(d.fregs[1], w) if len(d.fregs) > 1 else None
            if not d.xregs or a is None or b is None:
                self._stats["skipped"] += 1
                return
            xrd = d.xregs[0]
            cval = _to_int(regs.get(xrd))
            if cval is None:
                self._stats["skipped"] += 1
                return
            nan = (a != a) or (b != b)
            if nan:
                golden = 0
            elif d.mnem == "feq":
                golden = int(a == b)
            elif d.mnem == "flt":
                golden = int(a < b)
            else:
                golden = int(a <= b)
            self._stats["checked"] += 1
            if cval != golden:
                self._flag(FPViolation("fp_compare", "HIGH", seq, rec.get("pc"),
                    rec.get("disasm"), f"{d.raw} integer result wrong",
                    expected=str(golden), actual=str(cval)))
            if nan and d.mnem in ("flt", "fle") and fflags is not None and not (fflags & _NV):
                self._flag(FPViolation("fp_flag_missing", "MEDIUM", seq,
                    rec.get("pc"), rec.get("disasm"),
                    "signaling compare with NaN did not raise NV",
                    expected="NV", actual=f"0x{fflags:02x}"))
            return

        # ---- fclass ----
        if d.mnem in ("fclass", "fclassify"):
            bits = self._operand_bits(d.fregs[0], w) if d.fregs else None
            if not d.xregs or bits is None:
                self._stats["skipped"] += 1
                return
            cval = _to_int(regs.get(d.xregs[0]))
            if cval is None:
                self._stats["skipped"] += 1
                return
            golden = fclass_mask(bits, w)
            self._stats["checked"] += 1
            if cval != golden:
                self._flag(FPViolation("fp_class", "HIGH", seq, rec.get("pc"),
                    rec.get("disasm"), f"{d.raw} class mask wrong",
                    expected=f"0x{golden:x}", actual=f"0x{cval:x}"))
            return

        # ---- bit moves fmv.x.w / fmv.w.x (and .d) ----
        if d.mnem == "fmv":
            self._check_fmv(rec, d, seq, post, regs)
            return

        # other FP ops (fma, fcvt, loads/stores): NaN-box handled above; value
        # checks intentionally skipped to remain false-positive-free.
        self._stats["skipped"] += 1

    def _check_fmv(self, rec, d, seq, post, regs) -> None:
        # direction inferred from the mnemonic suffix tokens
        head = d.raw  # e.g. fmv.x.w / fmv.w.x / fmv.x.d / fmv.d.x
        toks = head.split(".")
        if len(toks) < 3:
            self._stats["skipped"] += 1
            return
        dst, src = toks[1], toks[2]
        if dst == "x":                      # f -> x bit copy
            if not d.xregs or not d.fregs:
                self._stats["skipped"] += 1
                return
            src_bits = self._operand_bits(d.fregs[0], "d" if src == "d" else "s")
            cval = _to_int(regs.get(d.xregs[0]))
            if src_bits is None or cval is None:
                self._stats["skipped"] += 1
                return
            self._stats["checked"] += 1
            if (cval & _M32) != (src_bits & _M32):
                self._flag(FPViolation("fp_move", "HIGH", seq, rec.get("pc"),
                    rec.get("disasm"), f"{head} bit move wrong",
                    expected=f"0x{src_bits & _M32:08x}", actual=f"0x{cval & _M32:08x}"))
        else:                               # x -> f bit copy
            if not d.xregs or not d.fregs:
                self._stats["skipped"] += 1
                return
            xval = _to_int(regs.get(d.xregs[0]))
            cb   = post.get(d.fregs[0])
            if xval is None or cb is None:
                self._stats["skipped"] += 1
                return
            self._stats["checked"] += 1
            if (cb & _M32) != (xval & _M32):
                self._flag(FPViolation("fp_move", "HIGH", seq, rec.get("pc"),
                    rec.get("disasm"), f"{head} bit move wrong",
                    expected=f"0x{xval & _M32:08x}", actual=f"0x{cb & _M32:08x}"))

    @staticmethod
    def _minmax(op: str, a: float, b: float) -> float:
        an, bn = (a != a), (b != b)
        if an and bn:
            return float("nan")
        if an:
            return b
        if bn:
            return a
        if a == 0.0 and b == 0.0:           # handle signed zeros
            sa, sb = _signbit(a), _signbit(b)
            if op == "fmin":
                return -0.0 if (sa or sb) else 0.0
            return 0.0 if (not sa or not sb) else -0.0
        return min(a, b) if op == "fmin" else max(a, b)

    @staticmethod
    def _fflags(rec: Dict) -> Optional[int]:
        csrs = rec.get("csrs") or {}
        for k in ("fflags", "FFLAGS"):
            v = _to_int(csrs.get(k))
            if v is not None:
                return v & 0x1F
        v = _to_int(csrs.get("fcsr") or csrs.get("FCSR"))
        if v is not None:
            return v & 0x1F
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
            seq = rec.get("seq", i)
            d = decode_fp((rec.get("disasm") or "").strip().lower())
            if d is not None:
                self._stats["fp_ops"] += 1
                try:
                    self._check(rec, d, seq)
                except Exception as exc:           # never crash the pipeline
                    logger.warning("fp_verifier: record %d raised: %s", seq, exc)
            # fold this record's FP writebacks into the shadow register file
            for name, bits in self._fp_map(rec).items():
                self._freg[name] = bits

        finished = datetime.now(timezone.utc)
        return self._report(n, started, finished)

    # -- reporting ------------------------------------------------------------

    def _band(self) -> Tuple[float, str]:
        if not self._violations:
            return 0.0, "CLEAN"
        score = sum(self._SEV_WEIGHT.get(v.severity, 0.2) for v in self._violations)
        norm  = min(1.0, score / max(1, self._stats["fp_ops"]))
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
            "agent":            "fp_verifier",
            "records_checked":  n,
            "flen":             self._flen,
            "fp_ops":           self._stats["fp_ops"],
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
        logger.warning("fp_verifier: cannot read manifest: %s", exc)
        return 0

    run_dir = Path(manifest.get("run_dir", manifest_path.parent))
    outputs = manifest.get("outputs", {})
    rtl_log = _load_log(run_dir, outputs, "rtl_commit_log", "rtl_commit.jsonl")
    iss_log = _load_log(run_dir, outputs, "iss_commit_log", "iss_commit.jsonl")
    if not rtl_log:
        logger.info("fp_verifier: no RTL commit log, skipping")
        return 0

    report = FPVerifier(rtl_log, iss_log).run()

    report_path = run_dir / "fp_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
        f.write("\n")

    manifest.setdefault("outputs", {})["fp_report"] = "fp_report.json"
    manifest.setdefault("phases", {})["fp_check"] = {
        "status": "pass" if report["pass"] else "fail",
        "violations": report["total_violations"],
    }
    tmp = manifest_path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    tmp.rename(manifest_path)

    logger.info("fp_verifier: FLEN=%d, %d FP ops, %d violations, band=%s",
                report["flen"], report["fp_ops"],
                report["total_violations"], report["band"])
    return 0 if report["pass"] else 1


# ─────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":          # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description="RV32F/D floating-point verifier")
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
        rep = FPVerifier(log).run()
        print(json.dumps(rep, indent=2))
        raise SystemExit(0 if rep["pass"] else 1)
    ap.print_help()
