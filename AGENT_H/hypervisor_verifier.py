"""
AGENT_H.hypervisor_verifier — Hypervisor Two-Stage Translation (T50)
=====================================================================

Golden checker for the RISC-V Hypervisor (H) extension's defining feature:
**two-stage address translation**. A guest virtual address is translated by the
**VS-stage** (guest page tables, `vsatp`) to a guest physical address, which the
**G-stage** (hypervisor page tables, `hgatp`) then translates to a supervisor/
host physical address.

What this models (and what it deliberately doesn't)
---------------------------------------------------
The novel, bug-prone part of the H-extension is the **composition** of the two
stages and its **fault semantics**, which are unlike anything in a non-virtual
core:

- a fault in the **VS-stage** is an *ordinary* page fault
  (instruction/load/store = **12 / 13 / 15**),
- a fault in the **G-stage** is a distinct *guest*-page fault
  (**20 / 21 / 23**),

and per-stage permissions are checked independently. This agent verifies exactly
that composition + fault classification. The multi-level page-table *walk* of a
single stage is already covered by `vm_verifier` / `sv_mmu_verifier`, so here
each stage is supplied as a resolved VPN→(GPN,perms) / GPN→(PPN,perms) mapping
(the walk's *output*), keeping the checker focused and its tests tractable.

Checks
------
- **htrans_result** (HIGH) — the composed supervisor PA is wrong.
- **htrans_fault** (HIGH) — a fault is missing, spurious, or reports the wrong
  cause / wrong stage.

Additive `hypervisor_trace.jsonl` contract
------------------------------------------
```
{"op":"config",
 "vs_map":{"0x80":{"gpn":"0x120","r":true,"w":true,"x":false,"v":true}},
 "g_map": {"0x120":{"ppn":"0x300","r":true,"w":true,"x":false,"v":true}}}
{"op":"translate","gva":"0x80abc","access":"load","pa":"0x300abc","fault":null}
```

Stdlib-only, schema-v2.1.0 report, graceful degradation throughout.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

log = logging.getLogger("AGENT_H.hypervisor")

SCHEMA_VERSION = "2.1.0"
AGENT_NAME = "hypervisor_verifier"
# access → (VS-stage page-fault cause, G-stage guest-page-fault cause)
_FAULT = {"exec": (12, 20), "load": (13, 21), "store": (15, 23)}
_PAGE_OFFSET = 0xFFF


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


def _norm_access(a: Any) -> str:
    s = str(a).lower()
    if s in ("exec", "fetch", "instruction", "x"):
        return "exec"
    if s in ("store", "write", "w", "amo"):
        return "store"
    return "load"


def _norm_map(raw: Any) -> Dict[int, Dict[str, Any]]:
    out: Dict[int, Dict[str, Any]] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            ki = _to_int(k)
            if ki is not None and isinstance(v, dict):
                out[ki] = v
    return out


def _perm_ok(pte: Dict[str, Any], access: str) -> bool:
    if access == "exec":
        return bool(pte.get("x", False))
    if access == "store":
        return bool(pte.get("w", False))
    return bool(pte.get("r", False))         # load


class TwoStageMMU:
    def __init__(self, vs_map: Dict[int, Dict[str, Any]],
                 g_map: Dict[int, Dict[str, Any]]):
        self.vs = vs_map
        self.g = g_map

    def translate(self, gva: int, access: str) -> Tuple[Optional[int], Optional[int]]:
        """Return (supervisor_pa, fault_cause). fault_cause None ⇒ success."""
        vs_cause, g_cause = _FAULT[access]
        off = gva & _PAGE_OFFSET
        vpn = gva >> 12

        # -- VS-stage: GVA → GPA (ordinary page fault on failure) --
        vs = self.vs.get(vpn)
        if not vs or not vs.get("v", True):
            return None, vs_cause
        if not _perm_ok(vs, access):
            return None, vs_cause
        gpn = _to_int(vs.get("gpn"))
        if gpn is None:
            return None, vs_cause

        # -- G-stage: GPA → SPA (guest-page fault on failure) --
        g = self.g.get(gpn)
        if not g or not g.get("v", True):
            return None, g_cause
        if not _perm_ok(g, access):
            return None, g_cause
        ppn = _to_int(g.get("ppn"))
        if ppn is None:
            return None, g_cause
        return (ppn << 12) | off, None


class HypervisorVerifier:
    def __init__(self, events: Sequence[Dict[str, Any]]):
        self.events = [e for e in (events or []) if isinstance(e, dict)]
        self.violations: List[Dict[str, Any]] = []
        self.metrics = {"translations": 0, "faults": 0, "hyp_active": False}

    def _v(self, i: int, check: str, detail: str) -> None:
        self.violations.append({"event": i, "check": check,
                                "severity": "HIGH", "detail": detail})

    def run(self) -> Dict[str, Any]:
        started = _now()
        vs_map: Dict[int, Dict[str, Any]] = {}
        g_map: Dict[int, Dict[str, Any]] = {}
        for i, e in enumerate(self.events):
            op = str(e.get("op", "")).lower()
            if op == "config":
                vs_map = _norm_map(e.get("vs_map"))
                g_map = _norm_map(e.get("g_map"))
                self.metrics["hyp_active"] = True
            elif op == "translate":
                self._check_translate(i, e, vs_map, g_map)
        return self._report(started)

    def _check_translate(self, i: int, e: Dict[str, Any],
                         vs_map, g_map) -> None:
        self.metrics["translations"] += 1
        self.metrics["hyp_active"] = True
        gva = _to_int(e.get("gva"))
        if gva is None:
            return
        access = _norm_access(e.get("access"))
        # allow per-request maps to override the config
        vm = _norm_map(e.get("vs_map")) or vs_map
        gm = _norm_map(e.get("g_map")) or g_map
        golden_pa, golden_cause = TwoStageMMU(vm, gm).translate(gva, access)

        dut_pa = _to_int(e.get("pa"))
        dut_cause = _to_int(e.get("fault"))
        if golden_cause is not None:
            self.metrics["faults"] += 1
            if dut_cause is None:
                self._v(i, "htrans_fault",
                        f"gva {hex(gva)} ({access}) should fault with cause "
                        f"{golden_cause} but DUT translated")
            elif dut_cause != golden_cause:
                self._v(i, "htrans_fault",
                        f"gva {hex(gva)} ({access}) fault cause {dut_cause} "
                        f"!= golden {golden_cause}")
        else:
            if dut_cause is not None:
                self._v(i, "htrans_fault",
                        f"gva {hex(gva)} ({access}) spurious fault cause {dut_cause} "
                        f"(golden translates to {hex(golden_pa)})")
            elif dut_pa is not None and golden_pa is not None and dut_pa != golden_pa:
                self._v(i, "htrans_result",
                        f"gva {hex(gva)} → {hex(dut_pa)} != golden {hex(golden_pa)}")

    def _report(self, started: str) -> Dict[str, Any]:
        total = len(self.violations)
        return {
            "schema_version": SCHEMA_VERSION,
            "agent": AGENT_NAME,
            "started_at": started,
            "finished_at": _now(),
            "records_checked": len(self.events),
            "hypervisor_active": self.metrics["hyp_active"],
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
    name = (manifest.get("outputs", {}) or {}).get("hypervisor_trace",
                                                    "hypervisor_trace.jsonl")
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
        log.warning("hypervisor_verifier: cannot read manifest: %s", exc)
        return 0
    run_dir = Path(manifest.get("run_dir", mp.parent))
    events = _load_events(run_dir, manifest)
    if not events:
        rep = {"schema_version": SCHEMA_VERSION, "agent": AGENT_NAME,
               "status": "skipped", "reason": "no hypervisor_trace", "pass": True}
    else:
        rep = HypervisorVerifier(events).run()
        rep["status"] = "completed"
    try:
        (run_dir / "hypervisor_report.json").write_text(
            json.dumps(rep, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("hypervisor_verifier: cannot write report: %s", exc)
    return 0 if rep.get("pass", True) else 1


if __name__ == "__main__":  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="AVA hypervisor two-stage translation checker")
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args()
    raise SystemExit(run_from_manifest(args.manifest))
