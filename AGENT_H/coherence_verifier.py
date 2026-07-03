"""
AGENT_H.coherence_verifier — Multicore Cache-Coherence Checker (T44)
=====================================================================

Golden-reference checker for the fundamental **cache-coherence** invariants of a
multicore system, driven from a multicore memory-access trace. This is a
distinct verification *level*: a single-core tandem-diff can't see coherence
bugs (missing invalidations, stale reads, two simultaneous writers) because they
only manifest across cores.

Coherence vs. consistency
-------------------------
This agent checks **coherence** (the per-location guarantees), which every sane
system must provide regardless of its memory-consistency model:

1. **read-from-a-real-write** — a load's value was actually written by *some*
   store to that address (or is the initial value); values are never fabricated.
2. **write serialization** — all writes to a *single* address occur in one total
   order, and **every core observes them in that order**. Formally: per core, the
   sequence of writes it reads-from must be non-decreasing in the global
   per-address write order. A core that sees a newer write and then an older one
   has observed a coherence violation (a stale read / missing invalidation).
3. **SWMR** (Single-Writer / Multiple-Reader) — at any instant an address is
   either held writable by exactly one core, or read-only by any number of
   cores; a writer never coexists with another writer or reader. Checked
   structurally when the trace exposes per-line MESI state.

The write order is taken from the trace's global time order (``cycle`` stamps,
else list order) — i.e. the order in which writes become globally visible, which
*is* the coherence order for a commit/retire trace. This assumption is stated so
a relaxed-visibility trace can be fed correctly (stamp by visibility, not issue).

Additive trace contract (a separate multicore event stream)
-----------------------------------------------------------
```
each event:
  {"core": 0, "op": "load"|"store", "addr": "0x40", "value": "0x7",
   "cycle": 12,              # optional global-visibility order
   "state": "M"|"E"|"S"|"I", # optional per-line MESI state after the op
   "ver": 3}                 # optional explicit write-id (disambiguates equal values)
```

Stdlib-only, schema-v2.1.0 report, graceful degradation throughout.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

log = logging.getLogger("AGENT_H.coherence")

SCHEMA_VERSION = "2.1.0"
AGENT_NAME = "coherence_verifier"
_EXCLUSIVE = {"M", "E"}
_VALID = {"M", "E", "S"}


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


def _norm_op(op: Any) -> Optional[str]:
    s = str(op).lower()
    if s in ("load", "ld", "read", "r"):
        return "load"
    if s in ("store", "st", "write", "w"):
        return "store"
    return None


class CoherenceVerifier:
    def __init__(self, events: Sequence[Dict[str, Any]]):
        self.events = [e for e in (events or []) if isinstance(e, dict)]
        self.violations: List[Dict[str, Any]] = []
        self.metrics = {
            "events": len(self.events), "cores": 0, "addresses": 0,
            "loads": 0, "stores": 0, "swmr_checks": 0,
        }

    def _v(self, idx: int, check: str, sev: str, detail: str, **extra) -> None:
        self.violations.append({"event": idx, "check": check, "severity": sev,
                                "detail": detail, **extra})

    def run(self) -> Dict[str, Any]:
        started = _now()
        # Order by global visibility (cycle) if provided, else trace order.
        indexed = list(enumerate(self.events))
        if all(_to_int(e.get("cycle")) is not None for _, e in indexed) and indexed:
            indexed.sort(key=lambda p: (_to_int(p[1].get("cycle")), p[0]))

        # per-address global write order
        writes: Dict[int, List[Dict[str, Any]]] = {}     # addr -> [{ver,value,core}]
        val_vers: Dict[int, Dict[int, List[int]]] = {}   # addr -> value -> [ver]
        last_seen: Dict[tuple, int] = {}                 # (core,addr) -> ver
        state: Dict[tuple, str] = {}                     # (core,addr) -> MESI
        cores: set = set()
        addrs: set = set()

        for idx, ev in indexed:
            op = _norm_op(ev.get("op"))
            addr = _to_int(ev.get("addr"))
            core = ev.get("core")
            if op is None or addr is None or core is None:
                continue
            cores.add(core)
            addrs.add(addr)
            writes.setdefault(addr, [])
            val_vers.setdefault(addr, {})

            if op == "store":
                self.metrics["stores"] += 1
                ver = _to_int(ev.get("ver"))
                if ver is None:
                    ver = len(writes[addr])
                value = _to_int(ev.get("value"))
                writes[addr].append({"ver": ver, "value": value, "core": core})
                if value is not None:
                    val_vers[addr].setdefault(value, []).append(ver)
                last_seen[(core, addr)] = max(last_seen.get((core, addr), -1), ver)
            else:  # load
                self.metrics["loads"] += 1
                self._check_load(idx, ev, addr, core, writes, val_vers, last_seen)

            # SWMR structural check (only if states are provided)
            st = ev.get("state")
            if isinstance(st, str) and st.upper() in ("M", "E", "S", "I"):
                state[(core, addr)] = st.upper()
                self._check_swmr(idx, addr, state, cores)

        self.metrics["cores"] = len(cores)
        self.metrics["addresses"] = len(addrs)
        return self._report(started)

    def _check_load(self, idx: int, ev: Dict[str, Any], addr: int, core: Any,
                    writes, val_vers, last_seen) -> None:
        value = _to_int(ev.get("value"))
        ev_ver = _to_int(ev.get("ver"))
        matched: Optional[int] = None

        if ev_ver is not None:
            if any(w["ver"] == ev_ver for w in writes[addr]):
                matched = ev_ver
        elif value is not None and value in val_vers[addr]:
            matched = val_vers[addr][value][-1]           # most-recent write of this value
        elif not writes[addr] and (value in (0, None)):
            matched = -1                                   # initial value, never written
        # else: unmatched → fabricated

        if matched is None:
            if not writes[addr] and value not in (0, None):
                self._v(idx, "read_from_valid", "HIGH",
                        f"core {core} load @ {hex(addr)} = {hex(value)} but address never written")
            elif value is not None:
                self._v(idx, "read_from_valid", "HIGH",
                        f"core {core} load @ {hex(addr)} = {hex(value)} matches no store (fabricated)")
            return

        prev = last_seen.get((core, addr), -1)
        if matched < prev:
            self._v(idx, "coherence_read_monotonic", "HIGH",
                    f"core {core} @ {hex(addr)} read write#{matched} after already "
                    f"observing write#{prev} — stale read / lost invalidation")
        last_seen[(core, addr)] = max(prev, matched)

    def _check_swmr(self, idx: int, addr: int, state: Dict[tuple, str],
                    cores: set) -> None:
        self.metrics["swmr_checks"] += 1
        holders = [(c, state.get((c, addr), "I")) for c in cores]
        exclusive = [c for c, s in holders if s in _EXCLUSIVE]
        valid = [c for c, s in holders if s in _VALID]
        if len(exclusive) > 1:
            self._v(idx, "swmr", "HIGH",
                    f"{len(exclusive)} cores hold {hex(addr)} writable (M/E) simultaneously: {exclusive}")
        elif exclusive and len(valid) > 1:
            others = [c for c in valid if c not in exclusive]
            self._v(idx, "swmr", "HIGH",
                    f"core {exclusive[0]} holds {hex(addr)} writable while cores {others} also hold it")

    def _report(self, started: str) -> Dict[str, Any]:
        total = len(self.violations)
        high = sum(1 for v in self.violations if v["severity"] == "HIGH")
        med = sum(1 for v in self.violations if v["severity"] == "MEDIUM")
        band = ("CLEAN" if total == 0 else "CRITICAL" if high else
                "DEGRADED" if med else "MINOR")
        return {
            "schema_version": SCHEMA_VERSION,
            "agent": AGENT_NAME,
            "started_at": started,
            "finished_at": _now(),
            "records_checked": len(self.events),
            "coherence_active": self.metrics["cores"] > 1,
            "metrics": self.metrics,
            "total_violations": total,
            "high_violations": high,
            "medium_violations": med,
            "severity_score": high * 3 + med,
            "band": band,
            "pass": high == 0,
            "violations": self.violations[:100],
        }


# ─────────────────────────────────────────────────────────────────────────────
# Manifest entry point
# ─────────────────────────────────────────────────────────────────────────────
def _load_events(run_dir: Path, manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    name = (manifest.get("outputs", {}) or {}).get("coherence_trace",
                                                    "coherence_trace.jsonl")
    p = run_dir / name
    if not p.exists():
        return []
    text = p.read_text(encoding="utf-8").strip()
    if not text:
        return []
    # accept a JSON array or JSONL
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
    """Load the multicore coherence trace, run the checker, write
    ``coherence_report.json``. Returns 0 on pass/skip, 1 on HIGH violations."""
    mp = Path(manifest_path)
    try:
        manifest = json.loads(mp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("coherence_verifier: cannot read manifest: %s", exc)
        return 0
    run_dir = Path(manifest.get("run_dir", mp.parent))
    events = _load_events(run_dir, manifest)
    if not events:
        rep = {"schema_version": SCHEMA_VERSION, "agent": AGENT_NAME,
               "status": "skipped", "reason": "no coherence_trace", "pass": True}
    else:
        rep = CoherenceVerifier(events).run()
        rep["status"] = "completed"
    try:
        (run_dir / "coherence_report.json").write_text(
            json.dumps(rep, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("coherence_verifier: cannot write report: %s", exc)
    return 0 if rep.get("pass", True) else 1


if __name__ == "__main__":  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="AVA multicore cache-coherence checker")
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args()
    raise SystemExit(run_from_manifest(args.manifest))
