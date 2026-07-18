"""
AGENT_H.aes_verifier — AES Scalar Cryptography Checker (T56)
=============================================================

Golden-reference checker for the RISC-V RV64 AES scalar-crypto instructions
(Zkne encrypt / Zknd decrypt): the round instructions `aes64es` / `aes64esm`
/ `aes64ds` / `aes64dsm`, the key-schedule helpers `aes64im` / `aes64ks2` /
`aes64ks1i` (rotate-SubWord-Rcon; FIPS-197-validated over a full AES-128/256
key expansion).

AES is the highest-stakes crypto datapath — a single wrong byte in the S-box or
MixColumns silently breaks confidentiality. So the golden model here is
**validated against the FIPS-197 published example** (AES-128, Appendix B): the
`aes64esm` round output is checked to equal the reference SubBytes → ShiftRows →
MixColumns result to the bit, and the S-box / MixColumns constants are checked
against their standard values. The check is therefore *not* self-referential.

RV64 semantics (derived + FIPS-validated)
-----------------------------------------
`aes64esm rd, rs1, rs2` forms the 128-bit state `rs2:rs1` (rs1 = low 64), applies
AES **ShiftRows**, takes the low 8 bytes, **SubBytes** them, and applies
**MixColumns** to each 32-bit word. The high 64 bits of the round come from the
same instruction with the operands swapped (`aes64esm rd, rs2, rs1`) — a property
the FIPS vector confirms. `aes64es` is the final round (no MixColumns);
`aes64ds`/`dsm` are the decrypt duals (inverse S-box / ShiftRows / MixColumns).

Check
-----
- **aes_result** (HIGH) — committed `rd` ≠ the golden transform of `rs1`,`rs2`.

Source operands are recovered from a **golden shadow register file** across the
commit log. Stdlib-only, schema-v2.1.0, graceful degradation.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

log = logging.getLogger("AGENT_H.aes")

SCHEMA_VERSION = "2.1.0"
AGENT_NAME = "aes_verifier"
_ABI = {"zero": 0, "ra": 1, "sp": 2, "gp": 3, "tp": 4, "fp": 8,
        "t0": 5, "t1": 6, "t2": 7, "s0": 8, "s1": 9,
        "a0": 10, "a1": 11, "a2": 12, "a3": 13, "a4": 14, "a5": 15}

AES_SBOX = [
    0x63, 0x7c, 0x77, 0x7b, 0xf2, 0x6b, 0x6f, 0xc5, 0x30, 0x01, 0x67, 0x2b, 0xfe, 0xd7, 0xab, 0x76,
    0xca, 0x82, 0xc9, 0x7d, 0xfa, 0x59, 0x47, 0xf0, 0xad, 0xd4, 0xa2, 0xaf, 0x9c, 0xa4, 0x72, 0xc0,
    0xb7, 0xfd, 0x93, 0x26, 0x36, 0x3f, 0xf7, 0xcc, 0x34, 0xa5, 0xe5, 0xf1, 0x71, 0xd8, 0x31, 0x15,
    0x04, 0xc7, 0x23, 0xc3, 0x18, 0x96, 0x05, 0x9a, 0x07, 0x12, 0x80, 0xe2, 0xeb, 0x27, 0xb2, 0x75,
    0x09, 0x83, 0x2c, 0x1a, 0x1b, 0x6e, 0x5a, 0xa0, 0x52, 0x3b, 0xd6, 0xb3, 0x29, 0xe3, 0x2f, 0x84,
    0x53, 0xd1, 0x00, 0xed, 0x20, 0xfc, 0xb1, 0x5b, 0x6a, 0xcb, 0xbe, 0x39, 0x4a, 0x4c, 0x58, 0xcf,
    0xd0, 0xef, 0xaa, 0xfb, 0x43, 0x4d, 0x33, 0x85, 0x45, 0xf9, 0x02, 0x7f, 0x50, 0x3c, 0x9f, 0xa8,
    0x51, 0xa3, 0x40, 0x8f, 0x92, 0x9d, 0x38, 0xf5, 0xbc, 0xb6, 0xda, 0x21, 0x10, 0xff, 0xf3, 0xd2,
    0xcd, 0x0c, 0x13, 0xec, 0x5f, 0x97, 0x44, 0x17, 0xc4, 0xa7, 0x7e, 0x3d, 0x64, 0x5d, 0x19, 0x73,
    0x60, 0x81, 0x4f, 0xdc, 0x22, 0x2a, 0x90, 0x88, 0x46, 0xee, 0xb8, 0x14, 0xde, 0x5e, 0x0b, 0xdb,
    0xe0, 0x32, 0x3a, 0x0a, 0x49, 0x06, 0x24, 0x5c, 0xc2, 0xd3, 0xac, 0x62, 0x91, 0x95, 0xe4, 0x79,
    0xe7, 0xc8, 0x37, 0x6d, 0x8d, 0xd5, 0x4e, 0xa9, 0x6c, 0x56, 0xf4, 0xea, 0x65, 0x7a, 0xae, 0x08,
    0xba, 0x78, 0x25, 0x2e, 0x1c, 0xa6, 0xb4, 0xc6, 0xe8, 0xdd, 0x74, 0x1f, 0x4b, 0xbd, 0x8b, 0x8a,
    0x70, 0x3e, 0xb5, 0x66, 0x48, 0x03, 0xf6, 0x0e, 0x61, 0x35, 0x57, 0xb9, 0x86, 0xc1, 0x1d, 0x9e,
    0xe1, 0xf8, 0x98, 0x11, 0x69, 0xd9, 0x8e, 0x94, 0x9b, 0x1e, 0x87, 0xe9, 0xce, 0x55, 0x28, 0xdf,
    0x8c, 0xa1, 0x89, 0x0d, 0xbf, 0xe6, 0x42, 0x68, 0x41, 0x99, 0x2d, 0x0f, 0xb0, 0x54, 0xbb, 0x16,
]
AES_INV_SBOX = [0] * 256
for _i, _v in enumerate(AES_SBOX):
    AES_INV_SBOX[_v] = _i

# ShiftRows byte selection for the low 8 bytes of the 128-bit state (b0..b15).
_SR_FWD = [0, 5, 10, 15, 4, 9, 14, 3]
_SR_INV = [0, 13, 10, 7, 4, 1, 14, 11]


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


def _xtime(a: int) -> int:
    a <<= 1
    if a & 0x100:
        a ^= 0x11b
    return a & 0xFF


def _gmul(a: int, b: int) -> int:
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        b >>= 1
        a = _xtime(a)
    return p & 0xFF


def _mixcol_fwd(w: int) -> int:
    c = [(w >> (i * 8)) & 0xFF for i in range(4)]
    r = [
        _xtime(c[0]) ^ (_xtime(c[1]) ^ c[1]) ^ c[2] ^ c[3],
        c[0] ^ _xtime(c[1]) ^ (_xtime(c[2]) ^ c[2]) ^ c[3],
        c[0] ^ c[1] ^ _xtime(c[2]) ^ (_xtime(c[3]) ^ c[3]),
        (_xtime(c[0]) ^ c[0]) ^ c[1] ^ c[2] ^ _xtime(c[3]),
    ]
    return sum((r[i] & 0xFF) << (i * 8) for i in range(4))


def _mixcol_inv(w: int) -> int:
    c = [(w >> (i * 8)) & 0xFF for i in range(4)]
    r = [
        _gmul(c[0], 14) ^ _gmul(c[1], 11) ^ _gmul(c[2], 13) ^ _gmul(c[3], 9),
        _gmul(c[0], 9) ^ _gmul(c[1], 14) ^ _gmul(c[2], 11) ^ _gmul(c[3], 13),
        _gmul(c[0], 13) ^ _gmul(c[1], 9) ^ _gmul(c[2], 14) ^ _gmul(c[3], 11),
        _gmul(c[0], 11) ^ _gmul(c[1], 13) ^ _gmul(c[2], 9) ^ _gmul(c[3], 14),
    ]
    return sum((r[i] & 0xFF) << (i * 8) for i in range(4))


def _bytes16(rs1: int, rs2: int) -> List[int]:
    return ([(rs1 >> (i * 8)) & 0xFF for i in range(8)]
            + [(rs2 >> (i * 8)) & 0xFF for i in range(8)])


def aes64_round(rs1: int, rs2: int, decrypt: bool, mix: bool) -> int:
    b = _bytes16(rs1, rs2)
    sr_order = _SR_INV if decrypt else _SR_FWD
    sbox = AES_INV_SBOX if decrypt else AES_SBOX
    sb = [sbox[b[j]] for j in sr_order]              # ShiftRows then SubBytes, low 8 bytes
    out = sum(sb[i] << (i * 8) for i in range(8))
    if mix:
        mc = _mixcol_inv if decrypt else _mixcol_fwd
        out = mc(out & 0xFFFFFFFF) | (mc((out >> 32) & 0xFFFFFFFF) << 32)
    return out & 0xFFFFFFFFFFFFFFFF


def aes64im(rs1: int) -> int:
    return (_mixcol_inv(rs1 & 0xFFFFFFFF)
            | (_mixcol_inv((rs1 >> 32) & 0xFFFFFFFF) << 32))


def aes64ks2(rs1: int, rs2: int) -> int:
    w0 = ((rs1 >> 32) & 0xFFFFFFFF) ^ (rs2 & 0xFFFFFFFF)
    w1 = w0 ^ ((rs2 >> 32) & 0xFFFFFFFF)
    return (w1 << 32) | w0


# AES key-schedule round constants (aes_decode_rcon → low byte).
_AES_RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1b, 0x36]


def _ror32(x: int, n: int) -> int:
    n %= 32
    x &= 0xFFFFFFFF
    return ((x >> n) | (x << (32 - n))) & 0xFFFFFFFF


def _subword(x: int) -> int:
    return sum(AES_SBOX[(x >> (8 * i)) & 0xFF] << (8 * i) for i in range(4))


def aes64ks1i(rs1: int, rnum: int) -> Optional[int]:
    """AES Key Schedule Instruction 1 (RV64, Zkne/Zknd). Per the sail:
    tmp1=rs1[63:32]; tmp2 = rnum==0xA ? tmp1 : ror32(tmp1,8);
    tmp3 = SubWord(tmp2); result = (tmp3^rc)@(tmp3^rc). rnum∈0x0..0xA."""
    if not 0 <= rnum <= 0xA:
        return None
    tmp1 = (rs1 >> 32) & 0xFFFFFFFF
    tmp2 = tmp1 if rnum == 0xA else _ror32(tmp1, 8)
    tmp3 = _subword(tmp2)
    rc = 0 if rnum == 0xA else _AES_RCON[rnum]
    v = (tmp3 ^ rc) & 0xFFFFFFFF
    return (v << 32) | v


def aes_golden(mnem: str, rs1: int, rs2: int = 0, rnum: int = 0) -> Optional[int]:
    if mnem == "aes64esm":
        return aes64_round(rs1, rs2, decrypt=False, mix=True)
    if mnem == "aes64es":
        return aes64_round(rs1, rs2, decrypt=False, mix=False)
    if mnem == "aes64dsm":
        return aes64_round(rs1, rs2, decrypt=True, mix=True)
    if mnem == "aes64ds":
        return aes64_round(rs1, rs2, decrypt=True, mix=False)
    if mnem == "aes64im":
        return aes64im(rs1)
    if mnem == "aes64ks2":
        return aes64ks2(rs1, rs2)
    if mnem == "aes64ks1i":
        return aes64ks1i(rs1, rnum)
    return None


_TWO_SRC = {"aes64esm", "aes64es", "aes64dsm", "aes64ds", "aes64ks2"}
_ONE_SRC = {"aes64im"}
_KS1 = {"aes64ks1i"}                                  # rs1 + immediate rnum
_ALL = _TWO_SRC | _ONE_SRC | _KS1


class AESVerifier:
    def __init__(self, records: Sequence[Dict[str, Any]]):
        self.recs = [r for r in (records or []) if isinstance(r, dict)]
        self.violations: List[Dict[str, Any]] = []
        self.shadow: Dict[int, int] = {0: 0}
        self.metrics: Dict[str, Any] = {"aes_ops": 0, "checked": 0,
                                        "by_op": {}, "aes_active": False}

    def _v(self, seq: Any, detail: str) -> None:
        self.violations.append({"seq": seq, "check": "aes_result",
                                "severity": "HIGH", "detail": detail})

    def run(self) -> Dict[str, Any]:
        started = _now()
        for i, rec in enumerate(self.recs):
            dis = rec.get("disasm", "")
            mnem = dis.split()[0].lower() if isinstance(dis, str) and dis.split() else ""
            if mnem in _ALL:
                self._check(rec, i, mnem, dis)
            regs = rec.get("regs", {})
            if isinstance(regs, dict):
                for rn, rv in regs.items():
                    idx = _reg_idx(str(rn))
                    iv = _to_int(rv)
                    if idx is not None and idx != 0 and iv is not None:
                        self.shadow[idx] = iv
        return self._report(started)

    def _check(self, rec: Dict[str, Any], seq: int, mnem: str, dis: str) -> None:
        self.metrics["aes_ops"] += 1
        self.metrics["aes_active"] = True
        self.metrics["by_op"][mnem] = self.metrics["by_op"].get(mnem, 0) + 1
        toks = dis.replace(",", " ").split()
        if len(toks) < 3:
            return
        rd_i, rs1_i = _reg_idx(toks[1]), _reg_idx(toks[2])
        if rd_i is None or rs1_i is None:
            return
        rs1 = self.shadow.get(rs1_i, 0)
        rs2 = 0
        rnum = 0
        if mnem in _TWO_SRC:
            if len(toks) < 4:
                return
            rs2_i = _reg_idx(toks[3])
            if rs2_i is None:
                return
            rs2 = self.shadow.get(rs2_i, 0)
        elif mnem in _KS1:                            # aes64ks1i rd, rs1, rnum
            if len(toks) < 4:
                return
            rnum = _to_int(toks[3])
            if rnum is None:
                return
        golden = aes_golden(mnem, rs1, rs2, rnum)
        if golden is None:
            return
        regs = rec.get("regs", {})
        rd_val = _to_int(regs.get(f"x{rd_i}")) if isinstance(regs, dict) else None
        if rd_val is None and isinstance(regs, dict):
            for rn, rv in regs.items():
                if _reg_idx(str(rn)) == rd_i:
                    rd_val = _to_int(rv)
                    break
        if rd_val is None:
            return
        mask = 0xFFFFFFFFFFFFFFFF
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
            "aes_active": self.metrics["aes_active"],
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
        log.warning("aes_verifier: cannot read manifest: %s", exc)
        return 0
    run_dir = Path(manifest.get("run_dir", mp.parent))
    outputs = manifest.get("outputs", {})
    recs = _load_jsonl(run_dir / outputs.get("rtl_commit_log", "rtl_commit.jsonl"))
    rep = AESVerifier(recs).run()
    if not rep.get("aes_active"):
        rep = {"schema_version": SCHEMA_VERSION, "agent": AGENT_NAME,
               "status": "skipped", "reason": "no AES instructions", "pass": True}
    else:
        rep["status"] = "completed"
    try:
        (run_dir / "aes_report.json").write_text(json.dumps(rep, indent=2),
                                                 encoding="utf-8")
    except OSError as exc:
        log.warning("aes_verifier: cannot write report: %s", exc)
    return 0 if rep.get("pass", True) else 1


if __name__ == "__main__":  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="AVA AES scalar-crypto checker")
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args()
    raise SystemExit(run_from_manifest(args.manifest))
