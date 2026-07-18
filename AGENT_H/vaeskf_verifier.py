"""
AGENT_H.vaeskf_verifier — Vector AES Key-Schedule Checker (T61, Zvkned)
=======================================================================

Golden-reference checker for the RISC-V **vector** AES forward key-schedule
instructions (Zvkned):

- ``vaeskf1.vi`` — one round of the **AES-128** forward key schedule.
- ``vaeskf2.vi`` — one round of the **AES-256** forward key schedule.

Both operate on **128-bit / 4-element** groups (SEW=32): a round key is four
32-bit words (element 3 = most-significant word). Each instruction derives the
next round key from the current key group (and, for ``vaeskf2``, the previous
round key held in ``vd``), using ``SubWord``, ``RotWord`` and a round constant
selected by the round-number immediate.

Why this golden is trustworthy
------------------------------
The two goldens transcribe the authoritative RISC-V sail (``vaeskf1.adoc`` /
``vaeskf2.adoc``), including the out-of-range immediate projection
(``uimm[3]`` inversion), and are **validated against FIPS-197**: iterating
``vaeskf1`` over rounds 1-10 reproduces the full AES-128 expanded key
(``a0fafe17…b6630ca6``), and iterating ``vaeskf2`` reproduces a full AES-256
(Nk=8) expansion. The scalar sibling ``aes64ks1i`` lives in ``aes_verifier`` and
is validated the same way.

Element / word convention
-------------------------
The trace carries each 32-bit key word in the sail byte order (``RotWord`` =
``ror32(·,8)``, round constant in the low byte). ``vs2``/``vd``/``result`` are
4-word groups, element 0 = least-significant word, element 3 = most-significant.

Check
-----
- **vaeskf_result** (HIGH) — computed next round key ≠ the reported result.

Additive ``vaeskf_trace.jsonl`` contract (words as 0x-hex or ints):
```
{"op":"vaeskf1","rnd":1,"vs2":[k0,k1,k2,k3],"result":[w0,w1,w2,w3]}
{"op":"vaeskf2","rnd":2,"vs2":[cur0..3],"vd":[prev0..3],"result":[w0..w3]}
```

Stdlib-only, schema-v2.1.0, graceful degradation.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

try:                                                 # package or standalone import
    from .aes_verifier import AES_SBOX
except ImportError:                                  # pragma: no cover
    from aes_verifier import AES_SBOX                # type: ignore

log = logging.getLogger("AGENT_H.vaeskf")

SCHEMA_VERSION = "2.1.0"
AGENT_NAME = "vaeskf_verifier"
_ALL_OPS = {"vaeskf1", "vaeskf2"}
_RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1b, 0x36]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _m(x: int) -> int:
    return x & 0xFFFFFFFF


def _ror32(x: int, n: int) -> int:
    n %= 32
    x = _m(x)
    return _m((x >> n) | (x << (32 - n)))


def _subword(x: int) -> int:
    return sum(AES_SBOX[(x >> (8 * i)) & 0xFF] << (8 * i) for i in range(4))


def _rotword(x: int) -> int:                         # sail aes_rotword
    return _ror32(x, 8)


# ── Golden element-group functions ──────────────────────────────────────────
def vaeskf1_golden(vs2: List[int], rnd: int) -> List[int]:
    """One AES-128 key-schedule round → next 4-word round key."""
    rnd &= 0xF
    if rnd > 10 or rnd == 0:                          # project out-of-range imm
        rnd ^= 0x8
    r = rnd - 1
    k = [_m(x) for x in vs2]
    w0 = _m(_subword(_rotword(k[3])) ^ _RCON[r] ^ k[0])
    w1 = _m(w0 ^ k[1])
    w2 = _m(w1 ^ k[2])
    w3 = _m(w2 ^ k[3])
    return [w0, w1, w2, w3]


def vaeskf2_golden(vs2: List[int], vd: List[int], rnd: int) -> List[int]:
    """One AES-256 key-schedule round → next 4-word round key. ``vs2`` = current
    round key group, ``vd`` = previous round key group (RoundKeyB)."""
    rnd &= 0xF
    if rnd < 2 or rnd > 14:                           # project out-of-range imm
        rnd ^= 0x8
    k = [_m(x) for x in vs2]
    b = [_m(x) for x in vd]
    if rnd & 1:
        w0 = _m(_subword(k[3]) ^ b[0])
    else:
        w0 = _m(_subword(_rotword(k[3])) ^ _RCON[(rnd >> 1) - 1] ^ b[0])
    w1 = _m(w0 ^ b[1])
    w2 = _m(w1 ^ b[2])
    w3 = _m(w2 ^ b[3])
    return [w0, w1, w2, w3]


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


def _hex4(words: List[int]) -> str:
    return "[" + ",".join(f"0x{_m(x):08x}" for x in words) + "]"


class VAESKFVerifier:
    def __init__(self, events: Sequence[Dict[str, Any]]):
        self.events = [e for e in (events or []) if isinstance(e, dict)]
        self.violations: List[Dict[str, Any]] = []
        self.metrics: Dict[str, Any] = {"vaeskf_ops": 0, "checked": 0,
                                        "by_op": {}, "vaeskf_active": False}

    def _v(self, i: int, detail: str) -> None:
        self.violations.append({"event": i, "check": "vaeskf_result",
                                "severity": "HIGH", "detail": detail})

    def run(self) -> Dict[str, Any]:
        started = _now()
        for i, e in enumerate(self.events):
            op = str(e.get("op", "")).lower().split(".")[0]
            if op not in _ALL_OPS:
                continue
            rnd = _word(e.get("rnd", e.get("uimm", 0)))
            vs2 = _words4(e.get("vs2"))
            result = _words4(e.get("result"))
            if rnd is None or vs2 is None or result is None:
                continue
            if op == "vaeskf1":
                golden = vaeskf1_golden(vs2, rnd)
            else:
                vd = _words4(e.get("vd"))
                if vd is None:
                    continue
                golden = vaeskf2_golden(vs2, vd, rnd)
            self.metrics["vaeskf_ops"] += 1
            self.metrics["vaeskf_active"] = True
            self.metrics["by_op"][op] = self.metrics["by_op"].get(op, 0) + 1
            golden = [_m(x) for x in golden]
            result = [_m(x) for x in result]
            self.metrics["checked"] += 1
            if golden != result:
                self._v(i, f"{op} (rnd {rnd & 0xF}): result {_hex4(result)} != "
                           f"golden {_hex4(golden)}")
        return self._report(started)

    def _report(self, started: str) -> Dict[str, Any]:
        total = len(self.violations)
        return {
            "schema_version": SCHEMA_VERSION,
            "agent": AGENT_NAME,
            "started_at": started,
            "finished_at": _now(),
            "records_checked": len(self.events),
            "vaeskf_active": self.metrics["vaeskf_active"],
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
    name = (manifest.get("outputs", {}) or {}).get("vaeskf_trace",
                                                    "vaeskf_trace.jsonl")
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
        log.warning("vaeskf_verifier: cannot read manifest: %s", exc)
        return 0
    run_dir = Path(manifest.get("run_dir", mp.parent))
    events = _load_events(run_dir, manifest)
    if not events:
        rep = {"schema_version": SCHEMA_VERSION, "agent": AGENT_NAME,
               "status": "skipped", "reason": "no vaeskf_trace", "pass": True}
    else:
        rep = VAESKFVerifier(events).run()
        rep["status"] = "completed"
    try:
        (run_dir / "vaeskf_report.json").write_text(json.dumps(rep, indent=2),
                                                    encoding="utf-8")
    except OSError as exc:
        log.warning("vaeskf_verifier: cannot write report: %s", exc)
    return 0 if rep.get("pass", True) else 1


if __name__ == "__main__":  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="AVA vector-AES key-schedule checker")
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args()
    raise SystemExit(run_from_manifest(args.manifest))
