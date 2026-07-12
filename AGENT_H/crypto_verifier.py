"""
AGENT_H.crypto_verifier — Scalar Cryptography Checker (T54)
============================================================

Golden-reference checker for the RISC-V scalar-cryptography transform
instructions — the SHA-256 / SHA-512 (Zknh) and SM3 (Zksh) message-schedule and
compression helpers. These are pure single-source bit functions with an *exact*
golden model (rotates, shifts, XORs), so a mismatch is an unambiguous datapath
bug — and crypto bugs are both critical (they silently break security) and easy
to miss (the output still "looks random").

Modelled instructions (single source `rs1` → `rd`)
--------------------------------------------------
- **SHA-256** (32-bit): `sha256sig0/sig1/sum0/sum1`
- **SHA-512** (RV64, 64-bit): `sha512sig0/sig1/sum0/sum1`
- **SM3**    (32-bit): `sm3p0`, `sm3p1`

Each `Σ/σ` function is defined by the standard rotate/shift/xor recipe (below);
the agent recomputes it from the source operand and compares to the committed
`rd`.

Operand recovery
----------------
The commit log records the *written* register; the source `rs1` is recovered
from a **golden shadow register file** the agent maintains across the trace
(read before the instruction's own write is applied). Runs on the standard
commit log — no separate trace.

Check
-----
- **crypto_result** (HIGH) — committed `rd` ≠ the golden transform of `rs1`.

Stdlib-only, schema-v2.1.0 report, graceful degradation throughout.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

log = logging.getLogger("AGENT_H.crypto")

SCHEMA_VERSION = "2.1.0"
AGENT_NAME = "crypto_verifier"
_ABI = {"zero": 0, "ra": 1, "sp": 2, "gp": 3, "tp": 4, "fp": 8,
        "t0": 5, "t1": 6, "t2": 7, "s0": 8, "s1": 9,
        "a0": 10, "a1": 11, "a2": 12, "a3": 13, "a4": 14, "a5": 15,
        "a6": 16, "a7": 17, "s2": 18, "s3": 19, "s4": 20, "s5": 21}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_int(v: Any) -> Optional[int]:
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        try:
            return int(v, 16) if v.lower().startswith("0x") else int(v)
        except ValueError:
            return None
    return None


def _reg_idx(tok: str) -> Optional[int]:
    t = tok.strip().strip("()")
    m = re.fullmatch(r"x(\d+)", t)
    if m:
        return int(m.group(1))
    return _ABI.get(t)


def _rotr(x: int, n: int, w: int) -> int:
    m = (1 << w) - 1
    x &= m
    n %= w
    return ((x >> n) | (x << (w - n))) & m


def _rotl(x: int, n: int, w: int) -> int:
    m = (1 << w) - 1
    x &= m
    n %= w
    return ((x << n) | (x >> (w - n))) & m


def _shr(x: int, n: int, w: int) -> int:
    return (x & ((1 << w) - 1)) >> n


# ─────────────────────────────────────────────────────────────────────────────
# Golden transforms
# ─────────────────────────────────────────────────────────────────────────────
def _sha256(name: str, x: int) -> int:
    w = 32
    if name == "sha256sum0":
        return _rotr(x, 2, w) ^ _rotr(x, 13, w) ^ _rotr(x, 22, w)
    if name == "sha256sum1":
        return _rotr(x, 6, w) ^ _rotr(x, 11, w) ^ _rotr(x, 25, w)
    if name == "sha256sig0":
        return _rotr(x, 7, w) ^ _rotr(x, 18, w) ^ _shr(x, 3, w)
    if name == "sha256sig1":
        return _rotr(x, 17, w) ^ _rotr(x, 19, w) ^ _shr(x, 10, w)
    raise KeyError(name)


def _sha512(name: str, x: int) -> int:
    w = 64
    if name == "sha512sum0":
        return _rotr(x, 28, w) ^ _rotr(x, 34, w) ^ _rotr(x, 39, w)
    if name == "sha512sum1":
        return _rotr(x, 14, w) ^ _rotr(x, 18, w) ^ _rotr(x, 41, w)
    if name == "sha512sig0":
        return _rotr(x, 1, w) ^ _rotr(x, 8, w) ^ _shr(x, 7, w)
    if name == "sha512sig1":
        return _rotr(x, 19, w) ^ _rotr(x, 61, w) ^ _shr(x, 6, w)
    raise KeyError(name)


def _sm3(name: str, x: int) -> int:
    w = 32
    if name == "sm3p0":
        return (x ^ _rotl(x, 9, w) ^ _rotl(x, 17, w)) & 0xFFFFFFFF
    if name == "sm3p1":
        return (x ^ _rotl(x, 15, w) ^ _rotl(x, 23, w)) & 0xFFFFFFFF
    raise KeyError(name)


_TRANSFORMS: Dict[str, Callable[[str, int], int]] = {}
for _n in ("sha256sig0", "sha256sig1", "sha256sum0", "sha256sum1"):
    _TRANSFORMS[_n] = _sha256
for _n in ("sha512sig0", "sha512sig1", "sha512sum0", "sha512sum1"):
    _TRANSFORMS[_n] = _sha512
for _n in ("sm3p0", "sm3p1"):
    _TRANSFORMS[_n] = _sm3

_WIDTH = {"sha256": 32, "sha512": 64, "sm3": 32}


def crypto_golden(mnem: str, x: int) -> Optional[int]:
    fn = _TRANSFORMS.get(mnem)
    return fn(mnem, x) if fn else None


# ─────────────────────────────────────────────────────────────────────────────
# Zbkb / Zbkx — bit-manipulation for cryptography (exact bit functions)
# ─────────────────────────────────────────────────────────────────────────────
_ZBK_ONE = {"brev8", "zip", "unzip"}                 # single source
_ZBK_TWO = {"pack", "packh", "packw", "xperm8", "xperm4"}  # two source


def _sext32(v: int) -> int:
    v &= 0xFFFFFFFF
    return v - (1 << 32) if v & 0x80000000 else v


def zbk_one(mnem: str, x: int, w: int) -> Optional[int]:
    if mnem == "brev8":                              # reverse bits within each byte
        out = 0
        for b in range(w // 8):
            byte = (x >> (b * 8)) & 0xFF
            r = int(f"{byte:08b}"[::-1], 2)
            out |= r << (b * 8)
        return out
    if mnem == "zip":                                # RV32: interleave low/high halves
        out = 0
        for i in range(16):
            out |= ((x >> i) & 1) << (2 * i)
            out |= ((x >> (i + 16)) & 1) << (2 * i + 1)
        return out & 0xFFFFFFFF
    if mnem == "unzip":                              # RV32: inverse of zip
        out = 0
        for i in range(16):
            out |= ((x >> (2 * i)) & 1) << i
            out |= ((x >> (2 * i + 1)) & 1) << (i + 16)
        return out & 0xFFFFFFFF
    return None


def zbk_two(mnem: str, a: int, b: int, w: int) -> Optional[int]:
    if mnem == "pack":                               # combine low halves
        h = w // 2
        mask = (1 << h) - 1
        return ((a & mask) | ((b & mask) << h)) & ((1 << w) - 1)
    if mnem == "packh":                              # low bytes → [15:0]
        return (a & 0xFF) | ((b & 0xFF) << 8)
    if mnem == "packw":                              # RV64: low 16s → 32, sign-extended
        return _sext32((a & 0xFFFF) | ((b & 0xFFFF) << 16)) & ((1 << w) - 1)
    if mnem == "xperm8":                             # byte permute (b = index vector)
        nb = w // 8
        out = 0
        for i in range(nb):
            idx = (b >> (i * 8)) & 0xFF
            byte = (a >> (idx * 8)) & 0xFF if idx < nb else 0
            out |= byte << (i * 8)
        return out
    if mnem == "xperm4":                             # nibble permute
        nn = w // 4
        out = 0
        for i in range(nn):
            idx = (b >> (i * 4)) & 0xF
            nib = (a >> (idx * 4)) & 0xF if idx < nn else 0
            out |= nib << (i * 4)
        return out
    return None


class CryptoVerifier:
    def __init__(self, records: Sequence[Dict[str, Any]]):
        self.recs = [r for r in (records or []) if isinstance(r, dict)]
        self.violations: List[Dict[str, Any]] = []
        self.shadow: Dict[int, int] = {0: 0}
        self.xlen = 64 if self._detect_rv64() else 32
        self.metrics: Dict[str, Any] = {"crypto_ops": 0, "checked": 0,
                                        "by_op": {}, "crypto_active": False}

    def _detect_rv64(self) -> bool:
        for rec in self.recs:
            regs = rec.get("regs", {})
            if isinstance(regs, dict):
                for v in regs.values():
                    iv = _to_int(v)
                    if iv is not None and iv > 0xFFFFFFFF:
                        return True
        return False

    def _v(self, seq: Any, detail: str) -> None:
        self.violations.append({"seq": seq, "check": "crypto_result",
                                "severity": "HIGH", "detail": detail})

    def run(self) -> Dict[str, Any]:
        started = _now()
        for i, rec in enumerate(self.recs):
            dis = rec.get("disasm", "")
            mnem = dis.split()[0].lower() if isinstance(dis, str) and dis.split() else ""
            if mnem in _TRANSFORMS:
                self._check(rec, i, mnem, dis)
            elif mnem in _ZBK_ONE or mnem in _ZBK_TWO:
                self._check_zbk(rec, i, mnem, dis)
            # update shadow with this record's register writes (after the check)
            regs = rec.get("regs", {})
            if isinstance(regs, dict):
                for rn, rv in regs.items():
                    idx = _reg_idx(str(rn))
                    iv = _to_int(rv)
                    if idx is not None and idx != 0 and iv is not None:
                        self.shadow[idx] = iv
        return self._report(started)

    def _check(self, rec: Dict[str, Any], seq: int, mnem: str, dis: str) -> None:
        self.metrics["crypto_ops"] += 1
        self.metrics["crypto_active"] = True
        self.metrics["by_op"][mnem] = self.metrics["by_op"].get(mnem, 0) + 1
        toks = dis.replace(",", " ").split()
        if len(toks) < 3:
            return
        rd_i, rs1_i = _reg_idx(toks[1]), _reg_idx(toks[2])
        if rd_i is None or rs1_i is None:
            return
        src = self.shadow.get(rs1_i, 0)
        golden = crypto_golden(mnem, src)
        if golden is None:
            return
        # committed rd value
        regs = rec.get("regs", {})
        rd_val = _to_int(regs.get(f"x{rd_i}")) if isinstance(regs, dict) else None
        if rd_val is None:
            for rn, rv in (regs or {}).items():
                if _reg_idx(str(rn)) == rd_i:
                    rd_val = _to_int(rv)
                    break
        if rd_val is None:
            return
        w = _WIDTH["sha512" if mnem.startswith("sha512") else
                  "sha256" if mnem.startswith("sha256") else "sm3"]
        mask = (1 << w) - 1
        self.metrics["checked"] += 1
        if (rd_val & mask) != (golden & mask):
            self._v(seq, f"{mnem}(rs1={hex(src)}): rd={hex(rd_val & mask)} "
                         f"!= golden {hex(golden & mask)}")

    def _rd_val(self, rec: Dict[str, Any], rd_i: int) -> Optional[int]:
        regs = rec.get("regs", {})
        if not isinstance(regs, dict):
            return None
        v = _to_int(regs.get(f"x{rd_i}"))
        if v is not None:
            return v
        for rn, rv in regs.items():
            if _reg_idx(str(rn)) == rd_i:
                return _to_int(rv)
        return None

    def _check_zbk(self, rec: Dict[str, Any], seq: int, mnem: str, dis: str) -> None:
        self.metrics["crypto_ops"] += 1
        self.metrics["crypto_active"] = True
        self.metrics["by_op"][mnem] = self.metrics["by_op"].get(mnem, 0) + 1
        toks = dis.replace(",", " ").split()
        if mnem in _ZBK_ONE:
            if len(toks) < 3:
                return
            rd_i, rs1_i = _reg_idx(toks[1]), _reg_idx(toks[2])
            if rd_i is None or rs1_i is None:
                return
            w = 32 if mnem in ("zip", "unzip") else self.xlen
            golden = zbk_one(mnem, self.shadow.get(rs1_i, 0), w)
        else:
            if len(toks) < 4:
                return
            rd_i, rs1_i, rs2_i = (_reg_idx(toks[1]), _reg_idx(toks[2]),
                                  _reg_idx(toks[3]))
            if rd_i is None or rs1_i is None or rs2_i is None:
                return
            golden = zbk_two(mnem, self.shadow.get(rs1_i, 0),
                             self.shadow.get(rs2_i, 0), self.xlen)
        if golden is None:
            return
        rd_val = self._rd_val(rec, rd_i)
        if rd_val is None:
            return
        mask = (1 << self.xlen) - 1
        self.metrics["checked"] += 1
        if (rd_val & mask) != (golden & mask):
            self._v(seq, f"{mnem}: rd={hex(rd_val & mask)} "
                         f"!= golden {hex(golden & mask)}")

    def _report(self, started: str) -> Dict[str, Any]:
        total = len(self.violations)
        return {
            "schema_version": SCHEMA_VERSION,
            "agent": AGENT_NAME,
            "started_at": started,
            "finished_at": _now(),
            "records_checked": len(self.recs),
            "crypto_active": self.metrics["crypto_active"],
            "metrics": self.metrics,
            "total_violations": total,
            "high_violations": total,
            "severity_score": total * 3,
            "band": "CLEAN" if total == 0 else "CRITICAL",
            "pass": total == 0,
            "violations": self.violations[:100],
        }


# ─────────────────────────────────────────────────────────────────────────────
# Manifest entry point
# ─────────────────────────────────────────────────────────────────────────────
def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return out


def run_from_manifest(manifest_path: str) -> int:
    mp = Path(manifest_path)
    try:
        manifest = json.loads(mp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("crypto_verifier: cannot read manifest: %s", exc)
        return 0
    run_dir = Path(manifest.get("run_dir", mp.parent))
    outputs = manifest.get("outputs", {})
    recs = _load_jsonl(run_dir / outputs.get("rtl_commit_log", "rtl_commit.jsonl"))
    rep = CryptoVerifier(recs).run()
    if not rep.get("crypto_active"):
        rep = {"schema_version": SCHEMA_VERSION, "agent": AGENT_NAME,
               "status": "skipped", "reason": "no crypto instructions", "pass": True}
    else:
        rep["status"] = "completed"
    try:
        (run_dir / "crypto_report.json").write_text(json.dumps(rep, indent=2),
                                                    encoding="utf-8")
    except OSError as exc:
        log.warning("crypto_verifier: cannot write report: %s", exc)
    return 0 if rep.get("pass", True) else 1


if __name__ == "__main__":  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="AVA scalar-cryptography checker")
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args()
    raise SystemExit(run_from_manifest(args.manifest))
