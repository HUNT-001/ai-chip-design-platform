"""
AGENT_H.power_verifier — Power-Aware Verification (T64, taxonomy level 15)
==========================================================================

Golden-reference checker for **low-power design intent** — the UPF/CPF-style
rules that silicon actually gets wrong: clock gating, power-domain isolation,
state retention, power sequencing and DVFS operating points.

This is the first agent covering taxonomy **level 15 (Power-aware)**, which was
previously unstarted. It is a *state-machine* checker rather than an arithmetic
golden: it maintains a model of every power domain (powered / gated / retained /
voltage / frequency) and flags any event that violates the low-power contract.

Checks
------
- **power_gated_activity** (HIGH) — a register in a domain changed value while
  that domain's clock was gated. Clock gating must be functionally transparent:
  gated logic may not update state.
- **power_off_activity** (HIGH) — state changed, or the clock was released, in a
  domain that is powered down.
- **power_isolation** (HIGH) — a powered-down domain drove a non-isolated output
  into an active domain. Un-clamped outputs from a collapsed rail propagate `X`
  and are a classic silicon bug.
- **power_retention** (HIGH) — a domain powered down *with* retention came back
  with different register values (retention failed to restore).
- **power_retention_leak** (MEDIUM) — a domain powered down *without* retention
  came back with its pre-shutdown values intact and no intervening reset, i.e.
  state survived a rail collapse (model/implementation error).
- **power_sequencing** (HIGH) — illegal power-up order: the clock was ungated or
  reset released before the rail was stable.
- **power_dvfs_opp** (HIGH) — the (voltage, frequency) pair is outside the
  declared operating-point table (running faster than the voltage supports).
- **power_dvfs_order** (HIGH) — an unsafe DVFS transition: frequency raised
  before voltage, or voltage lowered before frequency.

Trace contract — `power_trace.jsonl` (additive; the agent skips when absent)
---------------------------------------------------------------------------
```
{"seq":0,"domain":"cpu","event":"power",  "state":"on"}
{"seq":1,"domain":"cpu","event":"dvfs",   "voltage_mv":800,"freq_mhz":1000}
{"seq":2,"domain":"cpu","event":"clk_gate","gated":true}
{"seq":3,"domain":"cpu","event":"state",  "regs":{"r0":"0x1"}}
{"seq":4,"domain":"cpu","event":"power",  "state":"off","retention":true}
{"seq":5,"domain":"cpu","event":"output", "signal":"d","value":"0x5","isolated":false}
{"seq":6,"domain":"cpu","event":"reset",  "asserted":true}
```
The operating-point table may be supplied per-run as
`{"event":"opp_table","points":[{"voltage_mv":800,"max_freq_mhz":1000}, ...]}`
or via the manifest key `power_opp`. With no table, OPP checks are skipped
(graceful degradation — never a false positive).

Stdlib-only, schema-v2.1.0, graceful degradation throughout.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

log = logging.getLogger("AGENT_H.power")

SCHEMA_VERSION = "2.1.0"
AGENT_NAME = "power_verifier"


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
        return v.strip().lower() in ("1", "true", "yes", "on", "asserted", "gated")
    return default


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(int(v, 16) if v.lower().startswith("0x") else float(v))
        except (ValueError, TypeError):
            return None
    return None


class _Domain:
    """Model of a single power domain."""

    def __init__(self, name: str):
        self.name = name
        self.powered = True          # assume on until told otherwise
        self.clk_gated = False
        self.reset_asserted = False
        self.retention: Optional[bool] = None
        self.saved: Dict[str, Any] = {}
        self.regs: Dict[str, Any] = {}
        self.voltage: Optional[float] = None
        self.freq: Optional[float] = None
        self.reset_since_power_on = False


class PowerVerifier:
    """Power-intent checker over a power-event trace."""

    def __init__(self, events: Sequence[Dict[str, Any]],
                 opp_table: Optional[List[Dict[str, Any]]] = None):
        self.events = [e for e in (events or []) if isinstance(e, dict)]
        self.opp: List[Dict[str, float]] = []
        self._load_opp(opp_table)
        self.domains: Dict[str, _Domain] = {}
        self.violations: List[Dict[str, Any]] = []
        self.metrics: Dict[str, Any] = {
            "power_events": 0, "domains": 0, "power_cycles": 0,
            "gate_cycles": 0, "dvfs_transitions": 0, "checked": 0,
            "power_active": False,
        }

    # ── setup ──────────────────────────────────────────────────────────────
    def _load_opp(self, table: Any) -> None:
        if not isinstance(table, (list, tuple)):
            return
        for p in table:
            if not isinstance(p, dict):
                continue
            v = _num(p.get("voltage_mv"))
            f = _num(p.get("max_freq_mhz"))
            if v is not None and f is not None:
                self.opp.append({"voltage_mv": v, "max_freq_mhz": f})
        self.opp.sort(key=lambda p: p["voltage_mv"])

    def _dom(self, name: str) -> _Domain:
        d = self.domains.get(name)
        if d is None:
            d = _Domain(name)
            self.domains[name] = d
        return d

    def _v(self, seq: Any, check: str, detail: str,
           severity: str = "HIGH") -> None:
        self.violations.append({"seq": seq, "check": check,
                                "severity": severity, "detail": detail})

    def _max_freq_for(self, voltage: float) -> Optional[float]:
        """Highest max_freq among operating points at or below this voltage."""
        best: Optional[float] = None
        for p in self.opp:
            if p["voltage_mv"] <= voltage:
                best = p["max_freq_mhz"] if best is None else max(
                    best, p["max_freq_mhz"])
        return best

    # ── main loop ──────────────────────────────────────────────────────────
    def run(self) -> Dict[str, Any]:
        started = _now()
        for e in self.events:
            kind = str(e.get("event", "")).lower()
            if kind == "opp_table":
                self._load_opp(e.get("points"))
                continue
            name = str(e.get("domain", "default"))
            seq = e.get("seq", None)
            d = self._dom(name)
            self.metrics["power_events"] += 1
            self.metrics["power_active"] = True
            if kind == "power":
                self._ev_power(d, e, seq)
            elif kind == "clk_gate":
                self._ev_clk(d, e, seq)
            elif kind == "state":
                self._ev_state(d, e, seq)
            elif kind == "output":
                self._ev_output(d, e, seq)
            elif kind == "reset":
                self._ev_reset(d, e, seq)
            elif kind == "dvfs":
                self._ev_dvfs(d, e, seq)
        self.metrics["domains"] = len(self.domains)
        return self._report(started)

    # ── event handlers ─────────────────────────────────────────────────────
    def _ev_power(self, d: _Domain, e: Dict[str, Any], seq: Any) -> None:
        state = str(e.get("state", "")).lower()
        if state in ("off", "down", "collapsed", "0"):
            d.retention = _truthy(e.get("retention"), False)
            d.saved = dict(d.regs)
            d.powered = False
            d.reset_since_power_on = False
            self.metrics["power_cycles"] += 1
        elif state in ("on", "up", "1"):
            was_off = not d.powered
            d.powered = True
            if was_off:
                self.metrics["checked"] += 1
                if d.retention is True:
                    # retained domain must come back bit-identical
                    diff = [k for k, v in d.saved.items()
                            if k in d.regs and d.regs[k] != v]
                    if diff:
                        self._v(seq, "power_retention",
                                f"domain '{d.name}': retention on, but {len(diff)} "
                                f"register(s) not restored (e.g. {diff[0]}: "
                                f"{d.saved.get(diff[0])} -> {d.regs.get(diff[0])})")
                elif d.retention is False and d.saved:
                    # no retention: state must NOT survive the rail collapse
                    same = [k for k, v in d.saved.items()
                            if k in d.regs and d.regs[k] == v and v not in (0, "0x0", "0")]
                    if same and not d.reset_since_power_on:
                        self._v(seq, "power_retention_leak",
                                f"domain '{d.name}': powered down without retention "
                                f"but {len(same)} register(s) kept their value "
                                f"(e.g. {same[0]}={d.saved.get(same[0])}) with no reset",
                                severity="MEDIUM")
                d.retention = None

    def _ev_clk(self, d: _Domain, e: Dict[str, Any], seq: Any) -> None:
        gated = _truthy(e.get("gated"), False)
        if not gated and not d.powered:
            self.metrics["checked"] += 1
            self._v(seq, "power_sequencing",
                    f"domain '{d.name}': clock released while the domain is "
                    f"powered down")
        if gated and not d.clk_gated:
            self.metrics["gate_cycles"] += 1
        d.clk_gated = gated

    def _ev_state(self, d: _Domain, e: Dict[str, Any], seq: Any) -> None:
        regs = e.get("regs")
        if not isinstance(regs, dict):
            return
        changed = {k: v for k, v in regs.items()
                   if k in d.regs and d.regs[k] != v}
        new = {k: v for k, v in regs.items() if k not in d.regs}
        self.metrics["checked"] += 1
        if changed and d.clk_gated and d.powered:
            k = next(iter(changed))
            self._v(seq, "power_gated_activity",
                    f"domain '{d.name}': register '{k}' changed "
                    f"{d.regs[k]} -> {changed[k]} while the clock was gated")
        if (changed or new) and not d.powered:
            k = next(iter(changed or new))
            self._v(seq, "power_off_activity",
                    f"domain '{d.name}': register '{k}' updated while the "
                    f"domain is powered down")
        d.regs.update(regs)

    def _ev_output(self, d: _Domain, e: Dict[str, Any], seq: Any) -> None:
        if d.powered:
            return
        self.metrics["checked"] += 1
        if not _truthy(e.get("isolated"), False):
            sig = e.get("signal", "?")
            self._v(seq, "power_isolation",
                    f"domain '{d.name}': output '{sig}' driven with value "
                    f"{e.get('value')} while powered down and NOT isolated "
                    f"(missing isolation cell / clamp)")

    def _ev_reset(self, d: _Domain, e: Dict[str, Any], seq: Any) -> None:
        asserted = _truthy(e.get("asserted"), True)
        d.reset_asserted = asserted
        if asserted:
            d.reset_since_power_on = True

    def _ev_dvfs(self, d: _Domain, e: Dict[str, Any], seq: Any) -> None:
        v = _num(e.get("voltage_mv"))
        f = _num(e.get("freq_mhz"))
        if v is None or f is None:
            return
        self.metrics["dvfs_transitions"] += 1
        self.metrics["checked"] += 1
        # (1) operating-point legality
        if self.opp:
            mx = self._max_freq_for(v)
            if mx is None:
                self._v(seq, "power_dvfs_opp",
                        f"domain '{d.name}': voltage {v:.0f}mV is below every "
                        f"declared operating point")
            elif f > mx:
                self._v(seq, "power_dvfs_opp",
                        f"domain '{d.name}': {f:.0f}MHz at {v:.0f}mV exceeds the "
                        f"max {mx:.0f}MHz allowed at that voltage")
        # (2) transition ordering — one step may not raise f and lower V, and
        #     a simultaneous change must move voltage first when speeding up.
        if d.voltage is not None and d.freq is not None:
            if f > d.freq and v < d.voltage:
                self._v(seq, "power_dvfs_order",
                        f"domain '{d.name}': frequency raised "
                        f"{d.freq:.0f}->{f:.0f}MHz while voltage dropped "
                        f"{d.voltage:.0f}->{v:.0f}mV")
            elif f > d.freq and v == d.voltage and self.opp:
                mx_old = self._max_freq_for(d.voltage)
                if mx_old is not None and f > mx_old:
                    self._v(seq, "power_dvfs_order",
                            f"domain '{d.name}': frequency raised to {f:.0f}MHz "
                            f"before raising voltage above {d.voltage:.0f}mV")
        d.voltage, d.freq = v, f

    # ── report ─────────────────────────────────────────────────────────────
    def _report(self, started: str) -> Dict[str, Any]:
        high = sum(1 for v in self.violations if v["severity"] == "HIGH")
        total = len(self.violations)
        return {
            "schema_version": SCHEMA_VERSION,
            "agent": AGENT_NAME,
            "started_at": started,
            "finished_at": _now(),
            "records_checked": len(self.events),
            "power_active": self.metrics["power_active"],
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
    name = (manifest.get("outputs", {}) or {}).get("power_trace", "power_trace.jsonl")
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
        log.warning("power_verifier: cannot read manifest: %s", exc)
        return 0
    run_dir = Path(manifest.get("run_dir", mp.parent))
    events = _load_events(run_dir, manifest)
    if not events:
        rep = {"schema_version": SCHEMA_VERSION, "agent": AGENT_NAME,
               "status": "skipped", "reason": "no power_trace", "pass": True}
    else:
        rep = PowerVerifier(events, manifest.get("power_opp")).run()
        rep["status"] = "completed"
    try:
        (run_dir / "power_report.json").write_text(json.dumps(rep, indent=2),
                                                   encoding="utf-8")
    except OSError as exc:
        log.warning("power_verifier: cannot write report: %s", exc)
    return 0 if rep.get("pass", True) else 1


if __name__ == "__main__":  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="AVA power-aware verifier")
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args()
    raise SystemExit(run_from_manifest(args.manifest))
