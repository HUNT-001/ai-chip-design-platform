"""
AGENT_H.cas_verifier — Zacas Compare-and-Swap Checker (T55)
============================================================

Golden checker for the RISC-V **Zacas** atomic compare-and-swap instructions
(`amocas.w` / `.d` / `.q`). CAS is the primitive lock-free and concurrent code
is built on, so a subtly-wrong CAS silently corrupts every lock, queue and
reference count that uses it — while each individual access still "looks" fine.

Semantics (exact)
-----------------
`amocas` atomically:
1. reads the current memory value `mem_old`,
2. **compares** it to the expected value in `rd`,
3. if equal (**success**), writes the swap value (`rs2`) to memory; if not
   (**failure**), leaves memory unchanged,
4. writes `mem_old` back into `rd` in *both* cases.

Checks
------
- **cas_return** (HIGH) — `rd` after the op must equal the old memory value.
- **cas_success** (HIGH) — when `mem_old == compare`, memory must become `swap`.
- **cas_fail** (HIGH) — when `mem_old != compare`, memory must be **unchanged**.

Additive `cas_trace.jsonl` contract
-----------------------------------
```
{"op":"amocas.w", "addr":"0x40", "compare":"0x5", "swap":"0x9",
 "mem_old":"0x5", "mem_new":"0x9", "rd":"0x5"}
```
`op` width (`.w`/`.d`/`.q`) masks the compared values. Clean no-op on an absent
trace.

Stdlib-only, schema-v2.1.0 report, graceful degradation throughout.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

log = logging.getLogger("AGENT_H.cas")

SCHEMA_VERSION = "2.1.0"
AGENT_NAME = "cas_verifier"
_WIDTH = {"w": 32, "d": 64, "q": 128}


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


def _width_of(op: str) -> int:
    op = str(op).lower()
    if "." in op:
        return _WIDTH.get(op.rsplit(".", 1)[1], 64)
    return 64


class CASVerifier:
    def __init__(self, events: Sequence[Dict[str, Any]]):
        self.events = [e for e in (events or []) if isinstance(e, dict)]
        self.violations: List[Dict[str, Any]] = []
        self.metrics = {"cas_ops": 0, "successes": 0, "failures": 0,
                        "cas_active": False}

    def _v(self, i: int, check: str, detail: str) -> None:
        self.violations.append({"event": i, "check": check,
                                "severity": "HIGH", "detail": detail})

    def run(self) -> Dict[str, Any]:
        started = _now()
        for i, e in enumerate(self.events):
            op = str(e.get("op", "")).lower()
            if not op.startswith("amocas") and op != "cas":
                continue
            self._check(i, e, op)
        return self._report(started)

    def _check(self, i: int, e: Dict[str, Any], op: str) -> None:
        addr = _to_int(e.get("addr"))
        compare = _to_int(e.get("compare"))
        swap = _to_int(e.get("swap"))
        mem_old = _to_int(e.get("mem_old"))
        mem_new = _to_int(e.get("mem_new"))
        rd = _to_int(e.get("rd"))
        if None in (compare, swap, mem_old, mem_new):
            return
        self.metrics["cas_ops"] += 1
        self.metrics["cas_active"] = True
        mask = (1 << _width_of(op)) - 1
        c, s, mo, mn = compare & mask, swap & mask, mem_old & mask, mem_new & mask
        loc = hex(addr) if addr is not None else "?"

        # return value: rd == old memory
        if rd is not None and (rd & mask) != mo:
            self._v(i, "cas_return",
                    f"amocas @ {loc}: rd={hex(rd & mask)} != old memory {hex(mo)}")

        # success / failure semantics
        if mo == c:
            self.metrics["successes"] += 1
            if mn != s:
                self._v(i, "cas_success",
                        f"amocas @ {loc}: compare matched ({hex(c)}) but memory "
                        f"became {hex(mn)}, not swap {hex(s)}")
        else:
            self.metrics["failures"] += 1
            if mn != mo:
                self._v(i, "cas_fail",
                        f"amocas @ {loc}: compare failed ({hex(mo)}≠{hex(c)}) but "
                        f"memory changed to {hex(mn)} (must be unchanged)")

    def _report(self, started: str) -> Dict[str, Any]:
        total = len(self.violations)
        return {
            "schema_version": SCHEMA_VERSION,
            "agent": AGENT_NAME,
            "started_at": started,
            "finished_at": _now(),
            "records_checked": len(self.events),
            "cas_active": self.metrics["cas_active"],
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
    name = (manifest.get("outputs", {}) or {}).get("cas_trace", "cas_trace.jsonl")
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
        log.warning("cas_verifier: cannot read manifest: %s", exc)
        return 0
    run_dir = Path(manifest.get("run_dir", mp.parent))
    events = _load_events(run_dir, manifest)
    if not events:
        rep = {"schema_version": SCHEMA_VERSION, "agent": AGENT_NAME,
               "status": "skipped", "reason": "no cas_trace", "pass": True}
    else:
        rep = CASVerifier(events).run()
        rep["status"] = "completed"
    try:
        (run_dir / "cas_report.json").write_text(json.dumps(rep, indent=2),
                                                 encoding="utf-8")
    except OSError as exc:
        log.warning("cas_verifier: cannot write report: %s", exc)
    return 0 if rep.get("pass", True) else 1


if __name__ == "__main__":  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="AVA Zacas compare-and-swap checker")
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args()
    raise SystemExit(run_from_manifest(args.manifest))
