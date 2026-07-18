"""
AGENT_H.sm4_verifier — SM4 Scalar Cryptography Checker (T57)
============================================================

Golden-reference checker for the RISC-V SM4 scalar-crypto instructions (Zksed):
`sm4ed` (the cipher round) and `sm4ks` (the key-schedule round). SM4 is the
Chinese national block-cipher standard (GB/T 32907-2016); like AES, a wrong
S-box byte or linear-transform bit silently breaks confidentiality, so the
golden model is **validated against the published GB/T test vector** (see the
test suite) — it is not self-referential.

Instruction semantics (RV, byte-oriented)
-----------------------------------------
`sm4ed rd, rs1, rs2, bs` / `sm4ks rd, rs1, rs2, bs`:
1. select byte `bs` (0..3) of `rs2`, apply the SM4 **S-box**,
2. zero-extend to 32 bits and apply the **linear transform** —
   `L(x)  = x ^ (x⋘2) ^ (x⋘10) ^ (x⋘18) ^ (x⋘24)`  for `sm4ed`,
   `L'(x) = x ^ (x⋘13) ^ (x⋘23)`                     for `sm4ks`,
3. rotate the result left by `8·bs`, and XOR into `rs1`.

Chaining the four byte positions (`bs=0..3`) computes the SM4 T-function on a
32-bit word — the property the full-cipher validation relies on.

Check
-----
- **sm4_result** (HIGH) — committed `rd` ≠ the golden transform.

Source operands from a golden shadow register file; rides the standard commit
log. Stdlib-only, schema-v2.1.0, graceful degradation.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

log = logging.getLogger("AGENT_H.sm4")

SCHEMA_VERSION = "2.1.0"
AGENT_NAME = "sm4_verifier"
_ABI = {"zero": 0, "ra": 1, "sp": 2, "gp": 3, "tp": 4, "fp": 8,
        "t0": 5, "t1": 6, "t2": 7, "s0": 8, "s1": 9,
        "a0": 10, "a1": 11, "a2": 12, "a3": 13, "a4": 14, "a5": 15}

SM4_SBOX = [
    0xd6, 0x90, 0xe9, 0xfe, 0xcc, 0xe1, 0x3d, 0xb7, 0x16, 0xb6, 0x14, 0xc2, 0x28, 0xfb, 0x2c, 0x05,
    0x2b, 0x67, 0x9a, 0x76, 0x2a, 0xbe, 0x04, 0xc3, 0xaa, 0x44, 0x13, 0x26, 0x49, 0x86, 0x06, 0x99,
    0x9c, 0x42, 0x50, 0xf4, 0x91, 0xef, 0x98, 0x7a, 0x33, 0x54, 0x0b, 0x43, 0xed, 0xcf, 0xac, 0x62,
    0xe4, 0xb3, 0x1c, 0xa9, 0xc9, 0x08, 0xe8, 0x95, 0x80, 0xdf, 0x94, 0xfa, 0x75, 0x8f, 0x3f, 0xa6,
    0x47, 0x07, 0xa7, 0xfc, 0xf3, 0x73, 0x17, 0xba, 0x83, 0x59, 0x3c, 0x19, 0xe6, 0x85, 0x4f, 0xa8,
    0x68, 0x6b, 0x81, 0xb2, 0x71, 0x64, 0xda, 0x8b, 0xf8, 0xeb, 0x0f, 0x4b, 0x70, 0x56, 0x9d, 0x35,
    0x1e, 0x24, 0x0e, 0x5e, 0x63, 0x58, 0xd1, 0xa2, 0x25, 0x22, 0x7c, 0x3b, 0x01, 0x21, 0x78, 0x87,
    0xd4, 0x00, 0x46, 0x57, 0x9f, 0xd3, 0x27, 0x52, 0x4c, 0x36, 0x02, 0xe7, 0xa0, 0xc4, 0xc8, 0x9e,
    0xea, 0xbf, 0x8a, 0xd2, 0x40, 0xc7, 0x38, 0xb5, 0xa3, 0xf7, 0xf2, 0xce, 0xf9, 0x61, 0x15, 0xa1,
    0xe0, 0xae, 0x5d, 0xa4, 0x9b, 0x34, 0x1a, 0x55, 0xad, 0x93, 0x32, 0x30, 0xf5, 0x8c, 0xb1, 0xe3,
    0x1d, 0xf6, 0xe2, 0x2e, 0x82, 0x66, 0xca, 0x60, 0xc0, 0x29, 0x23, 0xab, 0x0d, 0x53, 0x4e, 0x6f,
    0xd5, 0xdb, 0x37, 0x45, 0xde, 0xfd, 0x8e, 0x2f, 0x03, 0xff, 0x6a, 0x72, 0x6d, 0x6c, 0x5b, 0x51,
    0x8d, 0x1b, 0xaf, 0x92, 0xbb, 0xdd, 0xbc, 0x7f, 0x11, 0xd9, 0x5c, 0x41, 0x1f, 0x10, 0x5a, 0xd8,
    0x0a, 0xc1, 0x31, 0x88, 0xa5, 0xcd, 0x7b, 0xbd, 0x2d, 0x74, 0xd0, 0x12, 0xb8, 0xe5, 0xb4, 0xb0,
    0x89, 0x69, 0x97, 0x4a, 0x0c, 0x96, 0x77, 0x7e, 0x65, 0xb9, 0xf1, 0x09, 0xc5, 0x6e, 0xc6, 0x84,
    0x18, 0xf0, 0x7d, 0xec, 0x3a, 0xdc, 0x4d, 0x20, 0x79, 0xee, 0x5f, 0x3e, 0xd7, 0xcb, 0x39, 0x48,
]


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


def _rotl32(x: int, n: int) -> int:
    x &= 0xFFFFFFFF
    n &= 31
    return ((x << n) | (x >> (32 - n))) & 0xFFFFFFFF


def _L_round(x: int) -> int:
    return x ^ _rotl32(x, 2) ^ _rotl32(x, 10) ^ _rotl32(x, 18) ^ _rotl32(x, 24)


def _L_key(x: int) -> int:
    return x ^ _rotl32(x, 13) ^ _rotl32(x, 23)


def sm4_golden(mnem: str, rs1: int, rs2: int, bs: int) -> Optional[int]:
    bs &= 3
    sb = SM4_SBOX[(rs2 >> (8 * bs)) & 0xFF]          # byte bs of rs2, S-boxed
    if mnem == "sm4ed":
        y = _L_round(sb)
    elif mnem == "sm4ks":
        y = _L_key(sb)
    else:
        return None
    z = _rotl32(y, 8 * bs)
    return (rs1 ^ z) & 0xFFFFFFFF


class SM4Verifier:
    def __init__(self, records: Sequence[Dict[str, Any]]):
        self.recs = [r for r in (records or []) if isinstance(r, dict)]
        self.violations: List[Dict[str, Any]] = []
        self.shadow: Dict[int, int] = {0: 0}
        self.metrics: Dict[str, Any] = {"sm4_ops": 0, "checked": 0,
                                        "by_op": {}, "sm4_active": False}

    def _v(self, seq: Any, detail: str) -> None:
        self.violations.append({"seq": seq, "check": "sm4_result",
                                "severity": "HIGH", "detail": detail})

    def run(self) -> Dict[str, Any]:
        started = _now()
        for i, rec in enumerate(self.recs):
            dis = rec.get("disasm", "")
            mnem = dis.split()[0].lower() if isinstance(dis, str) and dis.split() else ""
            if mnem in ("sm4ed", "sm4ks"):
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
        self.metrics["sm4_ops"] += 1
        self.metrics["sm4_active"] = True
        self.metrics["by_op"][mnem] = self.metrics["by_op"].get(mnem, 0) + 1
        toks = dis.replace(",", " ").split()
        if len(toks) < 5:                            # mnem rd rs1 rs2 bs
            return
        rd_i, rs1_i, rs2_i = _reg_idx(toks[1]), _reg_idx(toks[2]), _reg_idx(toks[3])
        bs = _to_int(toks[4])
        if None in (rd_i, rs1_i, rs2_i, bs):
            return
        golden = sm4_golden(mnem, self.shadow.get(rs1_i, 0),
                            self.shadow.get(rs2_i, 0), bs)
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
        self.metrics["checked"] += 1
        if (rd_val & 0xFFFFFFFF) != golden:
            self._v(seq, f"{mnem} bs={bs}: rd={hex(rd_val & 0xFFFFFFFF)} "
                         f"!= golden {hex(golden)}")

    def _report(self, started: str) -> Dict[str, Any]:
        total = len(self.violations)
        return {
            "schema_version": SCHEMA_VERSION,
            "agent": AGENT_NAME,
            "started_at": started,
            "finished_at": _now(),
            "records_checked": len(self.recs),
            "sm4_active": self.metrics["sm4_active"],
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
        log.warning("sm4_verifier: cannot read manifest: %s", exc)
        return 0
    run_dir = Path(manifest.get("run_dir", mp.parent))
    outputs = manifest.get("outputs", {})
    recs = _load_jsonl(run_dir / outputs.get("rtl_commit_log", "rtl_commit.jsonl"))
    rep = SM4Verifier(recs).run()
    if not rep.get("sm4_active"):
        rep = {"schema_version": SCHEMA_VERSION, "agent": AGENT_NAME,
               "status": "skipped", "reason": "no SM4 instructions", "pass": True}
    else:
        rep["status"] = "completed"
    try:
        (run_dir / "sm4_report.json").write_text(json.dumps(rep, indent=2),
                                                 encoding="utf-8")
    except OSError as exc:
        log.warning("sm4_verifier: cannot write report: %s", exc)
    return 0 if rep.get("pass", True) else 1


if __name__ == "__main__":  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="AVA SM4 scalar-crypto checker")
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args()
    raise SystemExit(run_from_manifest(args.manifest))
