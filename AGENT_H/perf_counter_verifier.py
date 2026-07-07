"""
AGENT_H.perf_counter_verifier — Performance-Counter Checker (T47)
==================================================================

Golden checker for the RISC-V hardware performance counters (`mcycle`,
`minstret`, `mcountinhibit`) driven from the *standard* commit log — it reads the
`perf_counters` field every record already carries, so it needs no separate
trace and runs in the normal pipeline.

Catches a real, easily-missed bug class: a `minstret` that skips or double-counts
retirements, an `mcycle` that runs backwards, or a counter that keeps ticking
while software has inhibited it via `mcountinhibit`.

Checks (all conservatively gated on `perf_counters` presence)
-------------------------------------------------------------
- **perf_instret_increment** (HIGH) — `minstret` must increase by **exactly 1**
  per retired instruction; **0** when the IR bit of `mcountinhibit` is set
  (frozen); a record that traps (does not retire) is allowed 0 or 1.
- **perf_cycle_monotonic** (HIGH) — `mcycle` is **non-decreasing**
  (superscalar retire ⇒ delta may be 0), and **exactly 0** when the CY bit of
  `mcountinhibit` is set.

Metrics (never fail): total cycles / instret spanned, IPC, CPI, records checked.

`mcountinhibit` bits: `[0]=CY` (mcycle), `[2]=IR` (minstret).

Stdlib-only, schema-v2.1.0 report, graceful degradation throughout.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

log = logging.getLogger("AGENT_H.perf")

SCHEMA_VERSION = "2.1.0"
AGENT_NAME = "perf_counter_verifier"
_INHIBIT_CY = 0x1
_INHIBIT_IR = 0x4


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


def _cycles(pc: Dict[str, Any]) -> Optional[int]:
    for k in ("cycles", "mcycle", "cycle"):
        if k in pc:
            return _to_int(pc[k])
    return None


def _instret(pc: Dict[str, Any]) -> Optional[int]:
    for k in ("instret", "minstret", "instructions"):
        if k in pc:
            return _to_int(pc[k])
    return None


class PerfCounterVerifier:
    def __init__(self, rtl_log: Sequence[Dict[str, Any]],
                 iss_log: Optional[Sequence[Dict[str, Any]]] = None):
        self.rtl = list(rtl_log or [])
        self.violations: List[Dict[str, Any]] = []
        self.metrics = {"records_with_perf": 0, "first_cycle": None,
                        "last_cycle": None, "first_instret": None,
                        "last_instret": None}

    def _v(self, seq: int, check: str, detail: str) -> None:
        self.violations.append({"seq": seq, "check": check,
                                "severity": "HIGH", "detail": detail})

    @staticmethod
    def _inhibit(rec: Dict[str, Any]) -> int:
        csrs = rec.get("csrs", {})
        if isinstance(csrs, dict):
            return _to_int(csrs.get("mcountinhibit")) or 0
        return 0

    def run(self) -> Dict[str, Any]:
        started = _now()
        prev_c: Optional[int] = None
        prev_i: Optional[int] = None
        for seq, rec in enumerate(self.rtl):
            if not isinstance(rec, dict):
                continue
            pc = rec.get("perf_counters")
            if not isinstance(pc, dict):
                continue
            self.metrics["records_with_perf"] += 1
            cyc, ins = _cycles(pc), _instret(pc)
            inh = self._inhibit(rec)
            trapped = isinstance(rec.get("trap"), dict) and \
                rec["trap"].get("cause") is not None

            if cyc is not None:
                if self.metrics["first_cycle"] is None:
                    self.metrics["first_cycle"] = cyc
                self.metrics["last_cycle"] = cyc
            if ins is not None:
                if self.metrics["first_instret"] is None:
                    self.metrics["first_instret"] = ins
                self.metrics["last_instret"] = ins

            # -- minstret --
            if ins is not None and prev_i is not None:
                d = ins - prev_i
                if inh & _INHIBIT_IR:
                    if d != 0:
                        self._v(seq, "perf_instret_increment",
                                f"minstret moved by {d} while IR-inhibited (must be 0)")
                elif not trapped and d != 1:
                    self._v(seq, "perf_instret_increment",
                            f"minstret delta {d} != 1 for a retired instruction")
                elif trapped and d not in (0, 1):
                    self._v(seq, "perf_instret_increment",
                            f"minstret delta {d} invalid across a trap (0 or 1)")

            # -- mcycle --
            if cyc is not None and prev_c is not None:
                dc = cyc - prev_c
                if inh & _INHIBIT_CY:
                    if dc != 0:
                        self._v(seq, "perf_cycle_monotonic",
                                f"mcycle moved by {dc} while CY-inhibited (must be 0)")
                elif dc < 0:
                    self._v(seq, "perf_cycle_monotonic",
                            f"mcycle went backwards by {-dc} ({prev_c}→{cyc})")

            prev_c = cyc if cyc is not None else prev_c
            prev_i = ins if ins is not None else prev_i

        return self._report(started)

    def _report(self, started: str) -> Dict[str, Any]:
        m = self.metrics
        span_c = (m["last_cycle"] - m["first_cycle"]) \
            if m["first_cycle"] is not None and m["last_cycle"] is not None else 0
        span_i = (m["last_instret"] - m["first_instret"]) \
            if m["first_instret"] is not None and m["last_instret"] is not None else 0
        m["cycles_spanned"] = span_c
        m["instret_spanned"] = span_i
        m["ipc"] = round(span_i / span_c, 4) if span_c > 0 else None
        m["cpi"] = round(span_c / span_i, 4) if span_i > 0 else None
        total = len(self.violations)
        return {
            "schema_version": SCHEMA_VERSION,
            "agent": AGENT_NAME,
            "started_at": started,
            "finished_at": _now(),
            "records_checked": len(self.rtl),
            "perf_active": m["records_with_perf"] > 0,
            "metrics": m,
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
        log.warning("perf_counter_verifier: cannot read manifest: %s", exc)
        return 0
    run_dir = Path(manifest.get("run_dir", mp.parent))
    outputs = manifest.get("outputs", {})
    rtl = _load_jsonl(run_dir / outputs.get("rtl_commit_log", "rtl_commit.jsonl"))
    rep = PerfCounterVerifier(rtl).run()
    try:
        (run_dir / "perf_counter_report.json").write_text(
            json.dumps(rep, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("perf_counter_verifier: cannot write report: %s", exc)
    return 0 if rep["pass"] else 1


if __name__ == "__main__":  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="AVA performance-counter checker")
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args()
    raise SystemExit(run_from_manifest(args.manifest))
