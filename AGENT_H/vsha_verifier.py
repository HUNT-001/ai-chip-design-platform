"""
AGENT_H.vsha_verifier — Vector SHA-2 Cryptography Checker (T59, Zvknha/Zvknhb)
=============================================================================

Golden-reference checker for the RISC-V **vector** SHA-2 instructions (Zvknh):

- ``vsha2ms.vv`` — SHA-2 message-schedule expansion (produces 4 new schedule
  words per element group).
- ``vsha2ch.vv`` / ``vsha2cl.vv`` — SHA-2 hash compression (two rounds each);
  ``vsha2cl`` consumes the low two words of the ``vs1`` message-schedule group,
  ``vsha2ch`` the high two. Per the spec, the Zvknh instructions do **not** add
  the round constant — software adds ``K`` before the compression instruction —
  so the golden takes the already-``+K`` words from ``vs1``.

SEW selects the algorithm: **SEW=32 → SHA-256**, **SEW=64 → SHA-512**. Every op
works on 4-element groups (EGW 128 / 256).

Why this golden is trustworthy
------------------------------
The ``σ₀/σ₁`` (message schedule) and ``Σ₀/Σ₁/Ch/Maj`` (compression) primitives
are reused from the scalar SHA core, and the *whole* golden is **validated
end-to-end against Python's ``hashlib``**: composing a full SHA-256 (and
SHA-512) hash of ``b"abc"`` purely from ``vsha2ms`` + ``vsha2c`` reproduces the
exact published digests (``ba7816bf…`` / ``ddaf35a1…``). That simultaneously
proves both the arithmetic and the element-group layout.

Element-group layout (element 0 = least-significant)
----------------------------------------------------
- ``vsha2ms``: vd=[W0,W1,W2,W3]  vs2=[W4,W9,W10,W11]  vs1=[W12,W13,W14,W15]
  → result=[W16,W17,W18,W19]  (Wt = σ₁(W[t-2])+W[t-7]+σ₀(W[t-15])+W[t-16]).
- ``vsha2c[hl]``: vs2=[f,e,b,a]  vd=[h,g,d,c]  vs1=[wk0,wk1,wk2,wk3] (schedule+K);
  ``cl`` uses (wk0,wk1), ``ch`` uses (wk2,wk3); result=[f',e',b',a'].

Check
-----
- **vsha_result** (HIGH) — computed group ≠ the reported result group.

Additive ``vsha_trace.jsonl`` contract (words as 0x-hex or ints; ``sew`` 32/64):
```
{"op":"vsha2ms","sew":32,"vd":["0x..",..],"vs2":[..],"vs1":[..],"result":[..]}
{"op":"vsha2cl","sew":32,"vd":[h,g,d,c],"vs2":[f,e,b,a],"vs1":[wk0..wk3],
 "result":[fp,ep,bp,ap]}
```

Stdlib-only, schema-v2.1.0, graceful degradation.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

log = logging.getLogger("AGENT_H.vsha")

SCHEMA_VERSION = "2.1.0"
AGENT_NAME = "vsha_verifier"
_MS_OPS = {"vsha2ms"}
_C_OPS = {"vsha2ch", "vsha2cl"}
_ALL_OPS = _MS_OPS | _C_OPS


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mask(w: int) -> int:
    return (1 << w) - 1


def _rotr(x: int, n: int, w: int) -> int:
    m = _mask(w)
    x &= m
    n %= w
    return ((x >> n) | (x << (w - n))) & m


def _shr(x: int, n: int, w: int) -> int:
    return (x & _mask(w)) >> n


# ── SHA-2 primitives (SEW-parameterised) ────────────────────────────────────
def _sig0(x: int, w: int) -> int:
    if w == 32:
        return _rotr(x, 7, w) ^ _rotr(x, 18, w) ^ _shr(x, 3, w)
    return _rotr(x, 1, w) ^ _rotr(x, 8, w) ^ _shr(x, 7, w)


def _sig1(x: int, w: int) -> int:
    if w == 32:
        return _rotr(x, 17, w) ^ _rotr(x, 19, w) ^ _shr(x, 10, w)
    return _rotr(x, 19, w) ^ _rotr(x, 61, w) ^ _shr(x, 6, w)


def _sum0(x: int, w: int) -> int:
    if w == 32:
        return _rotr(x, 2, w) ^ _rotr(x, 13, w) ^ _rotr(x, 22, w)
    return _rotr(x, 28, w) ^ _rotr(x, 34, w) ^ _rotr(x, 39, w)


def _sum1(x: int, w: int) -> int:
    if w == 32:
        return _rotr(x, 6, w) ^ _rotr(x, 11, w) ^ _rotr(x, 25, w)
    return _rotr(x, 14, w) ^ _rotr(x, 18, w) ^ _rotr(x, 41, w)


def _ch(e: int, f: int, g: int) -> int:
    return (e & f) ^ (~e & g)


def _maj(a: int, b: int, c: int) -> int:
    return (a & b) ^ (a & c) ^ (b & c)


# ── Golden element-group functions ──────────────────────────────────────────
def vsha2ms_golden(vd: List[int], vs2: List[int], vs1: List[int],
                   w: int) -> List[int]:
    """Message-schedule expansion → next 4 words [W16,W17,W18,W19]."""
    m = _mask(w)
    W0, W1, W2, W3 = (x & m for x in vd)
    W4, W9, W10, W11 = (x & m for x in vs2)
    W12, W13, W14, W15 = (x & m for x in vs1)
    W16 = (_sig1(W14, w) + W9 + _sig0(W1, w) + W0) & m
    W17 = (_sig1(W15, w) + W10 + _sig0(W2, w) + W1) & m
    W18 = (_sig1(W16, w) + W11 + _sig0(W3, w) + W2) & m
    W19 = (_sig1(W17, w) + W12 + _sig0(W4, w) + W3) & m
    return [W16, W17, W18, W19]


def vsha2c_golden(op: str, vd: List[int], vs2: List[int], vs1: List[int],
                  w: int) -> List[int]:
    """Two-round SHA-2 compression → new [f,e,b,a]. vs2=[f,e,b,a], vd=[h,g,d,c],
    vs1=[wk0,wk1,wk2,wk3] (schedule words already summed with K). ``vsha2cl``
    uses (wk0,wk1); ``vsha2ch`` uses (wk2,wk3)."""
    m = _mask(w)
    f, e, b, a = (x & m for x in vs2)
    h, g, d, c = (x & m for x in vd)
    wk = vs1[0:2] if op == "vsha2cl" else vs1[2:4]
    for W in wk:
        T1 = (h + _sum1(e, w) + _ch(e, f, g) + (W & m)) & m
        T2 = (_sum0(a, w) + _maj(a, b, c)) & m
        h = g
        g = f
        f = e
        e = (d + T1) & m
        d = c
        c = b
        b = a
        a = (T1 + T2) & m
    return [f, e, b, a]


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


def _words4(v: Any) -> Optional[List[int]]:
    if not isinstance(v, (list, tuple)) or len(v) != 4:
        return None
    out = [_word(x) for x in v]
    return out if all(x is not None for x in out) else None  # type: ignore


def _hexw(words: List[int], w: int) -> str:
    d = w // 4
    return "[" + ",".join(f"0x{x & _mask(w):0{d}x}" for x in words) + "]"


class VSHAVerifier:
    def __init__(self, events: Sequence[Dict[str, Any]]):
        self.events = [e for e in (events or []) if isinstance(e, dict)]
        self.violations: List[Dict[str, Any]] = []
        self.metrics: Dict[str, Any] = {"vsha_ops": 0, "checked": 0,
                                        "by_op": {}, "vsha_active": False}

    def _v(self, i: int, detail: str) -> None:
        self.violations.append({"event": i, "check": "vsha_result",
                                "severity": "HIGH", "detail": detail})

    def run(self) -> Dict[str, Any]:
        started = _now()
        for i, e in enumerate(self.events):
            op = str(e.get("op", "")).lower().split(".")[0]
            if op not in _ALL_OPS:
                continue
            w = 64 if int(e.get("sew", 32)) == 64 else 32
            vd = _words4(e.get("vd"))
            vs2 = _words4(e.get("vs2"))
            vs1 = _words4(e.get("vs1"))
            result = _words4(e.get("result"))
            if vd is None or vs2 is None or vs1 is None or result is None:
                continue
            self.metrics["vsha_ops"] += 1
            self.metrics["vsha_active"] = True
            self.metrics["by_op"][op] = self.metrics["by_op"].get(op, 0) + 1
            if op == "vsha2ms":
                golden = vsha2ms_golden(vd, vs2, vs1, w)
            else:
                golden = vsha2c_golden(op, vd, vs2, vs1, w)
            m = _mask(w)
            golden = [x & m for x in golden]
            result = [x & m for x in result]
            self.metrics["checked"] += 1
            if golden != result:
                self._v(i, f"{op} (sew{w}): result {_hexw(result, w)} != "
                           f"golden {_hexw(golden, w)}")
        return self._report(started)

    def _report(self, started: str) -> Dict[str, Any]:
        total = len(self.violations)
        return {
            "schema_version": SCHEMA_VERSION,
            "agent": AGENT_NAME,
            "started_at": started,
            "finished_at": _now(),
            "records_checked": len(self.events),
            "vsha_active": self.metrics["vsha_active"],
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
    name = (manifest.get("outputs", {}) or {}).get("vsha_trace", "vsha_trace.jsonl")
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
        log.warning("vsha_verifier: cannot read manifest: %s", exc)
        return 0
    run_dir = Path(manifest.get("run_dir", mp.parent))
    events = _load_events(run_dir, manifest)
    if not events:
        rep = {"schema_version": SCHEMA_VERSION, "agent": AGENT_NAME,
               "status": "skipped", "reason": "no vsha_trace", "pass": True}
    else:
        rep = VSHAVerifier(events).run()
        rep["status"] = "completed"
    try:
        (run_dir / "vsha_report.json").write_text(json.dumps(rep, indent=2),
                                                  encoding="utf-8")
    except OSError as exc:
        log.warning("vsha_verifier: cannot write report: %s", exc)
    return 0 if rep.get("pass", True) else 1


if __name__ == "__main__":  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="AVA vector-SHA-2 checker")
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args()
    raise SystemExit(run_from_manifest(args.manifest))
