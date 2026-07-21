"""
AGENT_H.vghash_verifier — Vector GHASH Checker (T63, Zvkg)
==========================================================

Golden-reference checker for the RISC-V **vector GHASH** instructions (Zvkg),
the GF(2¹²⁸) primitives behind AES-GCM authentication:

- ``vgmul.vv``  — GHASH multiply: ``vd ← vd ⊗ vs2`` over GF(2¹²⁸).
- ``vghsh.vv``  — GHASH add-multiply: ``vd ← (vd ⊕ vs1) ⊗ vs2`` (the partial
  hash XOR the block-cipher output, times the hash subkey ``H``).

Both operate on **128-bit / 4-element** groups (SEW=32). ``vgmul`` is exactly
``vghsh`` with ``vs1 = 0`` — a property the tests assert.

Field arithmetic
----------------
Multiplication is a carry-less multiply of two 128-bit polynomials modulo
GHASH's irreducible polynomial ``x¹²⁸ + x⁷ + x² + x + 1``. NIST orders
coefficients left-to-right (``x₀x₁…x₁₂₇``), so the hardware ``brev8``-reverses
the bits **within each byte** to reach the standard polynomial basis, multiplies
by repeated shift-and-reduce (``H <<= 1``; if the old MSB was set, ``H ^= 0x87``),
then ``brev8``-reverses the result back.

Why this golden is trustworthy
------------------------------
The golden transcribes the authoritative RISC-V sail (``vghsh``/``vgmul``) and is
validated two independent ways:

1. **Against an independent NIST SP 800-38D GF(2¹²⁸) multiply** (the textbook
   right-shift/``0xE1`` formulation) — agreement on random operand pairs.
2. **Against the NIST GCM test vectors** — running the GHASH recurrence with
   ``vghsh`` reproduces the published digest
   (Test Case 2: ``H=66e94bd4…``, ``C=0388dace…`` → ``f38cbb1a…b6b0f885``).

Byte-order note: a 128-bit element group holds the block with byte 0 at the
**least-significant** end, so bridging NIST big-endian hex to a register value
needs a 16-byte swap (``brev8`` only fixes the within-byte order). The tests do
this explicitly.

Check
-----
- **vghash_result** (HIGH) — computed product ≠ the reported result group.

Additive ``vghash_trace.jsonl`` contract (128-bit values as 32-hex strings, or
4-word element lists, element 0 = least-significant word):
```
{"op":"vgmul","vd":"…","vs2":"…","result":"…"}
{"op":"vghsh","vd":"…","vs2":"…","vs1":"…","result":"…"}
```

Stdlib-only, schema-v2.1.0, graceful degradation.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

log = logging.getLogger("AGENT_H.vghash")

SCHEMA_VERSION = "2.1.0"
AGENT_NAME = "vghash_verifier"
_ALL_OPS = {"vgmul", "vghsh"}
_M128 = (1 << 128) - 1
_REDUCE = 0x87                       # low terms of x¹²⁸ + x⁷ + x² + x + 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def brev8(x: int) -> int:
    """Reverse the bit order within each of the 16 bytes."""
    x &= _M128
    out = 0
    for i in range(16):
        b = (x >> (8 * i)) & 0xFF
        r = 0
        for k in range(8):
            if b & (1 << k):
                r |= 1 << (7 - k)
        out |= r << (8 * i)
    return out


def _mul_reduce(sel: int, h: int) -> int:
    """Shift-and-add GF(2¹²⁸) multiply in the standard polynomial basis."""
    z = 0
    for bit in range(128):
        if (sel >> bit) & 1:
            z ^= h
        top = (h >> 127) & 1
        h = (h << 1) & _M128
        if top:
            h ^= _REDUCE
    return z


# ── Golden element-group functions ──────────────────────────────────────────
def vgmul_golden(vd: int, vs2: int) -> int:
    """GHASH multiply: vd ⊗ vs2 over GF(2¹²⁸)."""
    return brev8(_mul_reduce(brev8(vd & _M128), brev8(vs2 & _M128)))


def vghsh_golden(vd: int, vs2: int, vs1: int) -> int:
    """GHASH add-multiply: (vd ⊕ vs1) ⊗ vs2 over GF(2¹²⁸)."""
    return brev8(_mul_reduce(brev8((vd ^ vs1) & _M128), brev8(vs2 & _M128)))


# ── Parsing helpers ─────────────────────────────────────────────────────────
def _val128(v: Any) -> Optional[int]:
    """Parse a 128-bit group: 32-hex string, int, or 4-word element list
    (element 0 = least-significant 32-bit word)."""
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v & _M128
    if isinstance(v, (list, tuple)) and len(v) == 4:
        out = 0
        for i, w in enumerate(v):
            iw = _val128(w)
            if iw is None:
                return None
            out |= (iw & 0xFFFFFFFF) << (32 * i)
        return out
    if isinstance(v, str):
        s = v.lower()
        s = s[2:] if s.startswith("0x") else s
        try:
            return int(s, 16) & _M128
        except ValueError:
            return None
    return None


class VGHASHVerifier:
    def __init__(self, events: Sequence[Dict[str, Any]]):
        self.events = [e for e in (events or []) if isinstance(e, dict)]
        self.violations: List[Dict[str, Any]] = []
        self.metrics: Dict[str, Any] = {"vghash_ops": 0, "checked": 0,
                                        "by_op": {}, "vghash_active": False}

    def _v(self, i: int, detail: str) -> None:
        self.violations.append({"event": i, "check": "vghash_result",
                                "severity": "HIGH", "detail": detail})

    def run(self) -> Dict[str, Any]:
        started = _now()
        for i, e in enumerate(self.events):
            op = str(e.get("op", "")).lower().split(".")[0]
            if op not in _ALL_OPS:
                continue
            vd = _val128(e.get("vd"))
            vs2 = _val128(e.get("vs2"))
            result = _val128(e.get("result"))
            if vd is None or vs2 is None or result is None:
                continue
            if op == "vghsh":
                vs1 = _val128(e.get("vs1"))
                if vs1 is None:
                    continue
                golden = vghsh_golden(vd, vs2, vs1)
            else:
                golden = vgmul_golden(vd, vs2)
            self.metrics["vghash_ops"] += 1
            self.metrics["vghash_active"] = True
            self.metrics["by_op"][op] = self.metrics["by_op"].get(op, 0) + 1
            self.metrics["checked"] += 1
            if golden != result:
                self._v(i, f"{op}: result 0x{result:032x} != golden 0x{golden:032x}")
        return self._report(started)

    def _report(self, started: str) -> Dict[str, Any]:
        total = len(self.violations)
        return {
            "schema_version": SCHEMA_VERSION,
            "agent": AGENT_NAME,
            "started_at": started,
            "finished_at": _now(),
            "records_checked": len(self.events),
            "vghash_active": self.metrics["vghash_active"],
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
def _load_events(run_dir: Path, manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    name = (manifest.get("outputs", {}) or {}).get("vghash_trace",
                                                    "vghash_trace.jsonl")
    p = run_dir / name
    if not p.exists():
        return []
    text = p.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if text.lstrip().startswith("["):
        try:
            data = json.loads(text)
            return data if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def run_from_manifest(manifest_path: str) -> int:
    mp = Path(manifest_path)
    try:
        manifest = json.loads(mp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("vghash_verifier: cannot read manifest: %s", exc)
        return 0
    run_dir = Path(manifest.get("run_dir", mp.parent))
    events = _load_events(run_dir, manifest)
    if not events:
        rep = {"schema_version": SCHEMA_VERSION, "agent": AGENT_NAME,
               "status": "skipped", "reason": "no vghash_trace", "pass": True}
    else:
        rep = VGHASHVerifier(events).run()
        rep["status"] = "completed"
    try:
        (run_dir / "vghash_report.json").write_text(json.dumps(rep, indent=2),
                                                    encoding="utf-8")
    except OSError as exc:
        log.warning("vghash_verifier: cannot write report: %s", exc)
    return 0 if rep.get("pass", True) else 1


if __name__ == "__main__":  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="AVA vector-GHASH checker")
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args()
    raise SystemExit(run_from_manifest(args.manifest))
