"""
AGENT_H.lsq_verifier — Load/Store Queue Checker (T53)
======================================================

Golden checker for the single-core **load/store queue** — store-to-load
forwarding and memory disambiguation, the subtle intra-core memory-ordering
machinery of an out-of-order pipeline. (Cross-*core* ordering is the coherence /
memory-consistency agents; this one is the LSQ *within* a core.)

Rides the **standard commit log**: a record's `mem_reads` are loads and
`mem_writes` are stores, so it needs no separate trace.

The invariant
-------------
Sequential semantics require that a load observe the value of the **youngest
program-order-older store to the same address** (store-to-load forwarding); if
no earlier store wrote that address, the load reads memory. So:

- **lsq_forward** (HIGH) — when a load's address has a program-order-older store,
  the load's value **must equal that youngest store's value**. A load that reads
  stale memory instead (missing forwarding), forwards from the wrong store, or
  bypasses an older store it should have seen (a memory-ordering / disambiguation
  violation) is caught here.
- **lsq_store_order** (HIGH, when commit cycles are present) — stores to the
  same address drain to memory in **program order** (the store queue commits in
  order): commit cycle increases with program order per address.

**Soundness:** a load whose address has *no* earlier in-trace store is skipped —
its value comes from initial memory the trace doesn't model, so pinning it would
be a false positive. Only forwarding cases with ground truth are asserted.

Trace: the commit log, or a simplified per-op stream
(`{"seq":0,"op":"store","addr":"0x40","value":"0x5"}`).

Stdlib-only, schema-v2.1.0 report, graceful degradation throughout.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

log = logging.getLogger("AGENT_H.lsq")

SCHEMA_VERSION = "2.1.0"
AGENT_NAME = "lsq_verifier"


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


def _mem_ops(rec: Dict[str, Any]) -> List[Tuple[str, Optional[int], Optional[int]]]:
    """Ordered (kind, addr, value) list for a record: loads (mem_reads) before
    stores (mem_writes); or a single ``op``/``addr``/``value`` triple."""
    out: List[Tuple[str, Optional[int], Optional[int]]] = []
    reads = rec.get("mem_reads")
    writes = rec.get("mem_writes")
    if isinstance(reads, list) or isinstance(writes, list):
        for a in (reads or []):
            if isinstance(a, dict):
                out.append(("load", _to_int(a.get("addr")), _to_int(a.get("value"))))
        for a in (writes or []):
            if isinstance(a, dict):
                out.append(("store", _to_int(a.get("addr")), _to_int(a.get("value"))))
        return out
    op = str(rec.get("op", "")).lower()
    if op in ("load", "ld", "read"):
        out.append(("load", _to_int(rec.get("addr")), _to_int(rec.get("value"))))
    elif op in ("store", "st", "write"):
        out.append(("store", _to_int(rec.get("addr")), _to_int(rec.get("value"))))
    return out


def _commit(rec: Dict[str, Any]) -> Optional[int]:
    o = rec.get("ooo", {}) if isinstance(rec.get("ooo"), dict) else {}
    return _to_int(o.get("commit", rec.get("commit")))


class LSQVerifier:
    def __init__(self, records: Sequence[Dict[str, Any]]):
        self.recs = [r for r in (records or []) if isinstance(r, dict)]
        self.violations: List[Dict[str, Any]] = []
        self.metrics = {"loads": 0, "stores": 0, "forwards_checked": 0,
                        "addresses": 0, "lsq_active": False}

    def _v(self, seq: Any, check: str, sev: str, detail: str) -> None:
        self.violations.append({"seq": seq, "check": check,
                                "severity": sev, "detail": detail})

    def run(self) -> Dict[str, Any]:
        started = _now()
        ordered = list(enumerate(self.recs))
        # program order: explicit seq if present, else appearance
        if ordered and all(_to_int(r.get("seq")) is not None for _, r in ordered):
            ordered.sort(key=lambda p: _to_int(p[1].get("seq")))

        last_store: Dict[int, int] = {}                 # addr → youngest store value
        store_hist: Dict[int, List[Tuple[int, int]]] = {}  # addr → [(order, commit)]
        addrs: set = set()
        order = 0

        for _, rec in ordered:
            seq = rec.get("seq", order)
            commit = _commit(rec)
            for kind, addr, value in _mem_ops(rec):
                if addr is None:
                    continue
                addrs.add(addr)
                self.metrics["lsq_active"] = True
                if kind == "load":
                    self.metrics["loads"] += 1
                    if addr in last_store and value is not None:
                        self.metrics["forwards_checked"] += 1
                        golden = last_store[addr]
                        if value != golden:
                            self._v(seq, "lsq_forward", "HIGH",
                                    f"load @ {hex(addr)} = {hex(value)} but youngest "
                                    f"older store wrote {hex(golden)} "
                                    f"(store-to-load forwarding / disambiguation)")
                else:  # store
                    self.metrics["stores"] += 1
                    if value is not None:
                        last_store[addr] = value
                    if commit is not None:
                        store_hist.setdefault(addr, []).append((order, commit))
                order += 1

        self._check_store_order(store_hist)
        self.metrics["addresses"] = len(addrs)
        return self._report(started)

    def _check_store_order(self, store_hist: Dict[int, List[Tuple[int, int]]]) -> None:
        for addr, hist in store_hist.items():
            prev = None
            for _, commit in hist:                      # hist already in program order
                if prev is not None and commit < prev:
                    self._v("-", "lsq_store_order", "HIGH",
                            f"stores to {hex(addr)} drain out of program order "
                            f"(commit {commit} after {prev})")
                prev = commit if prev is None else max(prev, commit)

    def _report(self, started: str) -> Dict[str, Any]:
        total = len(self.violations)
        high = sum(1 for v in self.violations if v["severity"] == "HIGH")
        return {
            "schema_version": SCHEMA_VERSION,
            "agent": AGENT_NAME,
            "started_at": started,
            "finished_at": _now(),
            "records_checked": len(self.recs),
            "lsq_active": self.metrics["lsq_active"],
            "metrics": self.metrics,
            "total_violations": total,
            "high_violations": high,
            "severity_score": high * 3,
            "band": "CLEAN" if total == 0 else "CRITICAL",
            "pass": high == 0,
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
        log.warning("lsq_verifier: cannot read manifest: %s", exc)
        return 0
    run_dir = Path(manifest.get("run_dir", mp.parent))
    outputs = manifest.get("outputs", {})
    name = outputs.get("lsq_trace", outputs.get("rtl_commit_log", "rtl_commit.jsonl"))
    recs = _load_jsonl(run_dir / name)
    rep = LSQVerifier(recs).run()
    if not rep.get("lsq_active"):
        rep = {"schema_version": SCHEMA_VERSION, "agent": AGENT_NAME,
               "status": "skipped", "reason": "no memory operations", "pass": True}
    else:
        rep["status"] = "completed"
    try:
        (run_dir / "lsq_report.json").write_text(json.dumps(rep, indent=2),
                                                 encoding="utf-8")
    except OSError as exc:
        log.warning("lsq_verifier: cannot write report: %s", exc)
    return 0 if rep.get("pass", True) else 1


if __name__ == "__main__":  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="AVA load/store-queue checker")
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args()
    raise SystemExit(run_from_manifest(args.manifest))
