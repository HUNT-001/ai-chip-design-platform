"""
AGENT_H/cache_verifier.py
=========================
T33 — Cache Subsystem Verification

Verifies cache behaviour from the canonical commit log against a **golden
set-associative cache model**.  Caches are where many verification efforts stop,
yet replacement-policy and dirty-eviction bugs silently corrupt data.  The novel
core here is a precise, configurable software cache (`CacheModel`) that replays
the memory-access stream and computes, for every access, the expected
hit/miss, the replacement victim, and whether a dirty write-back occurs — which
the checker then compares against what the DUT reported.

Checks
------
  cache_hitmiss     DUT-reported hit/miss != golden model
  cache_eviction    wrong line evicted (replacement-policy violation)
  cache_writeback   dirty eviction without a write-back (or a spurious one)
  cache_data        a hit returned data inconsistent with the last write
                    (cache-line corruption / stale data)

Metrics (analytics — never fail the run)
----------------------------------------
  accesses, hits, misses, hit_rate, evictions, writebacks

Conservative gating (no false positives)
----------------------------------------
  * runs only when a cache configuration is available (manifest ``cache_config``
    or constructor) **and** the replacement policy is deterministic (LRU/FIFO);
  * a correctness check fires only for the fields the DUT actually reports
    (``hit`` / ``evict_addr`` / ``writeback`` / ``value``); everything else is
    metrics-only.

Optional trace contract (additive only)
---------------------------------------
  manifest["cache_config"] = {sets, ways, line_size, policy, write_policy}
  mem_reads/mem_writes entry:
      {"addr": "0x..", "value": "0x..",
       "cache": {"hit": bool, "evict_addr": "0x..", "writeback": bool}}
  (the cache fields may also be inline on the entry: "hit"/"evict_addr"/"writeback")

Usage
-----
  from AGENT_H.cache_verifier import CacheModel, CacheVerifier
  report = CacheVerifier(rtl_log, config={"sets":64,"ways":4,"line_size":64}).run()

  from AGENT_H.cache_verifier import run_from_manifest
  run_from_manifest(Path(run_dir) / "run_manifest.json")
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "2.1.0"
_M32 = 0xFFFFFFFF

_DETERMINISTIC_POLICIES = {"lru", "fifo"}


def _to_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        v = value.strip()
        if not v:
            return None
        try:
            return int(v, 16) if v.lower().startswith("0x") else int(v, 0)
        except ValueError:
            try:
                return int(v)
            except ValueError:
                return None
    return None


def _log2(n: int) -> Optional[int]:
    if n <= 0 or (n & (n - 1)) != 0:
        return None
    return n.bit_length() - 1


# ─────────────────────────────────────────────────────────
# Golden cache model
# ─────────────────────────────────────────────────────────

@dataclass
class CacheResult:
    hit:         bool
    index:       int
    way:         int
    victim_addr: Optional[int] = None
    writeback:   bool = False


@dataclass
class _Way:
    valid: bool = False
    tag:   int = 0
    dirty: bool = False
    ts:    int = 0          # LRU timestamp / FIFO insertion order


class CacheModel:
    """
    Golden set-associative cache.

    Parameters
    ----------
    sets, ways    : geometry (both >= 1)
    line_size     : bytes per line (power of two)
    policy        : "lru" | "fifo"
    write_policy  : "wb" (write-back) | "wt" (write-through)
    """

    def __init__(self, sets: int, ways: int, line_size: int,
                 policy: str = "lru", write_policy: str = "wb") -> None:
        self.num_sets   = max(1, int(sets))
        self.num_ways   = max(1, int(ways))
        self.line_bits  = _log2(int(line_size))
        if self.line_bits is None:
            raise ValueError("line_size must be a power of two")
        self.policy        = policy.lower()
        self.write_policy  = write_policy.lower()
        self.time          = 0
        self.sets: List[List[_Way]] = [
            [_Way() for _ in range(self.num_ways)] for _ in range(self.num_sets)
        ]

    def _decompose(self, addr: int) -> Tuple[int, int]:
        blk = (addr & _M32) >> self.line_bits
        return blk % self.num_sets, blk // self.num_sets    # (index, tag)

    def _victim_addr(self, tag: int, index: int) -> int:
        blk = tag * self.num_sets + index
        return (blk << self.line_bits) & _M32

    def access(self, addr: int, write: bool) -> CacheResult:
        index, tag = self._decompose(addr)
        ways = self.sets[index]
        self.time += 1

        # hit?
        for wi, w in enumerate(ways):
            if w.valid and w.tag == tag:
                if self.policy == "lru":
                    w.ts = self.time
                wb = False
                if write:
                    if self.write_policy == "wb":
                        w.dirty = True
                    else:                       # write-through: bus write now
                        wb = True
                return CacheResult(True, index, wi, None, wb)

        # miss — choose a victim
        victim = next((i for i, w in enumerate(ways) if not w.valid), None)
        if victim is None:
            victim = min(range(len(ways)), key=lambda i: ways[i].ts)
        vw = ways[victim]

        writeback = False
        victim_addr = None
        if vw.valid:                            # a real line is being evicted
            victim_addr = self._victim_addr(vw.tag, index)
            if vw.dirty:                        # dirty eviction -> write-back
                writeback = True

        vw.valid = True
        vw.tag   = tag
        vw.ts    = self.time
        vw.dirty = bool(write and self.write_policy == "wb")
        if write and self.write_policy == "wt":
            writeback = True                    # allocate + write-through

        return CacheResult(False, index, victim, victim_addr, writeback)


# ─────────────────────────────────────────────────────────
# Violation
# ─────────────────────────────────────────────────────────

@dataclass
class CacheViolation:
    check:       str
    severity:    str
    seq:         int
    pc:          Optional[str]
    disasm:      Optional[str]
    description: str
    expected:    Optional[str] = None
    actual:      Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "check": self.check, "severity": self.severity, "seq": self.seq,
            "pc": self.pc, "disasm": self.disasm, "description": self.description,
            "expected": self.expected, "actual": self.actual,
        }


# ─────────────────────────────────────────────────────────
# Verifier
# ─────────────────────────────────────────────────────────

class CacheVerifier:
    """
    Verify cache hit/miss, eviction, write-back and line integrity.

    Parameters
    ----------
    rtl_log        : list of RTL commit records
    iss_log        : optional ISS commit records (reserved)
    config         : {sets, ways, line_size, policy, write_policy} or None
    max_violations : stop collecting after this many violations
    """

    _SEV_WEIGHT = {"HIGH": 1.0, "MEDIUM": 0.4, "LOW": 0.15}

    def __init__(
        self,
        rtl_log:        List[Dict[str, Any]],
        iss_log:        Optional[List[Dict[str, Any]]] = None,
        config:         Optional[Dict[str, Any]] = None,
        max_violations: int = 200,
    ) -> None:
        self.rtl_log        = rtl_log or []
        self.iss_log        = iss_log or []
        self.config         = config or {}
        self.max_violations = max_violations
        self._violations: List[CacheViolation] = []
        self._mem: Dict[int, int] = {}          # golden word memory (for cache_data)
        self._stats = {"accesses": 0, "hits": 0, "misses": 0,
                       "evictions": 0, "writebacks": 0, "checked": 0}
        self._model: Optional[CacheModel] = self._build_model()

    def _build_model(self) -> Optional[CacheModel]:
        c = self.config
        policy = str(c.get("policy", "lru")).lower()
        if not c or policy not in _DETERMINISTIC_POLICIES:
            return None
        sets = _to_int(c.get("sets"))
        ways = _to_int(c.get("ways"))
        line = _to_int(c.get("line_size"))
        if not sets or not ways or not line:
            return None
        try:
            return CacheModel(sets, ways, line, policy,
                              str(c.get("write_policy", "wb")).lower())
        except ValueError:
            return None

    def _flag(self, v: CacheViolation) -> None:
        if len(self._violations) < self.max_violations:
            self._violations.append(v)

    @staticmethod
    def _cache_event(entry: Dict) -> Optional[Dict[str, Any]]:
        if not isinstance(entry, dict):
            return None
        ev = entry.get("cache")
        if isinstance(ev, dict):
            return ev
        # inline form
        if any(k in entry for k in ("hit", "evict_addr", "writeback")):
            return {k: entry.get(k) for k in ("hit", "evict_addr", "writeback")}
        return None

    def _accesses(self, rec: Dict) -> List[Tuple[str, Dict]]:
        out = []
        for r in (rec.get("mem_reads") or []):
            if isinstance(r, dict):
                out.append(("load", r))
        for w in (rec.get("mem_writes") or []):
            if isinstance(w, dict):
                out.append(("store", w))
        return out

    def _check_record(self, rec: Dict, seq: int) -> None:
        for kind, entry in self._accesses(rec):
            addr = _to_int(entry.get("addr"))
            if addr is None:
                continue
            write = kind == "store"
            res = self._model.access(addr, write)
            self._stats["accesses"] += 1
            self._stats["hits" if res.hit else "misses"] += 1
            if res.victim_addr is not None:
                self._stats["evictions"] += 1
            if res.writeback:
                self._stats["writebacks"] += 1

            ev = self._cache_event(entry)
            if ev is not None:
                self._compare(rec, seq, kind, addr, res, ev)

            # golden data integrity
            val = _to_int(entry.get("value"))
            if write and val is not None:
                self._mem[addr] = val
            elif (not write) and val is not None and addr in self._mem:
                if res.hit and self._mem[addr] != val:
                    self._flag(CacheViolation(
                        "cache_data", "HIGH", seq, rec.get("pc"), rec.get("disasm"),
                        f"read hit at 0x{addr:08x} returned 0x{val:08x} but last write "
                        f"was 0x{self._mem[addr]:08x} (stale / corrupted line)",
                        expected=f"0x{self._mem[addr]:08x}", actual=f"0x{val:08x}"))

    def _compare(self, rec, seq, kind, addr, res: CacheResult, ev: Dict) -> None:
        self._stats["checked"] += 1
        rep_hit = ev.get("hit")
        if isinstance(rep_hit, bool) and rep_hit != res.hit:
            self._flag(CacheViolation(
                "cache_hitmiss", "HIGH", seq, rec.get("pc"), rec.get("disasm"),
                f"{kind} 0x{addr:08x} reported {'HIT' if rep_hit else 'MISS'} but golden "
                f"cache says {'HIT' if res.hit else 'MISS'}",
                expected="hit" if res.hit else "miss",
                actual="hit" if rep_hit else "miss"))

        rep_evict = _to_int(ev.get("evict_addr"))
        if rep_evict is not None and res.victim_addr is not None and \
                rep_evict != res.victim_addr:
            self._flag(CacheViolation(
                "cache_eviction", "HIGH", seq, rec.get("pc"), rec.get("disasm"),
                f"{kind} 0x{addr:08x} evicted 0x{rep_evict:08x} but golden policy "
                f"({self._model.policy.upper()}) evicts 0x{res.victim_addr:08x}",
                expected=f"0x{res.victim_addr:08x}", actual=f"0x{rep_evict:08x}"))

        rep_wb = ev.get("writeback")
        if isinstance(rep_wb, bool) and rep_wb != res.writeback:
            self._flag(CacheViolation(
                "cache_writeback", "HIGH", seq, rec.get("pc"), rec.get("disasm"),
                f"{kind} 0x{addr:08x} write-back reported {rep_wb} but golden model "
                f"requires {res.writeback} (dirty-eviction data loss risk)",
                expected=str(res.writeback), actual=str(rep_wb)))

    # -- main loop ------------------------------------------------------------

    def run(self) -> Dict[str, Any]:
        started = datetime.now(timezone.utc)
        n = len(self.rtl_log)
        if self._model is not None:
            for i, rec in enumerate(self.rtl_log):
                if len(self._violations) >= self.max_violations:
                    break
                if not isinstance(rec, dict):
                    continue
                seq = rec.get("seq", i)
                try:
                    self._check_record(rec, seq)
                except Exception as exc:           # never crash the pipeline
                    logger.warning("cache_verifier: record %d raised: %s", seq, exc)
        finished = datetime.now(timezone.utc)
        return self._report(n, started, finished)

    # -- reporting ------------------------------------------------------------

    def _band(self) -> Tuple[float, str]:
        if not self._violations:
            return 0.0, "CLEAN"
        score = sum(self._SEV_WEIGHT.get(v.severity, 0.2) for v in self._violations)
        norm  = min(1.0, score / max(1, self._stats["checked"] + 1))
        if any(v.severity == "HIGH" for v in self._violations):
            band = "CRITICAL"
        elif norm >= 0.3:
            band = "DEGRADED"
        else:
            band = "MINOR"
        return round(norm, 4), band

    def _metrics(self) -> Dict[str, Any]:
        a = self._stats["accesses"]
        return {
            "accesses":   a,
            "hits":       self._stats["hits"],
            "misses":     self._stats["misses"],
            "hit_rate":   round(self._stats["hits"] / a, 4) if a else None,
            "evictions":  self._stats["evictions"],
            "writebacks": self._stats["writebacks"],
        }

    def _report(self, n: int, started, finished) -> Dict[str, Any]:
        score, band = self._band()
        high = [v for v in self._violations if v.severity == "HIGH"]
        return {
            "schema_version":   SCHEMA_VERSION,
            "agent":            "cache_verifier",
            "records_checked":  n,
            "cache_enabled":    self._model is not None,
            "config":           dict(self.config) if self._model else None,
            "metrics":          self._metrics(),
            "stats":            dict(self._stats),
            "total_violations": len(self._violations),
            "high_violations":  len(high),
            "severity_score":   score,
            "band":             band,
            "pass":             len(self._violations) == 0,
            "violations":       [v.to_dict() for v in self._violations[:50]],
            "started_at":       started.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "finished_at":      finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duration_s":       round((finished - started).total_seconds(), 3),
        }


# ─────────────────────────────────────────────────────────
# Manifest integration
# ─────────────────────────────────────────────────────────

def _load_log(run_dir: Path, outputs: Dict, key: str, default: str) -> List[Dict]:
    p = run_dir / (outputs.get(key) or default)
    if not p.exists():
        return []
    recs: List[Dict] = []
    with open(p) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    recs.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return recs


def run_from_manifest(manifest_path: Path) -> int:
    """Pipeline entry point. Returns 0 on pass, 1 on any violation."""
    manifest_path = Path(manifest_path)
    try:
        with open(manifest_path) as f:
            manifest = json.load(f)
    except Exception as exc:
        logger.warning("cache_verifier: cannot read manifest: %s", exc)
        return 0

    run_dir = Path(manifest.get("run_dir", manifest_path.parent))
    outputs = manifest.get("outputs", {})
    rtl_log = _load_log(run_dir, outputs, "rtl_commit_log", "rtl_commit.jsonl")
    iss_log = _load_log(run_dir, outputs, "iss_commit_log", "iss_commit.jsonl")
    if not rtl_log:
        logger.info("cache_verifier: no RTL commit log, skipping")
        return 0

    config = manifest.get("cache_config")
    report = CacheVerifier(rtl_log, iss_log, config=config).run()

    report_path = run_dir / "cache_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
        f.write("\n")

    manifest.setdefault("outputs", {})["cache_report"] = "cache_report.json"
    manifest.setdefault("phases", {})["cache_check"] = {
        "status": "pass" if report["pass"] else "fail",
        "violations": report["total_violations"],
    }
    tmp = manifest_path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    tmp.rename(manifest_path)

    logger.info("cache_verifier: enabled=%s, metrics=%s, %d violations, band=%s",
                report["cache_enabled"], report["metrics"],
                report["total_violations"], report["band"])
    return 0 if report["pass"] else 1


# ─────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":          # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description="Cache subsystem verifier")
    ap.add_argument("--manifest", type=Path)
    ap.add_argument("--rtl", type=Path)
    ap.add_argument("--sets", type=int, default=64)
    ap.add_argument("--ways", type=int, default=4)
    ap.add_argument("--line", type=int, default=64)
    ap.add_argument("--policy", default="lru")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.manifest:
        raise SystemExit(run_from_manifest(args.manifest))
    if args.rtl:
        log = []
        with open(args.rtl) as fh:
            for ln in fh:
                ln = ln.strip()
                if ln:
                    log.append(json.loads(ln))
        cfg = {"sets": args.sets, "ways": args.ways,
               "line_size": args.line, "policy": args.policy}
        rep = CacheVerifier(log, config=cfg).run()
        print(json.dumps(rep, indent=2))
        raise SystemExit(0 if rep["pass"] else 1)
    ap.print_help()
