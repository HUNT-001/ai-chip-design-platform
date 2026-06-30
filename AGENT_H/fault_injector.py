"""
AGENT_H/fault_injector.py
=========================
T35 — Fault-Injection Campaign Engine

A meta-verification agent: instead of verifying a DUT, it verifies the
**verification suite itself**.  It takes a known-good commit log, injects
hardware fault models (bit-flips, stuck-at, register / memory / PC corruption),
re-runs a panel of AVA detector agents, and measures what fraction of the
injected faults the panel actually catches.

This is mutation testing applied to the verification environment — the
"who-watches-the-watchers" check.  The output is a quantitative **fault
coverage / detection rate** plus, crucially, the list of **undetected faults**:
each one is a concrete blind spot in the current verification panel.

Fault models
------------
  bit_flip              flip one bit of a register / memory / PC value
  stuck_at_0            force one bit of a value to 0
  stuck_at_1            force one bit of a value to 1
  register_corruption   replace a committed register value with a wrong one
  memory_corruption     corrupt a memory read/write value
  pc_corruption         corrupt a program-counter value

Usage
-----
  from AGENT_H.fault_injector import FaultCampaign, inject_fault
  report = FaultCampaign(golden_log, seed=1).run(n=100)
  print(report["detection_rate"], report["undetected"])

  from AGENT_H.fault_injector import run_from_manifest
  run_from_manifest(Path(run_dir) / "run_manifest.json")
"""

from __future__ import annotations

import copy
import json
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "2.1.0"
_M32 = 0xFFFFFFFF

# fault model names
BIT_FLIP            = "bit_flip"
STUCK_AT_0          = "stuck_at_0"
STUCK_AT_1          = "stuck_at_1"
REGISTER_CORRUPTION = "register_corruption"
MEMORY_CORRUPTION   = "memory_corruption"
PC_CORRUPTION       = "pc_corruption"

ALL_MODELS = [BIT_FLIP, STUCK_AT_0, STUCK_AT_1,
              REGISTER_CORRUPTION, MEMORY_CORRUPTION, PC_CORRUPTION]

_REG_MODELS = {BIT_FLIP, STUCK_AT_0, STUCK_AT_1, REGISTER_CORRUPTION}


def _to_int(value: Any) -> Optional[int]:
    if value is None:
        return None
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


def _hex(v: int) -> str:
    return f"0x{v & _M32:08x}"


# ─────────────────────────────────────────────────────────
# Fault descriptor
# ─────────────────────────────────────────────────────────

@dataclass
class Fault:
    model:  str
    seq:    int                 # index into the log
    target: str                 # "reg" | "mem" | "pc"
    reg:    Optional[str] = None
    mem_kind: Optional[str] = None    # "mem_reads" | "mem_writes"
    mem_idx:  Optional[int] = None
    bit:    Optional[int] = None
    old:    Optional[int] = None
    new:    Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model": self.model, "seq": self.seq, "target": self.target,
            "reg": self.reg, "mem_kind": self.mem_kind, "mem_idx": self.mem_idx,
            "bit": self.bit,
            "old": _hex(self.old) if self.old is not None else None,
            "new": _hex(self.new) if self.new is not None else None,
        }


def _mutate_value(model: str, value: int, bit: int, rng: random.Random) -> int:
    if model == BIT_FLIP:
        return value ^ (1 << bit)
    if model == STUCK_AT_0:
        return value & ~(1 << bit)
    if model == STUCK_AT_1:
        return value | (1 << bit)
    if model in (REGISTER_CORRUPTION, MEMORY_CORRUPTION, PC_CORRUPTION):
        new = value
        while new == value:
            new = rng.randint(0, _M32)
        return new
    return value


# ─────────────────────────────────────────────────────────
# Injection
# ─────────────────────────────────────────────────────────

def inject_fault(log: List[Dict[str, Any]], fault: Fault) -> List[Dict[str, Any]]:
    """Return a deep copy of ``log`` with ``fault`` applied (and fault.old/new set)."""
    out = copy.deepcopy(log)
    if not (0 <= fault.seq < len(out)) or not isinstance(out[fault.seq], dict):
        return out
    rec = out[fault.seq]
    bit = fault.bit if fault.bit is not None else 0

    if fault.target == "reg" and fault.reg:
        regs = rec.setdefault("regs", {})
        old = _to_int(regs.get(fault.reg)) or 0
        new = _mutate_value(fault.model, old, bit, random.Random(old ^ bit ^ fault.seq))
        fault.old, fault.new = old, new
        regs[fault.reg] = _hex(new)
    elif fault.target == "mem" and fault.mem_kind and fault.mem_idx is not None:
        entries = rec.get(fault.mem_kind) or []
        if 0 <= fault.mem_idx < len(entries) and isinstance(entries[fault.mem_idx], dict):
            old = _to_int(entries[fault.mem_idx].get("value")) or 0
            new = _mutate_value(fault.model, old, bit, random.Random(old ^ bit))
            fault.old, fault.new = old, new
            entries[fault.mem_idx]["value"] = _hex(new)
    elif fault.target == "pc":
        old = _to_int(rec.get("pc")) or 0
        new = _mutate_value(fault.model, old, bit, random.Random(old ^ bit))
        fault.old, fault.new = old, new
        rec["pc"] = _hex(new)
    return out


# ─────────────────────────────────────────────────────────
# Default detector panel
# ─────────────────────────────────────────────────────────

def default_detectors() -> List[Callable[[List[Dict]], bool]]:
    """A panel of AVA agents; each returns True if it flags the (faulted) log."""
    panel: List[Callable[[List[Dict]], bool]] = []

    def _pipeline(log):
        from .pipeline_verifier import PipelineVerifier
        return not PipelineVerifier(log).run()["pass"]

    def _csr(log):
        from .csr_verifier import CSRVerifier
        return not CSRVerifier(log).run()["pass"]

    def _atomics(log):
        from .atomics_verifier import AtomicsVerifier
        return not AtomicsVerifier(log).run()["pass"]

    for d in (_pipeline, _csr, _atomics):
        panel.append(d)
    return panel


def _detected(log: List[Dict], detectors: List[Callable]) -> bool:
    for d in detectors:
        try:
            if d(log):
                return True
        except Exception as exc:           # a detector must never crash the campaign
            logger.warning("fault_injector: detector raised: %s", exc)
    return False


# ─────────────────────────────────────────────────────────
# Campaign
# ─────────────────────────────────────────────────────────

class FaultCampaign:
    """
    Inject random faults into a golden log and measure detection by the panel.

    Parameters
    ----------
    golden_log : a known-good commit log (the panel should pass on it as-is)
    detectors  : list of callables log->bool (default: AVA agent panel)
    models     : fault models to sample (default: ALL_MODELS)
    seed       : RNG seed for a reproducible campaign
    """

    def __init__(
        self,
        golden_log: List[Dict[str, Any]],
        detectors:  Optional[List[Callable]] = None,
        models:     Optional[List[str]] = None,
        seed:       int = 0,
    ) -> None:
        self.golden_log = golden_log or []
        self.detectors  = detectors if detectors is not None else default_detectors()
        self.models     = models or list(ALL_MODELS)
        self.rng        = random.Random(seed)
        self.seed       = seed

    # -- random fault construction -------------------------------------------

    def _random_fault(self) -> Optional[Fault]:
        n = len(self.golden_log)
        if n == 0:
            return None
        model = self.rng.choice(self.models)
        bit = self.rng.randint(0, 31)

        if model in _REG_MODELS or model == REGISTER_CORRUPTION:
            cands = [(i, r) for i, r in enumerate(self.golden_log)
                     if isinstance(r, dict) and (r.get("regs") or {})]
            cands = [(i, r) for i, r in cands
                     if any(k != "x0" for k in r["regs"])]
            if not cands:
                return None
            i, r = self.rng.choice(cands)
            reg = self.rng.choice([k for k in r["regs"] if k != "x0"])
            return Fault(model, i, "reg", reg=reg, bit=bit)

        if model == MEMORY_CORRUPTION:
            cands = []
            for i, r in enumerate(self.golden_log):
                if not isinstance(r, dict):
                    continue
                for kind in ("mem_reads", "mem_writes"):
                    for j, e in enumerate(r.get(kind) or []):
                        if isinstance(e, dict) and e.get("value") is not None:
                            cands.append((i, kind, j))
            if not cands:
                return None
            i, kind, j = self.rng.choice(cands)
            return Fault(model, i, "mem", mem_kind=kind, mem_idx=j, bit=bit)

        if model == PC_CORRUPTION:
            cands = [i for i, r in enumerate(self.golden_log)
                     if isinstance(r, dict) and r.get("pc") is not None]
            if not cands:
                return None
            return Fault(model, self.rng.choice(cands), "pc", bit=bit)
        return None

    # -- run ------------------------------------------------------------------

    def run(self, n: int = 50) -> Dict[str, Any]:
        started = datetime.now(timezone.utc)
        per_model: Dict[str, Dict[str, int]] = {
            m: {"injected": 0, "detected": 0} for m in self.models
        }
        injected = 0
        detected = 0
        undetected: List[Dict[str, Any]] = []

        for _ in range(max(0, n)):
            fault = self._random_fault()
            if fault is None:
                continue
            faulted = inject_fault(self.golden_log, fault)
            if fault.old is not None and fault.new == fault.old:
                continue                       # no-op mutation, skip
            injected += 1
            per_model[fault.model]["injected"] += 1
            if _detected(faulted, self.detectors):
                detected += 1
                per_model[fault.model]["detected"] += 1
            else:
                if len(undetected) < 50:
                    undetected.append(fault.to_dict())

        finished = datetime.now(timezone.utc)
        rate = round(detected / injected, 4) if injected else None
        for m in per_model:
            inj = per_model[m]["injected"]
            per_model[m]["rate"] = round(per_model[m]["detected"] / inj, 4) if inj else None

        return {
            "schema_version":  SCHEMA_VERSION,
            "agent":           "fault_injector",
            "golden_records":  len(self.golden_log),
            "seed":            self.seed,
            "faults_injected": injected,
            "faults_detected": detected,
            "detection_rate":  rate,
            "fault_coverage":  rate,
            "per_model":       per_model,
            "undetected":      undetected,
            "band":            _coverage_band(rate),
            "pass":            True,           # a measurement, not a DUT pass/fail
            "started_at":      started.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "finished_at":     finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duration_s":      round((finished - started).total_seconds(), 3),
        }


def _coverage_band(rate: Optional[float]) -> str:
    if rate is None:
        return "CLEAN"
    if rate >= 0.9:
        return "VERIFIED"
    if rate >= 0.7:
        return "HIGH"
    if rate >= 0.5:
        return "MEDIUM"
    if rate >= 0.3:
        return "LOW"
    return "CRITICAL"


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


def run_from_manifest(manifest_path: Path, n: int = 30) -> int:
    """
    Pipeline entry point.  Runs a small fault-injection campaign against the
    run's own RTL commit log to measure the detection coverage of the agent
    panel, writing ``fault_report.json``.  Always returns 0 (it is a
    measurement, not a DUT verdict).
    """
    manifest_path = Path(manifest_path)
    try:
        with open(manifest_path) as f:
            manifest = json.load(f)
    except Exception as exc:
        logger.warning("fault_injector: cannot read manifest: %s", exc)
        return 0

    run_dir = Path(manifest.get("run_dir", manifest_path.parent))
    outputs = manifest.get("outputs", {})
    rtl_log = _load_log(run_dir, outputs, "rtl_commit_log", "rtl_commit.jsonl")
    if not rtl_log:
        logger.info("fault_injector: no RTL commit log, skipping")
        return 0

    report = FaultCampaign(rtl_log, seed=1).run(n=n)

    report_path = run_dir / "fault_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
        f.write("\n")

    manifest.setdefault("outputs", {})["fault_report"] = "fault_report.json"
    manifest.setdefault("phases", {})["fault_injection"] = {
        "detection_rate": report["detection_rate"],
        "band": report["band"],
    }
    tmp = manifest_path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    tmp.rename(manifest_path)

    logger.info("fault_injector: %d faults, detection_rate=%s, band=%s",
                report["faults_injected"], report["detection_rate"], report["band"])
    return 0


# ─────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":          # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description="Fault-injection campaign engine")
    ap.add_argument("--manifest", type=Path)
    ap.add_argument("--rtl", type=Path)
    ap.add_argument("-n", type=int, default=50)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.manifest:
        raise SystemExit(run_from_manifest(args.manifest, args.n))
    if args.rtl:
        log = []
        with open(args.rtl) as fh:
            for ln in fh:
                ln = ln.strip()
                if ln:
                    log.append(json.loads(ln))
        rep = FaultCampaign(log, seed=args.seed).run(n=args.n)
        print(json.dumps(rep, indent=2))
        raise SystemExit(0)
    ap.print_help()
