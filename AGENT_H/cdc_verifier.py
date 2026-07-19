"""
AGENT_H.cdc_verifier — Clock-Domain-Crossing Checker (T65, upgrades AGENT_J)
============================================================================

Golden checker for **clock-domain crossings** and **reset-domain crossings** —
the structural rules that make an asynchronous crossing safe. CDC bugs do not
show up in a single-clock simulation, so they need a dedicated structural /
protocol checker; this agent provides one.

`AGENT_J/agent_j_cdc` previously covered this only partially (heuristic hints).
This is a proper model with an explicit rule set and a declared crossing
inventory.

Checks
------
- **cdc_unsynchronized** (HIGH) — a signal crosses from one clock domain to
  another with **no synchronizer** on the destination side.
- **cdc_shallow_sync** (HIGH) — a crossing is synchronized with fewer than the
  required flop stages (default 2; configurable per crossing). One flop does not
  resolve metastability.
- **cdc_multibit_unsafe** (HIGH) — a **multi-bit** bus crossed through plain
  flop synchronizers. Independent per-bit settling re-converges to a corrupt
  word; multi-bit crossings need gray coding, a handshake, or an async FIFO.
- **cdc_gray_violation** (HIGH) — a bus declared gray-coded changed by more than
  one bit between consecutive samples (so it is not actually gray-coded).
- **cdc_handshake_protocol** (HIGH) — req/ack four-phase handshake violated:
  `ack` asserted without a pending `req`, `req` deasserted before `ack`, or a
  new `req` while the previous transaction is still outstanding.
- **cdc_reset_crossing** (HIGH) — an asynchronous reset asserted in one domain
  reaches another domain without synchronized de-assertion (reset-domain
  crossing / removal-recovery hazard).
- **cdc_glitch_source** (MEDIUM) — combinational logic drives a crossing
  directly (the source is not a registered flop output), which can emit glitches
  the destination may latch.

Trace contract — `cdc_trace.jsonl` (additive; skipped when absent)
------------------------------------------------------------------
A crossing inventory plus optional sampled activity:
```
{"event":"crossing","signal":"data_val","src_clk":"clk_a","dst_clk":"clk_b",
 "width":1,"sync_stages":2,"scheme":"ff_sync","src_registered":true}
{"event":"crossing","signal":"ptr","src_clk":"clk_a","dst_clk":"clk_b",
 "width":4,"sync_stages":2,"scheme":"gray"}
{"event":"sample","signal":"ptr","value":"0x3"}
{"event":"handshake","signal":"xfer","req":true,"ack":false}
{"event":"reset","domain":"clk_b","src_domain":"clk_a","async_assert":true,
 "sync_deassert":false}
```
`scheme` ∈ `ff_sync` | `gray` | `handshake` | `async_fifo` | `none`.
Signals with no declared crossing are ignored (never a false positive).

Stdlib-only, schema-v2.1.0, graceful degradation.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

log = logging.getLogger("AGENT_H.cdc")

SCHEMA_VERSION = "2.1.0"
AGENT_NAME = "cdc_verifier"
DEFAULT_MIN_STAGES = 2
# Schemes that are inherently safe for multi-bit buses.
_MULTIBIT_SAFE = {"gray", "handshake", "async_fifo", "mux_recirc"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truthy(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on", "asserted")
    return default


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


def popcount(x: int) -> int:
    return bin(x & ((1 << 4096) - 1)).count("1")


class _Crossing:
    def __init__(self, e: Dict[str, Any]):
        self.signal = str(e.get("signal", "?"))
        self.src = str(e.get("src_clk", e.get("src_domain", "")))
        self.dst = str(e.get("dst_clk", e.get("dst_domain", "")))
        self.width = _to_int(e.get("width")) or 1
        self.stages = _to_int(e.get("sync_stages"))
        self.scheme = str(e.get("scheme", "ff_sync")).lower()
        self.src_registered = _truthy(e.get("src_registered"), True)
        self.min_stages = _to_int(e.get("min_stages")) or DEFAULT_MIN_STAGES
        self.last_value: Optional[int] = None
        # handshake state
        self.req = False
        self.ack = False
        self.outstanding = False

    @property
    def is_async(self) -> bool:
        return bool(self.src) and bool(self.dst) and self.src != self.dst


class CDCVerifier:
    def __init__(self, events: Sequence[Dict[str, Any]]):
        self.events = [e for e in (events or []) if isinstance(e, dict)]
        self.crossings: Dict[str, _Crossing] = {}
        self.violations: List[Dict[str, Any]] = []
        self.metrics: Dict[str, Any] = {
            "crossings": 0, "async_crossings": 0, "multibit_crossings": 0,
            "samples": 0, "handshakes": 0, "checked": 0, "cdc_active": False,
            "by_scheme": {},
        }

    def _v(self, seq: Any, check: str, detail: str,
           severity: str = "HIGH") -> None:
        self.violations.append({"seq": seq, "check": check,
                                "severity": severity, "detail": detail})

    # ── main ───────────────────────────────────────────────────────────────
    def run(self) -> Dict[str, Any]:
        started = _now()
        for e in self.events:
            kind = str(e.get("event", "")).lower()
            seq = e.get("seq")
            if kind == "crossing":
                self._declare(e, seq)
            elif kind == "sample":
                self._sample(e, seq)
            elif kind == "handshake":
                self._handshake(e, seq)
            elif kind == "reset":
                self._reset(e, seq)
        return self._report(started)

    def _declare(self, e: Dict[str, Any], seq: Any) -> None:
        c = _Crossing(e)
        self.crossings[c.signal] = c
        self.metrics["crossings"] += 1
        self.metrics["cdc_active"] = True
        self.metrics["by_scheme"][c.scheme] = \
            self.metrics["by_scheme"].get(c.scheme, 0) + 1
        if not c.is_async:
            return                                   # same domain: nothing to do
        self.metrics["async_crossings"] += 1
        if c.width > 1:
            self.metrics["multibit_crossings"] += 1
        self.metrics["checked"] += 1

        # (1) no synchronizer at all
        if c.scheme in ("none", "") or (c.stages is not None and c.stages == 0):
            self._v(seq, "cdc_unsynchronized",
                    f"'{c.signal}' crosses {c.src} -> {c.dst} with no "
                    f"synchronizer (scheme={c.scheme or 'none'})")
            return
        # (2) too few flop stages for a plain synchronizer
        if c.scheme == "ff_sync" and c.stages is not None and c.stages < c.min_stages:
            self._v(seq, "cdc_shallow_sync",
                    f"'{c.signal}' {c.src} -> {c.dst} uses {c.stages} sync "
                    f"stage(s); at least {c.min_stages} required for MTBF")
        # (3) multi-bit through plain flop sync
        if c.width > 1 and c.scheme not in _MULTIBIT_SAFE:
            self._v(seq, "cdc_multibit_unsafe",
                    f"'{c.signal}' is {c.width} bits wide crossing "
                    f"{c.src} -> {c.dst} via '{c.scheme}'; multi-bit crossings "
                    f"need gray coding, a handshake or an async FIFO")
        # (4) combinational source can glitch into the crossing
        if not c.src_registered:
            self._v(seq, "cdc_glitch_source",
                    f"'{c.signal}' is driven by combinational logic into a "
                    f"{c.src} -> {c.dst} crossing (glitch hazard)",
                    severity="MEDIUM")

    def _sample(self, e: Dict[str, Any], seq: Any) -> None:
        sig = str(e.get("signal", "?"))
        c = self.crossings.get(sig)
        if c is None:
            return                                    # undeclared: ignore
        val = _to_int(e.get("value"))
        if val is None:
            return
        self.metrics["samples"] += 1
        if c.scheme == "gray" and c.last_value is not None and val != c.last_value:
            self.metrics["checked"] += 1
            delta = popcount(val ^ c.last_value)
            if delta > 1:
                self._v(seq, "cdc_gray_violation",
                        f"'{sig}' declared gray-coded but changed "
                        f"0x{c.last_value:x} -> 0x{val:x} ({delta} bits at once)")
        c.last_value = val

    def _handshake(self, e: Dict[str, Any], seq: Any) -> None:
        sig = str(e.get("signal", "?"))
        c = self.crossings.get(sig)
        if c is None:
            c = _Crossing({"signal": sig, "scheme": "handshake"})
            self.crossings[sig] = c
        req = _truthy(e.get("req"), False)
        ack = _truthy(e.get("ack"), False)
        self.metrics["handshakes"] += 1
        self.metrics["checked"] += 1
        # ack without a pending req
        if ack and not c.req and not c.outstanding:
            self._v(seq, "cdc_handshake_protocol",
                    f"'{sig}': ack asserted with no outstanding req")
        # req dropped before ack arrived
        if c.req and not req and not c.ack and not ack:
            self._v(seq, "cdc_handshake_protocol",
                    f"'{sig}': req de-asserted before ack was observed "
                    f"(transaction lost)")
        # new req while previous transfer still outstanding
        if req and not c.req and c.outstanding:
            self._v(seq, "cdc_handshake_protocol",
                    f"'{sig}': new req asserted while the previous transaction "
                    f"is still outstanding")
        if req and not c.req:
            c.outstanding = True
        if ack and c.outstanding:
            c.outstanding = False
        c.req, c.ack = req, ack

    def _reset(self, e: Dict[str, Any], seq: Any) -> None:
        dom = str(e.get("domain", "?"))
        src = str(e.get("src_domain", ""))
        if not src or src == dom:
            return
        self.metrics["checked"] += 1
        self.metrics["cdc_active"] = True
        if _truthy(e.get("async_assert"), False) and \
                not _truthy(e.get("sync_deassert"), False):
            self._v(seq, "cdc_reset_crossing",
                    f"reset from '{src}' asserts asynchronously into '{dom}' "
                    f"without synchronized de-assertion "
                    f"(removal/recovery violation)")

    def _report(self, started: str) -> Dict[str, Any]:
        high = sum(1 for v in self.violations if v["severity"] == "HIGH")
        total = len(self.violations)
        return {
            "schema_version": SCHEMA_VERSION,
            "agent": AGENT_NAME,
            "started_at": started,
            "finished_at": _now(),
            "records_checked": len(self.events),
            "cdc_active": self.metrics["cdc_active"],
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
    name = (manifest.get("outputs", {}) or {}).get("cdc_trace", "cdc_trace.jsonl")
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
        log.warning("cdc_verifier: cannot read manifest: %s", exc)
        return 0
    run_dir = Path(manifest.get("run_dir", mp.parent))
    events = _load_events(run_dir, manifest)
    if not events:
        rep = {"schema_version": SCHEMA_VERSION, "agent": AGENT_NAME,
               "status": "skipped", "reason": "no cdc_trace", "pass": True}
    else:
        rep = CDCVerifier(events).run()
        rep["status"] = "completed"
    try:
        (run_dir / "cdc_report.json").write_text(json.dumps(rep, indent=2),
                                                 encoding="utf-8")
    except OSError as exc:
        log.warning("cdc_verifier: cannot write report: %s", exc)
    return 0 if rep.get("pass", True) else 1


if __name__ == "__main__":  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="AVA CDC / RDC checker")
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args()
    raise SystemExit(run_from_manifest(args.manifest))
