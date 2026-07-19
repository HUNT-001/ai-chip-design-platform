"""
AGENT_H.equivalence_verifier — Equivalence Checking (T66, upgrades AGENT_L)
===========================================================================

Golden equivalence checker for comparing two designs (golden vs revised, RTL vs
gate-level, pre- vs post-retiming). `AGENT_L/agent_l_equiv` covered this only
partially; this agent provides a real decision procedure for the tractable
cases plus a rigorous bounded procedure for the rest.

Three complementary engines
---------------------------
1. **Combinational equivalence (exhaustive)** — for a boolean function given as
   a Python callable or a truth table over ≤ ``max_exhaustive_bits`` inputs, the
   checker enumerates the *entire* input space. That is a **proof**, not a
   sample: if it passes, the functions are equivalent. Counterexamples are
   reported as concrete input assignments.
2. **Sequential equivalence (bounded, with state matching)** — drives both
   machines from their reset states over an input sequence and compares outputs
   cycle by cycle. Supports **latency/pipeline offset**: a revised design may
   produce the same outputs `k` cycles later (retiming), and the checker finds
   the offset that aligns them rather than reporting spurious mismatches.
3. **I/O-trace equivalence** — compares two recorded traces (e.g. a golden ISS
   run and an RTL run) for identical output sequences, tolerating a declared
   latency offset.

Checks
------
- **equiv_comb_mismatch** (HIGH) — exhaustive combinational compare found an
  input assignment where the two functions differ.
- **equiv_seq_mismatch** (HIGH) — outputs differ at some cycle under the best
  alignment.
- **equiv_reset_state** (HIGH) — the two designs start from different reset
  states (so no alignment can be trusted).
- **equiv_latency_mismatch** (MEDIUM) — outputs match only under a latency
  offset different from the declared one.
- **equiv_incomplete** (MEDIUM) — the input space was too large to enumerate, so
  the result is a bounded check, not a proof (reported honestly rather than
  claiming equivalence).

Trace contract — `equiv_trace.jsonl` (additive; skipped when absent)
--------------------------------------------------------------------
```
{"event":"comb","name":"alu_add","inputs":4,
 "golden":[0,1,1,0,...],"revised":[0,1,1,0,...]}          # truth tables
{"event":"seq","name":"fifo","latency":2,
 "golden_out":[0,0,1,2,3],"revised_out":[0,0,1,2,3],
 "golden_reset":"0x0","revised_reset":"0x0"}
{"event":"trace","name":"core","golden":[...],"revised":[...],"latency":0}
```

Stdlib-only, schema-v2.1.0, graceful degradation.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

log = logging.getLogger("AGENT_H.equiv")

SCHEMA_VERSION = "2.1.0"
AGENT_NAME = "equivalence_verifier"
MAX_EXHAUSTIVE_BITS = 20                 # 2^20 = 1,048,576 assignments


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# Engine 1 — exhaustive combinational equivalence (a real proof)
# ─────────────────────────────────────────────────────────────────────────────
def comb_equivalent(golden: Callable[[int], int], revised: Callable[[int], int],
                    n_bits: int,
                    max_bits: int = MAX_EXHAUSTIVE_BITS
                    ) -> Tuple[bool, Optional[int], bool]:
    """Exhaustively compare two boolean functions of ``n_bits`` input bits.

    Returns ``(equivalent, counterexample_or_None, exhaustive)``. When
    ``exhaustive`` is False the space was too large and only a bounded subset was
    checked — the caller must not claim a proof.
    """
    if n_bits <= max_bits:
        for x in range(1 << n_bits):
            if golden(x) != revised(x):
                return False, x, True
        return True, None, True
    # too large: deterministic bounded sampling (corners + strided walk)
    import random
    rng = random.Random(0xE9D1F)                 # fixed seed: reproducible probes
    probes = [0, (1 << n_bits) - 1]
    probes += [1 << i for i in range(n_bits)]
    probes += [rng.getrandbits(n_bits) for _ in range(1 << max_bits)]
    for x in probes:
        if golden(x) != revised(x):
            return False, x, False
    return True, None, False


def truth_table_fn(table: Sequence[Any]) -> Callable[[int], int]:
    """Wrap a truth table (indexed by the integer input) as a callable."""
    def _f(x: int) -> Any:
        return table[x] if 0 <= x < len(table) else None
    return _f


# ─────────────────────────────────────────────────────────────────────────────
# Engine 2 — sequential / latency-tolerant output alignment
# ─────────────────────────────────────────────────────────────────────────────
def best_latency_offset(golden: Sequence[Any], revised: Sequence[Any],
                        max_offset: int = 16) -> Tuple[Optional[int], int]:
    """Find the offset k ≥ 0 such that revised[i+k] == golden[i] for the longest
    prefix. Returns ``(best_k_or_None, matched_length)``. ``None`` means no
    offset produced any alignment."""
    best_k: Optional[int] = None
    best_len = -1
    limit = min(max_offset, max(len(golden), len(revised)))
    for k in range(limit + 1):
        n = min(len(golden), len(revised) - k)
        if n <= 0:
            continue
        matched = 0
        for i in range(n):
            if golden[i] == revised[i + k]:
                matched += 1
            else:
                break
        if matched > best_len:
            best_len, best_k = matched, k
        if matched == n and n > 0:
            return k, matched                  # perfect alignment
    return best_k, max(best_len, 0)


class EquivalenceVerifier:
    def __init__(self, events: Sequence[Dict[str, Any]],
                 max_bits: int = MAX_EXHAUSTIVE_BITS):
        self.events = [e for e in (events or []) if isinstance(e, dict)]
        self.max_bits = max_bits
        self.violations: List[Dict[str, Any]] = []
        self.metrics: Dict[str, Any] = {
            "comb_checks": 0, "seq_checks": 0, "trace_checks": 0,
            "exhaustive_proofs": 0, "bounded_checks": 0, "checked": 0,
            "equiv_active": False, "input_space_covered": 0,
        }

    def _v(self, name: Any, check: str, detail: str,
           severity: str = "HIGH") -> None:
        self.violations.append({"unit": name, "check": check,
                                "severity": severity, "detail": detail})

    def run(self) -> Dict[str, Any]:
        started = _now()
        for e in self.events:
            kind = str(e.get("event", "")).lower()
            if kind == "comb":
                self._comb(e)
            elif kind == "seq":
                self._seq(e)
            elif kind == "trace":
                self._trace(e)
        return self._report(started)

    # ── combinational ──────────────────────────────────────────────────────
    def _comb(self, e: Dict[str, Any]) -> None:
        name = e.get("name", "?")
        g, r = e.get("golden"), e.get("revised")
        if not isinstance(g, (list, tuple)) or not isinstance(r, (list, tuple)):
            return
        n_bits = e.get("inputs")
        if not isinstance(n_bits, int) or n_bits < 0:
            n_bits = max(len(g), len(r)).bit_length() - 1
        self.metrics["comb_checks"] += 1
        self.metrics["equiv_active"] = True
        self.metrics["checked"] += 1
        if len(g) != len(r):
            self._v(name, "equiv_comb_mismatch",
                    f"truth tables differ in size ({len(g)} vs {len(r)})")
            return
        ok, cex, exhaustive = comb_equivalent(
            truth_table_fn(g), truth_table_fn(r), n_bits, self.max_bits)
        if exhaustive:
            self.metrics["exhaustive_proofs"] += 1
            self.metrics["input_space_covered"] += 1 << n_bits
        else:
            self.metrics["bounded_checks"] += 1
            self._v(name, "equiv_incomplete",
                    f"input space 2^{n_bits} exceeds the exhaustive limit "
                    f"2^{self.max_bits}; result is a bounded check, not a proof",
                    severity="MEDIUM")
        if not ok and cex is not None:
            self._v(name, "equiv_comb_mismatch",
                    f"differ at input 0x{cex:x}: golden={g[cex] if cex < len(g) else '?'} "
                    f"revised={r[cex] if cex < len(r) else '?'}")

    # ── sequential ─────────────────────────────────────────────────────────
    def _seq(self, e: Dict[str, Any]) -> None:
        name = e.get("name", "?")
        g = e.get("golden_out")
        r = e.get("revised_out")
        if not isinstance(g, (list, tuple)) or not isinstance(r, (list, tuple)):
            return
        self.metrics["seq_checks"] += 1
        self.metrics["equiv_active"] = True
        self.metrics["checked"] += 1
        gr, rr = e.get("golden_reset"), e.get("revised_reset")
        if gr is not None and rr is not None and gr != rr:
            self._v(name, "equiv_reset_state",
                    f"reset states differ: golden={gr} revised={rr}")
        declared = e.get("latency")
        k, matched = best_latency_offset(g, r)
        n = min(len(g), len(r) - (k or 0))
        if k is None or matched < max(n, 0):
            idx = matched
            gv = g[idx] if idx < len(g) else "?"
            off = (k or 0) + idx
            rv = r[off] if off < len(r) else "?"
            self._v(name, "equiv_seq_mismatch",
                    f"outputs diverge at cycle {idx} (offset {k}): "
                    f"golden={gv} revised={rv}")
        elif isinstance(declared, int) and k is not None and k != declared:
            self._v(name, "equiv_latency_mismatch",
                    f"outputs are equivalent but at latency offset {k}, "
                    f"not the declared {declared}", severity="MEDIUM")

    # ── raw trace ──────────────────────────────────────────────────────────
    def _trace(self, e: Dict[str, Any]) -> None:
        name = e.get("name", "?")
        g, r = e.get("golden"), e.get("revised")
        if not isinstance(g, (list, tuple)) or not isinstance(r, (list, tuple)):
            return
        self.metrics["trace_checks"] += 1
        self.metrics["equiv_active"] = True
        self.metrics["checked"] += 1
        lat = e.get("latency", 0)
        lat = lat if isinstance(lat, int) and lat >= 0 else 0
        n = min(len(g), len(r) - lat)
        for i in range(max(n, 0)):
            if g[i] != r[i + lat]:
                self._v(name, "equiv_seq_mismatch",
                        f"trace diverges at index {i} (latency {lat}): "
                        f"golden={g[i]} revised={r[i + lat]}")
                return

    def _report(self, started: str) -> Dict[str, Any]:
        high = sum(1 for v in self.violations if v["severity"] == "HIGH")
        total = len(self.violations)
        return {
            "schema_version": SCHEMA_VERSION,
            "agent": AGENT_NAME,
            "started_at": started,
            "finished_at": _now(),
            "records_checked": len(self.events),
            "equiv_active": self.metrics["equiv_active"],
            "metrics": self.metrics,
            "total_violations": total,
            "high_violations": high,
            "severity_score": high * 3 + (total - high),
            "band": "CLEAN" if total == 0 else ("CRITICAL" if high else "DEGRADED"),
            "pass": high == 0,
            "violations": self.violations[:100],
        }


# ─────────────────────────────────────────────────────────────────────────────
# Manifest entry point
# ─────────────────────────────────────────────────────────────────────────────
def _load_events(run_dir: Path, manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    name = (manifest.get("outputs", {}) or {}).get("equiv_trace", "equiv_trace.jsonl")
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
        log.warning("equivalence_verifier: cannot read manifest: %s", exc)
        return 0
    run_dir = Path(manifest.get("run_dir", mp.parent))
    events = _load_events(run_dir, manifest)
    if not events:
        rep = {"schema_version": SCHEMA_VERSION, "agent": AGENT_NAME,
               "status": "skipped", "reason": "no equiv_trace", "pass": True}
    else:
        rep = EquivalenceVerifier(events).run()
        rep["status"] = "completed"
    try:
        (run_dir / "equiv_report.json").write_text(json.dumps(rep, indent=2),
                                                   encoding="utf-8")
    except OSError as exc:
        log.warning("equivalence_verifier: cannot write report: %s", exc)
    return 0 if rep.get("pass", True) else 1


if __name__ == "__main__":  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="AVA equivalence checker")
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args()
    raise SystemExit(run_from_manifest(args.manifest))
