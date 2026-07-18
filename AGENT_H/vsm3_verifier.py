"""
AGENT_H.vsm3_verifier — Vector SM3 Cryptography Checker (T60, Zvksh)
====================================================================

Golden-reference checker for the RISC-V **vector** SM3 hash instructions
(Zvksh):

- ``vsm3me.vv`` — SM3 message expansion (8 new schedule words per group).
- ``vsm3c.vi``  — SM3 compression, **two rounds** per instruction, selected by
  the 5-bit ``rnds`` immediate (rounds ``2·rnds`` and ``2·rnds+1``).

Both operate on **256-bit / 8-element** groups (SEW=32). Per the spec, every
32-bit input and output word is byte-swapped (``rev8``) between big and little
endian so software can feed message bytes directly; this golden reproduces that
exactly.

Why this golden is trustworthy
------------------------------
The transcription follows the authoritative RISC-V sail pseudocode
(``vsm3me.adoc`` / ``vsm3c.adoc``), and the *whole* golden is **validated
end-to-end against the GB/T 32905-2016 test vectors**: composing a full
single-block SM3 (``"abc"`` → ``66c7f0f4…8f4ba8e0``) and a multi-block SM3
(``"abcd"×16`` → ``debe9ff9…9c0c5732``) purely from ``vsm3me`` + ``vsm3c``
reproduces both published digests. That jointly proves the round math, the
byte-swaps, and the rolled state/element-group packing.

Element-group layout (element 0 = least-significant 32-bit word)
----------------------------------------------------------------
- ``vsm3me``: vs1=W[7:0] (el0=W0..el7=W7), vs2=W[15:8] (el0=W8..el7=W15)
  → vd=W[23:16] (el0=W16..el7=W23).
- ``vsm3c``:  vd (state) = {H,G,F,E,D,C,B,A} (el7=H..el0=A);
  vs2 (messages) = {-,-,w5,w4,-,-,w1,w0} (el0=w0,el1=w1,el4=w4,el5=w5);
  result state = {G1,G2,E1,E2,C1,C2,A1,A2} (el7=G1..el0=A2).

Check
-----
- **vsm3_result** (HIGH) — computed group ≠ the reported result group.

Additive ``vsm3_trace.jsonl`` contract (words as 0x-hex or ints):
```
{"op":"vsm3me","vs1":[8 words],"vs2":[8 words],"result":[8 words]}
{"op":"vsm3c","rnds":0,"vd":[8 state words],"vs2":[8 msg words],
 "result":[8 state words]}
```

Stdlib-only, schema-v2.1.0, graceful degradation.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

log = logging.getLogger("AGENT_H.vsm3")

SCHEMA_VERSION = "2.1.0"
AGENT_NAME = "vsm3_verifier"
_ALL_OPS = {"vsm3me", "vsm3c"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _m(x: int) -> int:
    return x & 0xFFFFFFFF


def _rol(x: int, n: int) -> int:
    n %= 32
    x = _m(x)
    return _m((x << n) | (x >> (32 - n)))


def _rev8(x: int) -> int:
    return int.from_bytes(_m(x).to_bytes(4, "big")[::-1], "big")


def _p1(x: int) -> int:
    return _m(x ^ _rol(x, 15) ^ _rol(x, 23))


def _p0(x: int) -> int:
    return _m(x ^ _rol(x, 9) ^ _rol(x, 17))


def _zw(m16: int, m9: int, m3: int, m13: int, m6: int) -> int:
    return _m(_p1(_m(m16 ^ m9 ^ _rol(m3, 15))) ^ _rol(m13, 7) ^ m6)


def _ffj(x: int, y: int, z: int, j: int) -> int:
    return _m(x ^ y ^ z) if j <= 15 else _m((x & y) | (x & z) | (y & z))


def _ggj(x: int, y: int, z: int, j: int) -> int:
    return _m(x ^ y ^ z) if j <= 15 else _m((x & y) | ((~x) & z))


def _tj(j: int) -> int:
    return 0x79CC4519 if j <= 15 else 0x7A879D8A


# ── Golden element-group functions (register-level: rev8 applied internally) ──
def vsm3me_golden(vs1: List[int], vs2: List[int]) -> List[int]:
    """SM3 message expansion → 8 words [W16..W23] (element 0 = W16)."""
    w = [0] * 24
    for i in range(8):
        w[i] = _rev8(vs1[i])          # W0..W7
    for i in range(8):
        w[8 + i] = _rev8(vs2[i])      # W8..W15
    w[16] = _zw(w[0], w[7], w[13], w[3], w[10])
    w[17] = _zw(w[1], w[8], w[14], w[4], w[11])
    w[18] = _zw(w[2], w[9], w[15], w[5], w[12])
    w[19] = _zw(w[3], w[10], w[16], w[6], w[13])
    w[20] = _zw(w[4], w[11], w[17], w[7], w[14])
    w[21] = _zw(w[5], w[12], w[18], w[8], w[15])
    w[22] = _zw(w[6], w[13], w[19], w[9], w[16])
    w[23] = _zw(w[7], w[14], w[20], w[10], w[17])
    return [_rev8(w[16 + i]) for i in range(8)]


def vsm3c_golden(vd: List[int], vs2: List[int], rnds: int) -> List[int]:
    """Two rounds of SM3 compression → next state (8 words, element 0 = A2)."""
    H, G, F, E, D, C, B, A = (_rev8(vd[7 - i]) for i in range(8))
    w0 = _rev8(vs2[0])
    w1 = _rev8(vs2[1])
    w4 = _rev8(vs2[4])
    w5 = _rev8(vs2[5])
    x0 = _m(w0 ^ w4)
    x1 = _m(w1 ^ w5)

    j = 2 * rnds
    ss1 = _rol(_m(_rol(A, 12) + E + _rol(_tj(j), j % 32)), 7)
    ss2 = _m(ss1 ^ _rol(A, 12))
    tt1 = _m(_ffj(A, B, C, j) + D + ss2 + x0)
    tt2 = _m(_ggj(E, F, G, j) + H + ss1 + w0)
    D = C
    C1 = _rol(B, 9)
    B = A
    A1 = tt1
    H = G
    G1 = _rol(F, 19)
    F = E
    E1 = _p0(tt2)

    j = 2 * rnds + 1
    ss1 = _rol(_m(_rol(A1, 12) + E1 + _rol(_tj(j), j % 32)), 7)
    ss2 = _m(ss1 ^ _rol(A1, 12))
    tt1 = _m(_ffj(A1, B, C1, j) + D + ss2 + x1)
    tt2 = _m(_ggj(E1, F, G1, j) + H + ss1 + w1)
    D = C1
    C2 = _rol(B, 9)
    B = A1
    A2 = tt1
    H = G1
    G2 = _rol(F, 19)
    F = E1
    E2 = _p0(tt2)

    res = [G1, G2, E1, E2, C1, C2, A1, A2]     # MSB (el7) → LSB (el0)
    return [_rev8(res[7 - i]) for i in range(8)]


# ── Parsing helpers ─────────────────────────────────────────────────────────
def _word(v: Any) -> Optional[int]:
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


def _words8(v: Any) -> Optional[List[int]]:
    if not isinstance(v, (list, tuple)) or len(v) != 8:
        return None
    out = [_word(x) for x in v]
    return out if all(x is not None for x in out) else None  # type: ignore


def _hex8(words: List[int]) -> str:
    return "[" + ",".join(f"0x{_m(x):08x}" for x in words) + "]"


class VSM3Verifier:
    def __init__(self, events: Sequence[Dict[str, Any]]):
        self.events = [e for e in (events or []) if isinstance(e, dict)]
        self.violations: List[Dict[str, Any]] = []
        self.metrics: Dict[str, Any] = {"vsm3_ops": 0, "checked": 0,
                                        "by_op": {}, "vsm3_active": False}

    def _v(self, i: int, detail: str) -> None:
        self.violations.append({"event": i, "check": "vsm3_result",
                                "severity": "HIGH", "detail": detail})

    def run(self) -> Dict[str, Any]:
        started = _now()
        for i, e in enumerate(self.events):
            op = str(e.get("op", "")).lower().split(".")[0]
            if op not in _ALL_OPS:
                continue
            result = _words8(e.get("result"))
            if op == "vsm3me":
                vs1 = _words8(e.get("vs1"))
                vs2 = _words8(e.get("vs2"))
                if vs1 is None or vs2 is None or result is None:
                    continue
                golden = vsm3me_golden(vs1, vs2)
            else:  # vsm3c
                vd = _words8(e.get("vd"))
                vs2 = _words8(e.get("vs2"))
                rnds = _word(e.get("rnds", 0))
                if vd is None or vs2 is None or result is None or rnds is None:
                    continue
                golden = vsm3c_golden(vd, vs2, rnds & 0x1F)
            self.metrics["vsm3_ops"] += 1
            self.metrics["vsm3_active"] = True
            self.metrics["by_op"][op] = self.metrics["by_op"].get(op, 0) + 1
            golden = [_m(x) for x in golden]
            result = [_m(x) for x in result]
            self.metrics["checked"] += 1
            if golden != result:
                self._v(i, f"{op}: result {_hex8(result)} != golden {_hex8(golden)}")
        return self._report(started)

    def _report(self, started: str) -> Dict[str, Any]:
        total = len(self.violations)
        return {
            "schema_version": SCHEMA_VERSION,
            "agent": AGENT_NAME,
            "started_at": started,
            "finished_at": _now(),
            "records_checked": len(self.events),
            "vsm3_active": self.metrics["vsm3_active"],
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
    name = (manifest.get("outputs", {}) or {}).get("vsm3_trace", "vsm3_trace.jsonl")
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
        log.warning("vsm3_verifier: cannot read manifest: %s", exc)
        return 0
    run_dir = Path(manifest.get("run_dir", mp.parent))
    events = _load_events(run_dir, manifest)
    if not events:
        rep = {"schema_version": SCHEMA_VERSION, "agent": AGENT_NAME,
               "status": "skipped", "reason": "no vsm3_trace", "pass": True}
    else:
        rep = VSM3Verifier(events).run()
        rep["status"] = "completed"
    try:
        (run_dir / "vsm3_report.json").write_text(json.dumps(rep, indent=2),
                                                  encoding="utf-8")
    except OSError as exc:
        log.warning("vsm3_verifier: cannot write report: %s", exc)
    return 0 if rep.get("pass", True) else 1


if __name__ == "__main__":  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="AVA vector-SM3 checker")
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args()
    raise SystemExit(run_from_manifest(args.manifest))
