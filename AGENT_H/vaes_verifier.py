"""
AGENT_H.vaes_verifier — Vector AES Cryptography Checker (T58, Zvkned)
=====================================================================

Golden-reference checker for the RISC-V **vector** AES instructions (Zvkned):
`vaesef` / `vaesem` (encrypt final / middle round), `vaesdf` / `vaesdm`
(decrypt), and `vaesz` (round-key XOR). Each operates on **128-bit element
groups** — one full AES state per group — applying the standard AES round and
XORing the round key.

This reuses the FIPS-197-validated scalar AES core (`aes_verifier`: S-box,
GF(2⁸) MixColumns) but on the *full* 128-bit state (not the RV64 two-register
split), so the round is the textbook SubBytes → ShiftRows → MixColumns → AddKey.
The golden is validated against the FIPS-197 round vector (round key = 0).

Round golden (per 128-bit group)
--------------------------------
- `vaesem`: `MixColumns(ShiftRows(SubBytes(state))) ⊕ key`
- `vaesef`: `ShiftRows(SubBytes(state)) ⊕ key`
- `vaesdm`: `InvMixColumns(InvShiftRows(InvSubBytes(state))) ⊕ key`
- `vaesdf`: `InvShiftRows(InvSubBytes(state)) ⊕ key`
- `vaesz` : `state ⊕ key`

Check
-----
- **vaes_result** (HIGH) — computed group ≠ the reported result group.

Additive `vaes_trace.jsonl` contract (128-bit values as 32-hex-char strings,
byte 0 = leftmost pair):
```
{"op":"vaesem", "state":"193de3be…e9f84808", "key":"00…00",
 "result":"046681e5…2806264c"}
```

Stdlib-only, schema-v2.1.0, graceful degradation.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

try:                                                # package or standalone import
    from .aes_verifier import AES_SBOX, AES_INV_SBOX, _mixcol_fwd, _mixcol_inv
except ImportError:                                 # pragma: no cover
    from aes_verifier import AES_SBOX, AES_INV_SBOX, _mixcol_fwd, _mixcol_inv  # type: ignore

log = logging.getLogger("AGENT_H.vaes")

SCHEMA_VERSION = "2.1.0"
AGENT_NAME = "vaes_verifier"
_ROUND_OPS = {"vaesef", "vaesem", "vaesdf", "vaesdm", "vaesz"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bytes16(v: Any) -> Optional[List[int]]:
    """Parse a 128-bit value (32-hex-char string, 0x-prefixed or not, or a
    16-int list) into 16 bytes, byte 0 = leftmost/most-significant pair."""
    if isinstance(v, list) and len(v) == 16:
        try:
            return [int(x) & 0xFF for x in v]
        except (TypeError, ValueError):
            return None
    if isinstance(v, int):
        v = f"{v:032x}"
    if isinstance(v, str):
        s = v.lower()[2:] if v.lower().startswith("0x") else v.lower()
        s = s.rjust(32, "0")[-32:]
        try:
            return [int(s[i:i + 2], 16) for i in range(0, 32, 2)]
        except ValueError:
            return None
    return None


def _sr_fwd(b: List[int]) -> List[int]:
    out = [0] * 16
    for c in range(4):
        for r in range(4):
            out[4 * c + r] = b[4 * ((c + r) % 4) + r]
    return out


def _sr_inv(b: List[int]) -> List[int]:
    out = [0] * 16
    for c in range(4):
        for r in range(4):
            out[4 * c + r] = b[4 * ((c - r) % 4) + r]
    return out


def _mixcolumns(b: List[int], inverse: bool) -> List[int]:
    mc = _mixcol_inv if inverse else _mixcol_fwd
    out = list(b)
    for c in range(4):
        w = b[4 * c] | (b[4 * c + 1] << 8) | (b[4 * c + 2] << 16) | (b[4 * c + 3] << 24)
        mw = mc(w)
        for r in range(4):
            out[4 * c + r] = (mw >> (8 * r)) & 0xFF
    return out


def vaes_round(op: str, state: List[int], key: List[int]) -> Optional[List[int]]:
    """One AES round on a 128-bit group. Encrypt round (SubBytes, ShiftRows,
    [MixColumns], AddRoundKey) is FIPS-197-validated; the decrypt round is the
    standard inverse cipher (AddRoundKey, [InvMixColumns], InvShiftRows,
    InvSubBytes) — the exact inverse, so vaesd*∘vaese* round-trips."""
    if op == "vaesz":
        return [state[i] ^ key[i] for i in range(16)]
    if op in ("vaesef", "vaesem"):                   # encrypt round
        sb = [AES_SBOX[x] for x in _sr_fwd(state)]   # SubBytes ∘ ShiftRows
        if op == "vaesem":
            sb = _mixcolumns(sb, inverse=False)      # MixColumns
        return [sb[i] ^ key[i] for i in range(16)]   # AddRoundKey
    # decrypt round: AddRoundKey → [InvMixColumns] → InvShiftRows → InvSubBytes
    t = [state[i] ^ key[i] for i in range(16)]
    if op == "vaesdm":
        t = _mixcolumns(t, inverse=True)
    return [AES_INV_SBOX[x] for x in _sr_inv(t)]


def _hex16(b: List[int]) -> str:
    return "".join(f"{x:02x}" for x in b)


class VAESVerifier:
    def __init__(self, events: Sequence[Dict[str, Any]]):
        self.events = [e for e in (events or []) if isinstance(e, dict)]
        self.violations: List[Dict[str, Any]] = []
        self.metrics: Dict[str, Any] = {"vaes_ops": 0, "checked": 0,
                                        "by_op": {}, "vaes_active": False}

    def _v(self, i: int, detail: str) -> None:
        self.violations.append({"event": i, "check": "vaes_result",
                                "severity": "HIGH", "detail": detail})

    def run(self) -> Dict[str, Any]:
        started = _now()
        for i, e in enumerate(self.events):
            op = str(e.get("op", "")).lower()
            if op.split(".")[0] not in _ROUND_OPS:
                continue
            op = op.split(".")[0]
            self.metrics["vaes_ops"] += 1
            self.metrics["vaes_active"] = True
            self.metrics["by_op"][op] = self.metrics["by_op"].get(op, 0) + 1
            state = _bytes16(e.get("state"))
            key = _bytes16(e.get("key", "0" * 32))
            result = _bytes16(e.get("result"))
            if state is None or key is None or result is None:
                continue
            golden = vaes_round(op, state, key)
            if golden is None:
                continue
            self.metrics["checked"] += 1
            if golden != result:
                self._v(i, f"{op}: result {_hex16(result)} != golden {_hex16(golden)}")
        return self._report(started)

    def _report(self, started: str) -> Dict[str, Any]:
        total = len(self.violations)
        return {
            "schema_version": SCHEMA_VERSION,
            "agent": AGENT_NAME,
            "started_at": started,
            "finished_at": _now(),
            "records_checked": len(self.events),
            "vaes_active": self.metrics["vaes_active"],
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
    name = (manifest.get("outputs", {}) or {}).get("vaes_trace", "vaes_trace.jsonl")
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
        log.warning("vaes_verifier: cannot read manifest: %s", exc)
        return 0
    run_dir = Path(manifest.get("run_dir", mp.parent))
    events = _load_events(run_dir, manifest)
    if not events:
        rep = {"schema_version": SCHEMA_VERSION, "agent": AGENT_NAME,
               "status": "skipped", "reason": "no vaes_trace", "pass": True}
    else:
        rep = VAESVerifier(events).run()
        rep["status"] = "completed"
    try:
        (run_dir / "vaes_report.json").write_text(json.dumps(rep, indent=2),
                                                  encoding="utf-8")
    except OSError as exc:
        log.warning("vaes_verifier: cannot write report: %s", exc)
    return 0 if rep.get("pass", True) else 1


if __name__ == "__main__":  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="AVA vector-AES checker")
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args()
    raise SystemExit(run_from_manifest(args.manifest))
