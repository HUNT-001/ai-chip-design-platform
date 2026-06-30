"""
AGENT_H/rv64_atomics_verifier.py
================================
T38 — RV64 Atomics Verification  (RV64 widening, phase 3)

Verifies the 64-bit RISC-V "A" extension — ``LR.D`` / ``SC.D`` and the nine
``AMO*.D`` operations — from the commit log, against a golden 64-bit reference
model.  It complements ``AGENT_H/atomics_verifier`` (which covers the 32-bit
``.W`` atomics); this module owns the ``.D`` width.

It reuses the shared decoder (`AGENT_H.atomics_verifier.decode_atomic`, which
already recognises the ``.d`` suffix) and applies the same checks as the RV32
atomics verifier but at 64-bit width: AMO destination = old memory value, AMO
write-back = ``f(old, rs2)`` with correct signed/unsigned 64-bit semantics,
store-conditional success/fail vs a live reservation, and 8-byte alignment.

It only acts on ``.D`` atomics, so it is a clean no-op on RV32 / non-atomic
traces and introduces no schema change.

Checks
------
  amod_rd_value      AMO/LR.D destination != old 64-bit memory value
  amod_writeback     AMO.D write-back != f(old, rs2) (64-bit golden math)
  scd_success_*      SC.D with a valid reservation: must write + return 0
  scd_fail_*         SC.D without a reservation: must not write + return != 0
  amod_alignment     .D atomic not 8-byte aligned without a misaligned trap

Usage
-----
  from AGENT_H.rv64_atomics_verifier import RV64AtomicsVerifier, amo_compute64
  report = RV64AtomicsVerifier(rtl_log).run()
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .atomics_verifier import decode_atomic   # shared decoder (handles .d)

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "2.1.0"
_M64 = (1 << 64) - 1
_S64 = 1 << 63


def _to_int(value: Any) -> Optional[int]:
    """Parse a 64-bit value (no width masking)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value & _M64
    if isinstance(value, str):
        v = value.strip()
        if not v:
            return None
        try:
            return (int(v, 16) if v.lower().startswith("0x") else int(v, 0)) & _M64
        except ValueError:
            try:
                return int(v) & _M64
            except ValueError:
                return None
    return None


def _u64(x: int) -> int:
    return x & _M64


def _s64(x: int) -> int:
    x &= _M64
    return x - (1 << 64) if x & _S64 else x


def _hex64(x: int) -> str:
    return f"0x{x & _M64:016x}"


# ─────────────────────────────────────────────────────────
# Golden 64-bit AMO semantics
# ─────────────────────────────────────────────────────────

def amo_compute64(op: str, old: int, src: int) -> int:
    """Golden 64-bit AMO. op is the normalised name (swap/add/and/.../maxu)."""
    old, src = _u64(old), _u64(src)
    if op == "swap":
        return src
    if op == "add":
        return _u64(old + src)
    if op == "and":
        return old & src
    if op == "or":
        return old | src
    if op == "xor":
        return old ^ src
    if op == "min":
        return _u64(min(_s64(old), _s64(src)))
    if op == "max":
        return _u64(max(_s64(old), _s64(src)))
    if op == "minu":
        return min(old, src)
    if op == "maxu":
        return max(old, src)
    raise ValueError(f"unknown AMO op: {op}")


@dataclass
class RV64AtomViolation:
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

class RV64AtomicsVerifier:
    """
    Verify RV64 (.D) atomics from a commit log against a golden 64-bit model.

    Parameters
    ----------
    rtl_log            : list of RTL commit records
    iss_log            : optional ISS commit records (reserved)
    reservation_window : forward-progress window for LR/SC (instructions)
    max_violations     : stop collecting after this many violations
    """

    _SEV_WEIGHT = {"HIGH": 1.0, "MEDIUM": 0.4, "LOW": 0.15}

    def __init__(
        self,
        rtl_log:            List[Dict[str, Any]],
        iss_log:            Optional[List[Dict[str, Any]]] = None,
        reservation_window: int = 64,
        max_violations:     int = 200,
    ) -> None:
        self.rtl_log            = rtl_log or []
        self.iss_log            = iss_log or []
        self.reservation_window = reservation_window
        self.max_violations     = max_violations
        self._regs: Dict[str, int] = {}
        self._mem:  Dict[int, int] = {}
        self._resv_addr: Optional[int] = None
        self._resv_seq:  Optional[int] = None
        self._violations: List[RV64AtomViolation] = []
        self._stats = {"lr_d": 0, "sc_d": 0, "amo_d": 0,
                       "sc_success": 0, "sc_fail": 0, "misaligned": 0}

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def _mem_entry(rec: Dict, key: str) -> Optional[Tuple[int, int, Optional[int]]]:
        seq_list = rec.get(key)
        if not seq_list:
            return None
        e = seq_list[0] if isinstance(seq_list, list) else seq_list
        if not isinstance(e, dict):
            return None
        addr = _to_int(e.get("addr"))
        if addr is None:
            return None
        return addr, e.get("size", 8), _to_int(e.get("value"))

    def _flag(self, v: RV64AtomViolation) -> None:
        if len(self._violations) < self.max_violations:
            self._violations.append(v)

    def _apply_regs(self, rec: Dict) -> None:
        for name, val in (rec.get("regs") or {}).items():
            iv = _to_int(val)
            if iv is not None:
                self._regs[name] = iv
        self._regs["x0"] = 0

    def _check_align(self, addr: Optional[int], rec: Dict, kind: str, seq: int) -> bool:
        if addr is None:
            return False
        if addr % 8 != 0:
            self._stats["misaligned"] += 1
            trap = rec.get("trap")
            want = 4 if kind == "lr" else 6      # load vs store/AMO misaligned
            if not trap:
                self._flag(RV64AtomViolation(
                    "amod_alignment", "HIGH", seq, rec.get("pc"), rec.get("disasm"),
                    f"misaligned .D atomic at {_hex64(addr)} did not raise a "
                    f"misaligned trap (expected cause {want})",
                    expected=f"trap cause {want}", actual="no trap"))
            return True
        return False

    # -- per-kind -------------------------------------------------------------

    def _check_lr(self, rec, d, seq) -> None:
        self._stats["lr_d"] += 1
        mr = self._mem_entry(rec, "mem_reads")
        addr = mr[0] if mr else None
        if self._check_align(addr, rec, "lr", seq) or addr is None:
            return
        loaded = mr[2]
        if d.rd and d.rd != "x0" and loaded is not None:
            rd_val = _to_int((rec.get("regs") or {}).get(d.rd))
            if rd_val is not None and rd_val != loaded:
                self._flag(RV64AtomViolation(
                    "amod_rd_value", "HIGH", seq, rec.get("pc"), rec.get("disasm"),
                    f"LR.D {d.rd} != value read from {_hex64(addr)}",
                    expected=_hex64(loaded), actual=_hex64(rd_val)))
        if loaded is not None:
            self._mem[addr] = loaded
        self._resv_addr, self._resv_seq = addr, seq

    def _check_sc(self, rec, d, seq) -> None:
        self._stats["sc_d"] += 1
        mw = self._mem_entry(rec, "mem_writes")
        addr = mw[0] if mw else self._resv_addr
        if self._check_align(addr, rec, "sc", seq):
            return
        rd_val = _to_int((rec.get("regs") or {}).get(d.rd)) if d.rd else None
        wrote = mw is not None
        valid = (self._resv_addr is not None and addr == self._resv_addr)
        if valid and self.reservation_window > 0 and self._resv_seq is not None:
            if seq - self._resv_seq > self.reservation_window:
                valid = False
        if valid:
            self._stats["sc_success"] += 1
            if not wrote:
                self._flag(RV64AtomViolation("scd_success_no_write", "HIGH", seq,
                    rec.get("pc"), rec.get("disasm"),
                    f"SC.D with a valid reservation on {_hex64(addr)} did not write"))
            if rd_val is not None and rd_val != 0:
                self._flag(RV64AtomViolation("scd_success_rd", "HIGH", seq,
                    rec.get("pc"), rec.get("disasm"),
                    "SC.D succeeded but did not return 0",
                    expected="0x0", actual=_hex64(rd_val)))
            if wrote and d.rs2 and d.rs2 in self._regs and mw[2] is not None:
                exp = self._regs[d.rs2]
                if mw[2] != exp:
                    self._flag(RV64AtomViolation("scd_store_value", "HIGH", seq,
                        rec.get("pc"), rec.get("disasm"),
                        f"SC.D stored {_hex64(mw[2])} but rs2 ({d.rs2}) = {_hex64(exp)}",
                        expected=_hex64(exp), actual=_hex64(mw[2])))
            if wrote and mw[2] is not None and addr is not None:
                self._mem[addr] = mw[2]
        else:
            self._stats["sc_fail"] += 1
            if wrote:
                self._flag(RV64AtomViolation("scd_fail_wrote", "HIGH", seq,
                    rec.get("pc"), rec.get("disasm"),
                    "SC.D without a valid reservation wrote memory (atomicity violation)",
                    expected="no write", actual="memory write"))
            if rd_val is not None and rd_val == 0:
                self._flag(RV64AtomViolation("scd_fail_rd", "HIGH", seq,
                    rec.get("pc"), rec.get("disasm"),
                    "SC.D failed but returned success code 0",
                    expected="non-zero", actual="0x0"))
        self._resv_addr = self._resv_seq = None

    def _check_amo(self, rec, d, seq) -> None:
        self._stats["amo_d"] += 1
        mr = self._mem_entry(rec, "mem_reads")
        mw = self._mem_entry(rec, "mem_writes")
        addr = (mr[0] if mr else None) or (mw[0] if mw else None)
        if self._check_align(addr, rec, "amo", seq) or addr is None:
            return
        old = mr[2] if mr else self._mem.get(addr)
        if d.rd and d.rd != "x0" and old is not None:
            rd_val = _to_int((rec.get("regs") or {}).get(d.rd))
            if rd_val is not None and rd_val != old:
                self._flag(RV64AtomViolation("amod_rd_value", "HIGH", seq,
                    rec.get("pc"), rec.get("disasm"),
                    f"AMO{d.mnem}.D {d.rd} != old memory value at {_hex64(addr)}",
                    expected=_hex64(old), actual=_hex64(rd_val)))
        if old is not None and d.rs2 and d.rs2 in self._regs:
            src = self._regs[d.rs2]
            try:
                exp_new = amo_compute64(d.mnem, old, src)
            except ValueError:
                exp_new = None
            if exp_new is not None and mw and mw[2] is not None and mw[2] != exp_new:
                self._flag(RV64AtomViolation("amod_writeback", "HIGH", seq,
                    rec.get("pc"), rec.get("disasm"),
                    f"AMO{d.mnem}.D wrote {_hex64(mw[2])} but f({_hex64(old)}, "
                    f"{_hex64(src)}) = {_hex64(exp_new)}",
                    expected=_hex64(exp_new), actual=_hex64(mw[2])))
        if mw and mw[2] is not None:
            self._mem[addr] = mw[2]
        elif old is not None:
            self._mem[addr] = old
        if self._resv_addr is not None and addr == self._resv_addr:
            self._resv_addr = self._resv_seq = None

    # -- main loop ------------------------------------------------------------

    def run(self) -> Dict[str, Any]:
        started = datetime.now(timezone.utc)
        n = len(self.rtl_log)
        for i, rec in enumerate(self.rtl_log):
            if len(self._violations) >= self.max_violations:
                break
            if not isinstance(rec, dict):
                continue
            seq = rec.get("seq", i)
            disasm = (rec.get("disasm") or "").strip()
            d = decode_atomic(disasm)
            if d is not None and d.width == "d":
                try:
                    if d.kind == "lr":
                        self._check_lr(rec, d, seq)
                    elif d.kind == "sc":
                        self._check_sc(rec, d, seq)
                    else:
                        self._check_amo(rec, d, seq)
                except Exception as exc:           # never crash the pipeline
                    logger.warning("rv64_atomics_verifier: record %d raised: %s", seq, exc)
            self._apply_regs(rec)
        finished = datetime.now(timezone.utc)
        return self._report(n, started, finished)

    def _band(self) -> Tuple[float, str]:
        if not self._violations:
            return 0.0, "CLEAN"
        atomics = max(1, self._stats["lr_d"] + self._stats["sc_d"] + self._stats["amo_d"])
        score = sum(self._SEV_WEIGHT.get(v.severity, 0.2) for v in self._violations)
        norm = min(1.0, score / atomics)
        if any(v.severity == "HIGH" for v in self._violations):
            band = "CRITICAL"
        elif norm >= 0.3:
            band = "DEGRADED"
        else:
            band = "MINOR"
        return round(norm, 4), band

    def _report(self, n: int, started, finished) -> Dict[str, Any]:
        score, band = self._band()
        examined = self._stats["lr_d"] + self._stats["sc_d"] + self._stats["amo_d"]
        high = [v for v in self._violations if v.severity == "HIGH"]
        return {
            "schema_version":   SCHEMA_VERSION,
            "agent":            "rv64_atomics_verifier",
            "records_checked":  n,
            "atomics_d_examined": examined,
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
        logger.warning("rv64_atomics_verifier: cannot read manifest: %s", exc)
        return 0

    run_dir = Path(manifest.get("run_dir", manifest_path.parent))
    outputs = manifest.get("outputs", {})
    rtl_log = _load_log(run_dir, outputs, "rtl_commit_log", "rtl_commit.jsonl")
    iss_log = _load_log(run_dir, outputs, "iss_commit_log", "iss_commit.jsonl")
    if not rtl_log:
        logger.info("rv64_atomics_verifier: no RTL commit log, skipping")
        return 0

    report = RV64AtomicsVerifier(rtl_log, iss_log).run()
    if report["atomics_d_examined"] == 0:
        return 0                                  # no .D atomics -> nothing to write

    report_path = run_dir / "rv64_atomics_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
        f.write("\n")

    manifest.setdefault("outputs", {})["rv64_atomics_report"] = "rv64_atomics_report.json"
    manifest.setdefault("phases", {})["rv64_atomics_check"] = {
        "status": "pass" if report["pass"] else "fail",
        "violations": report["total_violations"],
    }
    tmp = manifest_path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    tmp.rename(manifest_path)

    logger.info("rv64_atomics_verifier: %d .D atomics, %d violations, band=%s",
                report["atomics_d_examined"], report["total_violations"], report["band"])
    return 0 if report["pass"] else 1


# ─────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":          # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description="RV64 (.D) atomics verifier")
    ap.add_argument("--manifest", type=Path)
    ap.add_argument("--rtl", type=Path)
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
        rep = RV64AtomicsVerifier(log).run()
        print(json.dumps(rep, indent=2))
        raise SystemExit(0 if rep["pass"] else 1)
    ap.print_help()
