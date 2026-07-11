"""
AGENT_H.aia_verifier — Advanced Interrupt Architecture / IMSIC (T51)
=====================================================================

Golden checker for the RISC-V Advanced Interrupt Architecture (AIA), specifically
the **IMSIC** (Incoming Message-Signaled Interrupt Controller) — the piece that
receives MSIs and selects, per hart, the highest-priority interrupt to present
to the core via the **`topei`** (top external interrupt) register.

IMSIC selection rules (the novel, bug-prone logic)
--------------------------------------------------
Unlike the PLIC (larger `priority` value = more urgent, ties → lowest id), IMSIC
priority is the **interrupt identity number itself, smaller = higher priority**.
`topei` returns the identity of the interrupt that is:

- **pending** (`eip[i] = 1`) AND **enabled** (`eie[i] = 1`), AND
- eligible w.r.t. **`eithreshold`** — if the threshold is non-zero only
  identities `1 .. eithreshold-1` are eligible (identity ≥ threshold masked),
- **only if `eidelivery` is enabled**; otherwise `topei = 0` (no delivery).

Among eligible interrupts the **smallest identity** wins.

Checks
------
- **imsic_topei** (HIGH) — the DUT's `topei` != the golden selection.
- **imsic_delivery** (HIGH) — `topei != 0` while `eidelivery` is off.
- **imsic_threshold** (HIGH) — the selected identity is ≥ `eithreshold` (masked).
- **imsic_disabled** (HIGH) — the selected identity is not pending+enabled.

Additive `aia_trace.jsonl` contract
-----------------------------------
```
{"op":"imsic_config","eidelivery":1,"eithreshold":8,"eie":[2,3,7],"eip":[3,7]}
{"op":"imsic_topei","result":3}     # DUT topei identity (0 = none)
```

Stdlib-only, schema-v2.1.0 report, graceful degradation throughout.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

log = logging.getLogger("AGENT_H.aia")

SCHEMA_VERSION = "2.1.0"
AGENT_NAME = "aia_verifier"


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


def _idset(raw: Any) -> set:
    out = set()
    for x in raw or []:
        xi = _to_int(x)
        if xi is not None:
            out.add(xi)
    return out


class IMSICModel:
    def __init__(self) -> None:
        self.eidelivery = True
        self.eithreshold = 0        # 0 ⇒ no masking
        self.eie: set = set()
        self.eip: set = set()

    def configure(self, eidelivery=None, eithreshold=None,
                  eie=None, eip=None) -> None:
        if eidelivery is not None:
            self.eidelivery = bool(_to_int(eidelivery))
        if eithreshold is not None:
            t = _to_int(eithreshold)
            if t is not None:
                self.eithreshold = t
        if eie is not None:
            self.eie = _idset(eie)
        if eip is not None:
            self.eip = _idset(eip)

    def eligible(self, i: int) -> bool:
        return (i in self.eip and i in self.eie
                and (self.eithreshold == 0 or i < self.eithreshold))

    def topei(self) -> int:
        if not self.eidelivery:
            return 0
        cands = [i for i in (self.eip & self.eie) if self.eligible(i)]
        return min(cands) if cands else 0     # smallest identity = highest priority


class APLICModel:
    """Advanced PLIC, direct-delivery mode. Unlike IMSIC (priority = identity)
    and PLIC (larger priority value wins), APLIC priority is the target
    ``iprio`` where **smaller = higher priority** (0 → treated as 1); a source is
    eligible only if pending (``setip``) ∧ enabled (``setie``) ∧ *active*
    (sourcecfg not inactive/delegated) ∧ ``iprio < ithreshold`` (0 ⇒ no
    threshold), and delivery is enabled (``idelivery``). ``topi``/``claimi``
    returns the lowest ``iprio``, ties → lowest identity."""

    def __init__(self) -> None:
        self.idelivery = True
        self.ithreshold = 0
        self.setip: set = set()
        self.setie: set = set()
        self.iprio: Dict[int, int] = {}
        self.sourcecfg: Dict[int, str] = {}

    def configure(self, idelivery=None, ithreshold=None, setip=None,
                  setie=None, iprio=None, sourcecfg=None) -> None:
        if idelivery is not None:
            self.idelivery = bool(_to_int(idelivery))
        if ithreshold is not None:
            t = _to_int(ithreshold)
            if t is not None:
                self.ithreshold = t
        if setip is not None:
            self.setip = _idset(setip)
        if setie is not None:
            self.setie = _idset(setie)
        if iprio is not None:
            for s, p in (iprio or {}).items():
                si, pi = _to_int(s), _to_int(p)
                if si is not None and pi is not None:
                    self.iprio[si] = pi
        if sourcecfg is not None:
            for s, m in (sourcecfg or {}).items():
                si = _to_int(s)
                if si is not None:
                    self.sourcecfg[si] = str(m).lower()

    def active(self, s: int) -> bool:
        return self.sourcecfg.get(s, "active") not in ("inactive", "delegated", "0")

    def prio(self, s: int) -> int:
        p = self.iprio.get(s, 1)
        return p if p != 0 else 1              # iprio 0 is reserved → 1

    def eligible(self, s: int) -> bool:
        return (s in self.setip and s in self.setie and self.active(s)
                and (self.ithreshold == 0 or self.prio(s) < self.ithreshold))

    def topi(self) -> int:
        if not self.idelivery:
            return 0
        cands = [s for s in (self.setip & self.setie) if self.eligible(s)]
        if not cands:
            return 0
        return min(cands, key=lambda s: (self.prio(s), s))   # lowest iprio, tie lowest id


class AIAVerifier:
    def __init__(self, events: Sequence[Dict[str, Any]]):
        self.events = [e for e in (events or []) if isinstance(e, dict)]
        self.violations: List[Dict[str, Any]] = []
        self.metrics = {"topei_queries": 0, "topi_queries": 0, "aia_active": False}

    def _v(self, i: int, check: str, detail: str) -> None:
        self.violations.append({"event": i, "check": check,
                                "severity": "HIGH", "detail": detail})

    def run(self) -> Dict[str, Any]:
        started = _now()
        im = IMSICModel()
        ap = APLICModel()
        for i, e in enumerate(self.events):
            op = str(e.get("op", "")).lower()
            if op == "imsic_config":
                im.configure(e.get("eidelivery"), e.get("eithreshold"),
                             e.get("eie"), e.get("eip"))
                self.metrics["aia_active"] = True
            elif op == "imsic_topei":
                self._check_topei(i, e, im)
            elif op == "aplic_config":
                ap.configure(e.get("idelivery"), e.get("ithreshold"),
                             e.get("setip"), e.get("setie"), e.get("iprio"),
                             e.get("sourcecfg"))
                self.metrics["aia_active"] = True
            elif op == "aplic_topi":
                self._check_topi(i, e, ap)
        return self._report(started)

    def _check_topi(self, i: int, e: Dict[str, Any], ap: APLICModel) -> None:
        self.metrics["topi_queries"] += 1
        self.metrics["aia_active"] = True
        golden = ap.topi()
        dut = _to_int(e.get("result", e.get("topi")))
        if dut is None:
            return
        if dut != golden:
            self._v(i, "aplic_topi",
                    f"topi={dut}, golden highest-priority (lowest iprio) is {golden}")
        if dut != 0:
            if not ap.idelivery:
                self._v(i, "aplic_delivery",
                        f"topi={dut} while idelivery disabled (must be 0)")
            if not ap.active(dut) or dut not in ap.setip or dut not in ap.setie:
                self._v(i, "aplic_inactive",
                        f"topi source {dut} is inactive/delegated or not pending+enabled")
            elif ap.ithreshold and ap.prio(dut) >= ap.ithreshold:
                self._v(i, "aplic_threshold",
                        f"topi source {dut} iprio {ap.prio(dut)} ≥ ithreshold "
                        f"{ap.ithreshold} (masked)")

    def _check_topei(self, i: int, e: Dict[str, Any], im: IMSICModel) -> None:
        self.metrics["topei_queries"] += 1
        self.metrics["aia_active"] = True
        golden = im.topei()
        dut = _to_int(e.get("result", e.get("topei")))
        if dut is None:
            return
        if dut != golden:
            self._v(i, "imsic_topei",
                    f"topei={dut}, golden highest-priority (lowest id) is {golden}")
        if dut != 0:
            if not im.eidelivery:
                self._v(i, "imsic_delivery",
                        f"topei={dut} while eidelivery disabled (must be 0)")
            if not (dut in im.eip and dut in im.eie):
                self._v(i, "imsic_disabled",
                        f"topei identity {dut} is not both pending and enabled")
            elif im.eithreshold and dut >= im.eithreshold:
                self._v(i, "imsic_threshold",
                        f"topei identity {dut} ≥ eithreshold {im.eithreshold} (masked)")

    def _report(self, started: str) -> Dict[str, Any]:
        total = len(self.violations)
        return {
            "schema_version": SCHEMA_VERSION,
            "agent": AGENT_NAME,
            "started_at": started,
            "finished_at": _now(),
            "records_checked": len(self.events),
            "aia_active": self.metrics["aia_active"],
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
    name = (manifest.get("outputs", {}) or {}).get("aia_trace", "aia_trace.jsonl")
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
        log.warning("aia_verifier: cannot read manifest: %s", exc)
        return 0
    run_dir = Path(manifest.get("run_dir", mp.parent))
    events = _load_events(run_dir, manifest)
    if not events:
        rep = {"schema_version": SCHEMA_VERSION, "agent": AGENT_NAME,
               "status": "skipped", "reason": "no aia_trace", "pass": True}
    else:
        rep = AIAVerifier(events).run()
        rep["status"] = "completed"
    try:
        (run_dir / "aia_report.json").write_text(
            json.dumps(rep, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("aia_verifier: cannot write report: %s", exc)
    return 0 if rep.get("pass", True) else 1


if __name__ == "__main__":  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="AVA AIA/IMSIC checker")
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args()
    raise SystemExit(run_from_manifest(args.manifest))
