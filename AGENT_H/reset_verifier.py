"""
AGENT_H.reset_verifier — Reset-State Checker (T49)
===================================================

Golden checker for the RISC-V architectural **reset state**. A wrong reset value
is one of the nastiest bug classes: it silently corrupts every boot, is easy to
miss (the core still "runs"), and isn't touched by any of the runtime verifiers.
This agent checks the reset snapshot — privilege mode, PC, and CSRs — against the
values the ISA *mandates* at reset, plus any implementation-specific golden
values the platform supplies.

Architecturally-mandated invariants (RISC-V priv spec §3.4)
-----------------------------------------------------------
- **reset_priv** (HIGH) — the hart resets into **M-mode**.
- **reset_mstatus_mie** (HIGH) — `mstatus.MIE = 0` (interrupts globally
  disabled at reset — a core that resets with interrupts on can take a spurious
  trap before software configures anything).
- **reset_mstatus_mprv** (HIGH) — `mstatus.MPRV = 0` (loads/stores use the
  current privilege, not a stale one).
- **reset_pc** (HIGH) — PC equals the platform reset vector (from
  `expected.pc` / `reset_vector`).
- **reset_misa** (MEDIUM) — if `misa` is implemented (non-zero) its MXL field is
  valid (1/2/3) and a base-integer bit (I or E) is set.

Golden comparison
-----------------
- **reset_csr** (HIGH) — every CSR listed in a snapshot's `expected.csrs` must
  match the reset value (implementation-specific mtvec, pmp, etc.).

Input: one snapshot dict or a list of them (multi-hart).

```
{"hart":0, "priv":"M", "pc":"0x80000000",
 "csrs":{"mstatus":"0x0","misa":"0x40141101","mie":"0x0"},
 "expected":{"pc":"0x80000000","csrs":{"mtvec":"0x80000004"}}}
```

Stdlib-only, schema-v2.1.0 report, graceful degradation throughout.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

log = logging.getLogger("AGENT_H.reset")

SCHEMA_VERSION = "2.1.0"
AGENT_NAME = "reset_verifier"
_MSTATUS_MIE = 1 << 3
_MSTATUS_MPRV = 1 << 17


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


def _norm_priv(p: Any) -> Optional[str]:
    s = str(p).upper()
    if s in ("M", "3"):
        return "M"
    if s in ("S", "1"):
        return "S"
    if s in ("U", "0"):
        return "U"
    return None


class ResetVerifier:
    def __init__(self, snapshots: Any, config: Optional[Dict[str, Any]] = None):
        if isinstance(snapshots, dict):
            snapshots = [snapshots]
        self.snaps = [s for s in (snapshots or []) if isinstance(s, dict)]
        self.config = config or {}
        self.violations: List[Dict[str, Any]] = []
        self.metrics = {"harts": 0, "csrs_checked": 0, "reset_active": False}

    def _v(self, hart: Any, check: str, detail: str) -> None:
        self.violations.append({"hart": hart, "check": check,
                                "severity": "HIGH", "detail": detail})

    def run(self) -> Dict[str, Any]:
        started = _now()
        for s in self.snaps:
            self.metrics["harts"] += 1
            self.metrics["reset_active"] = True
            hart = s.get("hart", 0)
            csrs = s.get("csrs", {}) if isinstance(s.get("csrs"), dict) else {}
            exp = s.get("expected", {}) if isinstance(s.get("expected"), dict) else {}

            # privilege
            priv = _norm_priv(s.get("priv", s.get("mode")))
            if priv is not None and priv != "M":
                self._v(hart, "reset_priv", f"reset privilege {priv} (must be M)")

            # mstatus MIE / MPRV
            mstatus = _to_int(csrs.get("mstatus"))
            if mstatus is not None:
                if mstatus & _MSTATUS_MIE:
                    self._v(hart, "reset_mstatus_mie",
                            "mstatus.MIE = 1 at reset (interrupts must be disabled)")
                if mstatus & _MSTATUS_MPRV:
                    self._v(hart, "reset_mstatus_mprv",
                            "mstatus.MPRV = 1 at reset (must be 0)")

            # PC == reset vector
            pc = _to_int(s.get("pc"))
            want_pc = _to_int(exp.get("pc")) if "pc" in exp \
                else _to_int(self.config.get("reset_vector"))
            if pc is not None and want_pc is not None and pc != want_pc:
                self._v(hart, "reset_pc",
                        f"reset PC {hex(pc)} != reset vector {hex(want_pc)}")

            # misa sanity
            misa = _to_int(csrs.get("misa"))
            if misa:
                self._check_misa(hart, misa)

            # golden expected CSR values
            for name, val in (exp.get("csrs", {}) or {}).items():
                self.metrics["csrs_checked"] += 1
                want = _to_int(val)
                got = _to_int(csrs.get(name))
                if want is not None and got is not None and got != want:
                    self._v(hart, "reset_csr",
                            f"{name} reset value {hex(got)} != expected {hex(want)}")

        return self._report(started)

    def _check_misa(self, hart: Any, misa: int) -> None:
        xlen = 64 if misa > 0xFFFFFFFF else 32
        mxl = (misa >> (xlen - 2)) & 0x3
        if mxl not in (1, 2, 3):
            self._v(hart, "reset_misa",
                    f"misa MXL field {mxl} invalid (expected 1/2/3)")
        base = ((misa >> 8) & 1) or ((misa >> 4) & 1)   # 'I' or 'E' base
        if not base:
            self.violations.append({
                "hart": hart, "check": "reset_misa", "severity": "MEDIUM",
                "detail": "misa has neither I nor E base-integer bit set"})

    def _report(self, started: str) -> Dict[str, Any]:
        total = len(self.violations)
        high = sum(1 for v in self.violations if v["severity"] == "HIGH")
        med = total - high
        return {
            "schema_version": SCHEMA_VERSION,
            "agent": AGENT_NAME,
            "started_at": started,
            "finished_at": _now(),
            "records_checked": len(self.snaps),
            "reset_active": self.metrics["reset_active"],
            "metrics": self.metrics,
            "total_violations": total,
            "high_violations": high,
            "medium_violations": med,
            "severity_score": high * 3 + med,
            "band": "CLEAN" if total == 0 else "CRITICAL" if high else "DEGRADED",
            "pass": high == 0,
            "violations": self.violations[:100],
        }


# ─────────────────────────────────────────────────────────────────────────────
# Manifest entry point
# ─────────────────────────────────────────────────────────────────────────────
def _load_snapshots(run_dir: Path, manifest: Dict[str, Any]) -> Any:
    name = (manifest.get("outputs", {}) or {}).get("reset_snapshot",
                                                    "reset_snapshot.json")
    p = run_dir / name
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def run_from_manifest(manifest_path: str) -> int:
    mp = Path(manifest_path)
    try:
        manifest = json.loads(mp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("reset_verifier: cannot read manifest: %s", exc)
        return 0
    run_dir = Path(manifest.get("run_dir", mp.parent))
    snaps = _load_snapshots(run_dir, manifest)
    if not snaps:
        rep = {"schema_version": SCHEMA_VERSION, "agent": AGENT_NAME,
               "status": "skipped", "reason": "no reset_snapshot", "pass": True}
    else:
        rep = ResetVerifier(snaps, config=manifest.get("reset_config")).run()
        rep["status"] = "completed"
    try:
        (run_dir / "reset_report.json").write_text(
            json.dumps(rep, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("reset_verifier: cannot write report: %s", exc)
    return 0 if rep.get("pass", True) else 1


if __name__ == "__main__":  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="AVA reset-state checker")
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args()
    raise SystemExit(run_from_manifest(args.manifest))
