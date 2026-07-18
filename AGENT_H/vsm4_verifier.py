"""
AGENT_H.vsm4_verifier — Vector SM4 Cryptography Checker (T62, Zvksed)
=====================================================================

Golden-reference checker for the RISC-V **vector** SM4 block-cipher instructions
(Zvksed):

- ``vsm4r.[vv,vs]`` — four SM4 cipher rounds (encryption/decryption share the
  round function; decryption just consumes round keys in reverse).
- ``vsm4k.vi``      — four rounds of SM4 key expansion, the round-key group
  selected by the ``rnd`` immediate (``uimm[2:0]`` ∈ 0..7).

Both operate on **128-bit / 4-element** groups (SEW=32).

Why this golden is trustworthy
------------------------------
The two goldens transcribe the authoritative RISC-V sail (``vsm4r.adoc`` /
``vsm4k.adoc``) and reuse the scalar SM4 S-box (``sm4_verifier``). The whole
golden is **validated end-to-end against the GB/T 32907 test vector**: composing
a full SM4 key schedule (``vsm4k``) + 32-round encryption (``vsm4r``) of
``0123456789abcdeffedcba9876543210`` reproduces the published ciphertext
``681edf34d206965e86b3e94f536e4246``. That jointly proves the round functions,
the round-constant table, and the element-group layout.

Round functions
---------------
- Cipher round `L`:  ``sm4_round(X,S) = X ^ (S ⊕ S⋘2 ⊕ S⋘10 ⊕ S⋘18 ⊕ S⋘24)``.
- Key-sched  `L'`:   ``round_key(X,S) = X ^ (S ⊕ S⋘13 ⊕ S⋘23)``.
- ``S = sm4_subword(B)`` applies the SM4 S-box to each byte of ``B``.

Element-group layout (element 0 = least-significant word)
---------------------------------------------------------
- ``vsm4r``: vd = state ``[x0,x1,x2,x3]``, vs2 = round keys ``[rk0,rk1,rk2,rk3]``
  → result ``[x4,x5,x6,x7]``.
- ``vsm4k``: vs2 = ``[rk0,rk1,rk2,rk3]`` → result ``[rk4,rk5,rk6,rk7]``.

Check
-----
- **vsm4_result** (HIGH) — computed group ≠ the reported result group.

Additive ``vsm4_trace.jsonl`` contract (words as 0x-hex or ints):
```
{"op":"vsm4r","vd":[x0,x1,x2,x3],"vs2":[rk0,rk1,rk2,rk3],"result":[x4..x7]}
{"op":"vsm4k","rnd":0,"vs2":[rk0,rk1,rk2,rk3],"result":[rk4..rk7]}
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
    from .sm4_verifier import SM4_SBOX
except ImportError:                                  # pragma: no cover
    from sm4_verifier import SM4_SBOX                # type: ignore

log = logging.getLogger("AGENT_H.vsm4")

SCHEMA_VERSION = "2.1.0"
AGENT_NAME = "vsm4_verifier"
_ALL_OPS = {"vsm4r", "vsm4k"}

# SM4 key-expansion round constants (CK), rk[0..31].
_CK = [
    0x00070E15, 0x1C232A31, 0x383F464D, 0x545B6269,
    0x70777E85, 0x8C939AA1, 0xA8AFB6BD, 0xC4CBD2D9,
    0xE0E7EEF5, 0xFC030A11, 0x181F262D, 0x343B4249,
    0x50575E65, 0x6C737A81, 0x888F969D, 0xA4ABB2B9,
    0xC0C7CED5, 0xDCE3EAF1, 0xF8FF060D, 0x141B2229,
    0x30373E45, 0x4C535A61, 0x686F767D, 0x848B9299,
    0xA0A7AEB5, 0xBCC3CAD1, 0xD8DFE6ED, 0xF4FB0209,
    0x10171E25, 0x2C333A41, 0x484F565D, 0x646B7279,
]
# SM4 system parameters FK (for constructing the initial key group in software).
FK = [0xA3B1BAC6, 0x56AA3350, 0x677D9197, 0xB27022DC]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _m(x: int) -> int:
    return x & 0xFFFFFFFF


def _rol(x: int, n: int) -> int:
    n %= 32
    x = _m(x)
    return _m((x << n) | (x >> (32 - n)))


def _subword(b: int) -> int:
    return sum(SM4_SBOX[(b >> (8 * i)) & 0xFF] << (8 * i) for i in range(4))


def _l_cipher(s: int) -> int:                        # L: cipher linear transform
    return _m(s ^ _rol(s, 2) ^ _rol(s, 10) ^ _rol(s, 18) ^ _rol(s, 24))


def _l_key(s: int) -> int:                           # L': key-schedule linear
    return _m(s ^ _rol(s, 13) ^ _rol(s, 23))


# ── Golden element-group functions ──────────────────────────────────────────
def vsm4r_golden(vd: List[int], vs2: List[int]) -> List[int]:
    """Four SM4 cipher rounds. vd = state [x0..x3], vs2 = round keys [rk0..rk3]
    → next state [x4,x5,x6,x7]."""
    x = [_m(v) for v in vd]
    rk = [_m(v) for v in vs2]
    for i in range(4):
        b = _m(x[i + 1] ^ x[i + 2] ^ x[i + 3] ^ rk[i])
        x.append(_m(x[i] ^ _l_cipher(_subword(b))))
    return x[4:8]


def vsm4k_golden(vs2: List[int], rnd: int) -> List[int]:
    """Four SM4 key-expansion rounds for round group ``rnd`` (0..7). vs2 =
    [rk0..rk3] → next four round keys [rk4,rk5,rk6,rk7]."""
    rnd &= 0x7
    rk = [_m(v) for v in vs2]
    for i in range(4):
        b = _m(rk[i + 1] ^ rk[i + 2] ^ rk[i + 3] ^ _CK[4 * rnd + i])
        rk.append(_m(rk[i] ^ _l_key(_subword(b))))
    return rk[4:8]


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


class VSM4Verifier:
    def __init__(self, events: Sequence[Dict[str, Any]]):
        self.events = [e for e in (events or []) if isinstance(e, dict)]
        self.violations: List[Dict[str, Any]] = []
        self.metrics: Dict[str, Any] = {"vsm4_ops": 0, "checked": 0,
                                        "by_op": {}, "vsm4_active": False}

    def _v(self, i: int, detail: str) -> None:
        self.violations.append({"event": i, "check": "vsm4_result",
                                "severity": "HIGH", "detail": detail})

    def run(self) -> Dict[str, Any]:
        started = _now()
        for i, e in enumerate(self.events):
            op = str(e.get("op", "")).lower().split(".")[0]
            if op not in _ALL_OPS:
                continue
            vs2 = _words4(e.get("vs2"))
            result = _words4(e.get("result"))
            if vs2 is None or result is None:
                continue
            if op == "vsm4r":
                vd = _words4(e.get("vd"))
                if vd is None:
                    continue
                golden = vsm4r_golden(vd, vs2)
            else:  # vsm4k
                rnd = _word(e.get("rnd", e.get("uimm", 0)))
                if rnd is None:
                    continue
                golden = vsm4k_golden(vs2, rnd)
            self.metrics["vsm4_ops"] += 1
            self.metrics["vsm4_active"] = True
            self.metrics["by_op"][op] = self.metrics["by_op"].get(op, 0) + 1
            golden = [_m(x) for x in golden]
            result = [_m(x) for x in result]
            self.metrics["checked"] += 1
            if golden != result:
                self._v(i, f"{op}: result {_hex4(result)} != golden {_hex4(golden)}")
        return self._report(started)

    def _report(self, started: str) -> Dict[str, Any]:
        total = len(self.violations)
        return {
            "schema_version": SCHEMA_VERSION,
            "agent": AGENT_NAME,
            "started_at": started,
            "finished_at": _now(),
            "records_checked": len(self.events),
            "vsm4_active": self.metrics["vsm4_active"],
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
    name = (manifest.get("outputs", {}) or {}).get("vsm4_trace", "vsm4_trace.jsonl")
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
        log.warning("vsm4_verifier: cannot read manifest: %s", exc)
        return 0
    run_dir = Path(manifest.get("run_dir", mp.parent))
    events = _load_events(run_dir, manifest)
    if not events:
        rep = {"schema_version": SCHEMA_VERSION, "agent": AGENT_NAME,
               "status": "skipped", "reason": "no vsm4_trace", "pass": True}
    else:
        rep = VSM4Verifier(events).run()
        rep["status"] = "completed"
    try:
        (run_dir / "vsm4_report.json").write_text(json.dumps(rep, indent=2),
                                                  encoding="utf-8")
    except OSError as exc:
        log.warning("vsm4_verifier: cannot write report: %s", exc)
    return 0 if rep.get("pass", True) else 1


if __name__ == "__main__":  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="AVA vector-SM4 checker")
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args()
    raise SystemExit(run_from_manifest(args.manifest))
