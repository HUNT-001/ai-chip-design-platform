"""
AGENT_H.interrupt_verifier — Interrupt Controller Checker (T46)
================================================================

Golden-reference checker for the RISC-V interrupt controllers — **PLIC**
(Platform-Level Interrupt Controller, external interrupts) and **CLINT**
(Core-Local Interruptor, timer + software interrupts). Interrupt-handling bugs
(wrong priority arbitration, a masked interrupt firing, a timer that misses its
compare) are severe and common, and are not covered by the trap/privilege
agents (which check the CSR side of an already-delivered trap).

PLIC semantics checked
----------------------
- **claim** returns the highest-priority interrupt that is *pending* AND
  *enabled* for the context AND whose priority is strictly greater than the
  context's *threshold*; ties break to the **lowest source id**; priority 0
  means "never interrupt".  (`plic_claim_wrong`, HIGH)
- **threshold masking** — a source with priority ≤ threshold is never claimed.
  (`plic_threshold`, HIGH)
- **priority-0** sources are never claimed. (`plic_priority0`, HIGH)
- **claim clears pending** — a claimed source is no longer claimable until it is
  re-asserted (verified implicitly by the golden model's state progression).

CLINT semantics checked
-----------------------
- **MTIP** (timer interrupt pending) is set **iff** `mtime >= mtimecmp`.
  (`clint_mtip`, HIGH)
- **MSIP** (software interrupt pending) equals the written msip bit.
  (`clint_msip`, HIGH)

Additive trace contract (a separate interrupt-event stream)
-----------------------------------------------------------
```
events (processed in order):
  {"op":"config", "priorities":{"3":7}, "enables":{"0":[3,5]}, "thresholds":{"0":2}}
  {"op":"pending", "source":3}
  {"op":"claim",   "context":0, "result":3}     # DUT's claimed id (0 = none)
  {"op":"complete","context":0, "source":3}
  {"op":"clint",   "mtime":100, "mtimecmp":100, "mtip":true, "msip":false, "expected_msip":false}
```

Stdlib-only, schema-v2.1.0 report, graceful degradation throughout.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

log = logging.getLogger("AGENT_H.interrupt")

SCHEMA_VERSION = "2.1.0"
AGENT_NAME = "interrupt_verifier"


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


# ─────────────────────────────────────────────────────────────────────────────
# Golden PLIC model
# ─────────────────────────────────────────────────────────────────────────────
class PLICModel:
    def __init__(self) -> None:
        self.prio: Dict[int, int] = {}          # source → priority
        self.enable: Dict[Any, set] = {}        # context → set(sources)
        self.thresh: Dict[Any, int] = {}        # context → threshold
        self.pending: set = set()

    def configure(self, priorities=None, enables=None, thresholds=None) -> None:
        for s, p in (priorities or {}).items():
            si, pi = _to_int(s), _to_int(p)
            if si is not None and pi is not None:
                self.prio[si] = pi
        for ctx, srcs in (enables or {}).items():
            self.enable[_to_int(ctx) if _to_int(ctx) is not None else ctx] = \
                set(_to_int(s) for s in srcs if _to_int(s) is not None)
        for ctx, t in (thresholds or {}).items():
            ti = _to_int(t)
            if ti is not None:
                self.thresh[_to_int(ctx) if _to_int(ctx) is not None else ctx] = ti

    def set_pending(self, s: int) -> None:
        self.pending.add(s)

    def clear_pending(self, s: int) -> None:
        self.pending.discard(s)

    def best(self, ctx: Any) -> int:
        """Highest-priority claimable source for ``ctx`` (0 = none)."""
        en = self.enable.get(ctx, set())
        th = self.thresh.get(ctx, 0)
        cands = [s for s in self.pending
                 if s in en and self.prio.get(s, 0) > th and self.prio.get(s, 0) > 0]
        if not cands:
            return 0
        # highest priority, then lowest id
        return max(cands, key=lambda s: (self.prio.get(s, 0), -s))


# ─────────────────────────────────────────────────────────────────────────────
# Verifier
# ─────────────────────────────────────────────────────────────────────────────
class InterruptVerifier:
    def __init__(self, events: Sequence[Dict[str, Any]]):
        self.events = [e for e in (events or []) if isinstance(e, dict)]
        self.violations: List[Dict[str, Any]] = []
        self.metrics = {"claims": 0, "completes": 0, "pendings": 0,
                        "clint_checks": 0, "plic_active": False, "clint_active": False}

    def _v(self, i: int, check: str, detail: str) -> None:
        self.violations.append({"event": i, "check": check,
                                "severity": "HIGH", "detail": detail})

    def run(self) -> Dict[str, Any]:
        started = _now()
        plic = PLICModel()
        for i, e in enumerate(self.events):
            op = str(e.get("op", "")).lower()
            if op == "config":
                plic.configure(e.get("priorities"), e.get("enables"),
                               e.get("thresholds"))
            elif op in ("pending", "set_pending"):
                s = _to_int(e.get("source"))
                if s is not None:
                    plic.set_pending(s)
                    self.metrics["pendings"] += 1
                    self.metrics["plic_active"] = True
            elif op == "claim":
                self._check_claim(i, e, plic)
            elif op == "complete":
                self.metrics["completes"] += 1
            elif op == "clint":
                self._check_clint(i, e)
        return self._report(started)

    def _check_claim(self, i: int, e: Dict[str, Any], plic: PLICModel) -> None:
        self.metrics["claims"] += 1
        self.metrics["plic_active"] = True
        ctx = _to_int(e.get("context"))
        ctx = ctx if ctx is not None else e.get("context")
        golden = plic.best(ctx)
        dut = _to_int(e.get("result", e.get("claimed")))
        if dut is None:
            plic.clear_pending(golden)
            return
        if dut != golden:
            self._v(i, "plic_claim_wrong",
                    f"context {ctx}: claimed {dut}, golden highest-priority is {golden}")
        if dut != 0:
            if plic.prio.get(dut, 0) == 0:
                self._v(i, "plic_priority0", f"claimed source {dut} has priority 0")
            if plic.prio.get(dut, 0) <= plic.thresh.get(ctx, 0):
                self._v(i, "plic_threshold",
                        f"claimed source {dut} priority {plic.prio.get(dut, 0)} "
                        f"≤ threshold {plic.thresh.get(ctx, 0)}")
        plic.clear_pending(dut if dut != 0 else golden)

    def _check_clint(self, i: int, e: Dict[str, Any]) -> None:
        self.metrics["clint_checks"] += 1
        self.metrics["clint_active"] = True
        mt, cmp_ = _to_int(e.get("mtime")), _to_int(e.get("mtimecmp"))
        if mt is not None and cmp_ is not None and e.get("mtip") is not None:
            golden = mt >= cmp_
            if bool(e.get("mtip")) != golden:
                self._v(i, "clint_mtip",
                        f"MTIP={e.get('mtip')} but mtime({mt}) >= mtimecmp({cmp_}) is {golden}")
        if e.get("msip") is not None and e.get("expected_msip") is not None:
            if bool(e.get("msip")) != bool(e.get("expected_msip")):
                self._v(i, "clint_msip",
                        f"MSIP={e.get('msip')} but expected {e.get('expected_msip')}")

    def _report(self, started: str) -> Dict[str, Any]:
        total = len(self.violations)
        high = total
        return {
            "schema_version": SCHEMA_VERSION,
            "agent": AGENT_NAME,
            "started_at": started,
            "finished_at": _now(),
            "records_checked": len(self.events),
            "interrupt_active": self.metrics["plic_active"] or self.metrics["clint_active"],
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
def _load_events(run_dir: Path, manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    name = (manifest.get("outputs", {}) or {}).get("interrupt_trace",
                                                    "interrupt_trace.jsonl")
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
        log.warning("interrupt_verifier: cannot read manifest: %s", exc)
        return 0
    run_dir = Path(manifest.get("run_dir", mp.parent))
    events = _load_events(run_dir, manifest)
    if not events:
        rep = {"schema_version": SCHEMA_VERSION, "agent": AGENT_NAME,
               "status": "skipped", "reason": "no interrupt_trace", "pass": True}
    else:
        rep = InterruptVerifier(events).run()
        rep["status"] = "completed"
    try:
        (run_dir / "interrupt_report.json").write_text(
            json.dumps(rep, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("interrupt_verifier: cannot write report: %s", exc)
    return 0 if rep.get("pass", True) else 1


if __name__ == "__main__":  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="AVA interrupt-controller checker")
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args()
    raise SystemExit(run_from_manifest(args.manifest))
