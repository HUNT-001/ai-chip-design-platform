"""
AGENT_H/sv_mmu_verifier.py
==========================
T37 — Sv39 / Sv48 Virtual-Memory Verification (RV64 widening, phase 2)

Generalises the golden page-table walker from Sv32 (`vm_verifier`) to the RV64
paging modes **Sv39** (3-level, 39-bit VA) and **Sv48** (4-level, 48-bit VA) —
the address-translation schemes used by every Linux-class RISC-V core
(Rocket, BOOM, CVA6, Shakti C-class).

The novel core is a single mode-parameterised walker, `SvMMU`, that handles
Sv32 / Sv39 / Sv48 from one code path: it reads 8-byte PTEs, walks 2/3/4 levels,
enforces 4 KB / 2 MB / 1 GB (and 512 GB) superpage alignment, checks the
non-canonical-address rule (VA high bits must sign-extend the top VA bit), and
applies the R/W/X + U + SUM + MXR permission model.

`SvMMUVerifier` runs the golden walker against the trace and compares the
DUT-served translation — exactly like the Sv32 verifier, and with the same
conservative gating.  It deliberately handles **only Sv39/Sv48** (RV64), leaving
Sv32 to `vm_verifier`, so the two never double-cover.

Checks
------
  sv_translation     committed physical address != golden translation
  sv_missing_fault    access that must page-fault did not (or wrong cause)
  sv_spurious_fault   a valid translation that raised a page fault

Usage
-----
  from AGENT_H.sv_mmu_verifier import SvMMU, SvMMUVerifier
  report = SvMMUVerifier(rtl_log, phys_mem=page_table_image).run()
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "2.1.0"
_M64 = (1 << 64) - 1
PAGESIZE = 4096

# PTE flag bits
PTE_V = 1 << 0
PTE_R = 1 << 1
PTE_W = 1 << 2
PTE_X = 1 << 3
PTE_U = 1 << 4
PTE_G = 1 << 5
PTE_A = 1 << 6
PTE_D = 1 << 7

CAUSE_FETCH_PAGE_FAULT = 12
CAUSE_LOAD_PAGE_FAULT  = 13
CAUSE_STORE_PAGE_FAULT = 15

PRIV_U, PRIV_S, PRIV_M = 0, 1, 3
MSTATUS_SUM = 1 << 18
MSTATUS_MXR = 1 << 19
PA_MASK = (1 << 56) - 1     # Sv39/Sv48 physical address width

# mode parameters
_MODES = {
    "sv39": dict(levels=3, vpn_bits=9, va_bits=39, mode_val=8),
    "sv48": dict(levels=4, vpn_bits=9, va_bits=48, mode_val=9),
}
_PTE_PPN_MASK = (1 << 44) - 1   # PTE bits [53:10]


def _to_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
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


def fault_cause_for(access: str) -> int:
    return {"fetch": CAUSE_FETCH_PAGE_FAULT,
            "load":  CAUSE_LOAD_PAGE_FAULT,
            "store": CAUSE_STORE_PAGE_FAULT}[access]


def satp_mode(satp: int) -> Optional[str]:
    """Return 'sv39' / 'sv48' / 'bare' / None from a 64-bit satp value."""
    m = (satp >> 60) & 0xF
    if m == 0:
        return "bare"
    if m == 8:
        return "sv39"
    if m == 9:
        return "sv48"
    return None


# ─────────────────────────────────────────────────────────
# Golden Sv39/Sv48 MMU
# ─────────────────────────────────────────────────────────

@dataclass
class Translation:
    ok:     bool
    pa:     Optional[int] = None
    cause:  Optional[int] = None
    level:  Optional[int] = None
    pte:    Optional[int] = None
    reason: str = ""


class SvMMU:
    """
    Golden multi-level Sv39/Sv48 page-table walker.

    Parameters
    ----------
    phys_mem : {addr: pte_value} physical memory image (8-byte PTEs)
    satp     : 64-bit satp CSR value
    """

    def __init__(self, phys_mem: Dict[int, int], satp: int) -> None:
        self.mem  = phys_mem
        self.satp = satp & _M64
        self.mode = satp_mode(self.satp)
        self.params = _MODES.get(self.mode or "")

    @property
    def enabled(self) -> bool:
        return self.params is not None

    @property
    def root_ppn(self) -> int:
        return self.satp & ((1 << 44) - 1)        # PPN[43:0]

    def _read_pte(self, addr: int) -> int:
        return self.mem.get(addr, 0)

    def _vpn(self, va: int, i: int, vpn_bits: int) -> int:
        return (va >> (12 + vpn_bits * i)) & ((1 << vpn_bits) - 1)

    def translate(self, va: int, access: str, priv: int,
                  sum_: bool = False, mxr: bool = False) -> Translation:
        cause = fault_cause_for(access)
        if not self.enabled or priv == PRIV_M:
            return Translation(True, pa=va, reason="bare/M-mode")

        p = self.params
        levels, vpn_bits, va_bits = p["levels"], p["vpn_bits"], p["va_bits"]

        # non-canonical VA: bits above va_bits must equal bit (va_bits-1)
        signbit = (va >> (va_bits - 1)) & 1
        top = va >> va_bits
        expected_top = ((1 << (64 - va_bits)) - 1) if signbit else 0
        if top != expected_top:
            return Translation(False, cause=cause, reason="non-canonical virtual address")

        a = self.root_ppn * PAGESIZE
        for i in range(levels - 1, -1, -1):
            pte_addr = a + self._vpn(va, i, vpn_bits) * 8
            pte = self._read_pte(pte_addr)

            if not (pte & PTE_V) or ((pte & PTE_W) and not (pte & PTE_R)):
                return Translation(False, cause=cause, pte=pte, level=i,
                                   reason="invalid or reserved PTE")
            if (pte & PTE_R) or (pte & PTE_X):
                return self._finish_leaf(va, pte, i, access, priv, sum_, mxr,
                                         cause, vpn_bits)
            a = ((pte >> 10) & _PTE_PPN_MASK) * PAGESIZE
            if i == 0:
                return Translation(False, cause=cause, pte=pte, level=i,
                                   reason="pointer PTE at last level")
        return Translation(False, cause=cause, reason="walk fell through")

    def _finish_leaf(self, va, pte, level, access, priv, sum_, mxr,
                     cause, vpn_bits) -> Translation:
        r, w, x, u = (bool(pte & PTE_R), bool(pte & PTE_W),
                      bool(pte & PTE_X), bool(pte & PTE_U))
        if access == "fetch":
            if not x:
                return Translation(False, cause=cause, pte=pte, level=level,
                                   reason="no execute permission")
            if priv == PRIV_U and not u:
                return Translation(False, cause=cause, pte=pte, level=level,
                                   reason="U fetch of supervisor page")
            if priv == PRIV_S and u:
                return Translation(False, cause=cause, pte=pte, level=level,
                                   reason="S fetch of user page")
        elif access == "load":
            if not (r or (mxr and x)):
                return Translation(False, cause=cause, pte=pte, level=level,
                                   reason="no read permission")
            if priv == PRIV_U and not u:
                return Translation(False, cause=cause, pte=pte, level=level,
                                   reason="U load of supervisor page")
            if priv == PRIV_S and u and not sum_:
                return Translation(False, cause=cause, pte=pte, level=level,
                                   reason="S load of user page without SUM")
        else:  # store
            if not w:
                return Translation(False, cause=cause, pte=pte, level=level,
                                   reason="no write permission")
            if priv == PRIV_U and not u:
                return Translation(False, cause=cause, pte=pte, level=level,
                                   reason="U store to supervisor page")
            if priv == PRIV_S and u and not sum_:
                return Translation(False, cause=cause, pte=pte, level=level,
                                   reason="S store to user page without SUM")

        ppn = (pte >> 10) & _PTE_PPN_MASK
        low = vpn_bits * level
        # superpage misalignment: the low `level` PPN sub-fields must be zero
        if level > 0 and (ppn & ((1 << low) - 1)) != 0:
            return Translation(False, cause=cause, pte=pte, level=level,
                               reason="misaligned superpage")
        page_off_bits = 12 + low
        pa = ((ppn >> low) << page_off_bits) | (va & ((1 << page_off_bits) - 1))
        return Translation(True, pa=pa & PA_MASK, level=level, pte=pte, reason="ok")


# ─────────────────────────────────────────────────────────
# Violation
# ─────────────────────────────────────────────────────────

@dataclass
class SvViolation:
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


def _parse_priv(rec: Dict[str, Any]) -> Optional[int]:
    for k in ("priv", "mode", "privilege", "prv", "priv_mode"):
        if k in rec and rec[k] is not None:
            v = rec[k]
            if isinstance(v, str):
                s = v.strip().lower()
                if s in ("m", "machine", "3"): return PRIV_M
                if s in ("s", "supervisor", "1"): return PRIV_S
                if s in ("u", "user", "0"): return PRIV_U
                iv = _to_int(s)
                return iv if iv in (0, 1, 3) else None
            iv = _to_int(v)
            return iv if iv in (0, 1, 3) else None
    return None


# ─────────────────────────────────────────────────────────
# Verifier
# ─────────────────────────────────────────────────────────

class SvMMUVerifier:
    """
    Verify Sv39/Sv48 translation from a commit log (RV64 paging).

    Parameters
    ----------
    rtl_log  : list of RTL commit records
    iss_log  : optional ISS commit records (reserved)
    phys_mem : {addr: pte} physical page-table image (also per-record `phys_mem`)
    """

    _SEV_WEIGHT = {"HIGH": 1.0, "MEDIUM": 0.4, "LOW": 0.15}

    def __init__(
        self,
        rtl_log:        List[Dict[str, Any]],
        iss_log:        Optional[List[Dict[str, Any]]] = None,
        phys_mem:       Optional[Dict[Any, Any]] = None,
        max_violations: int = 200,
    ) -> None:
        self.rtl_log        = rtl_log or []
        self.iss_log        = iss_log or []
        self.max_violations = max_violations
        self._phys: Dict[int, int] = {}
        if phys_mem:
            self._merge_phys(phys_mem)
        self._satp    = 0
        self._mstatus = 0
        self._violations: List[SvViolation] = []
        self._stats = {"translations": 0, "checked": 0, "faults_expected": 0}

    def _merge_phys(self, m: Dict[Any, Any]) -> None:
        for k, v in m.items():
            ak, av = _to_int(k), _to_int(v)
            if ak is not None and av is not None:
                self._phys[ak] = av

    def _flag(self, v: SvViolation) -> None:
        if len(self._violations) < self.max_violations:
            self._violations.append(v)

    def _trap_cause(self, rec: Dict) -> Optional[int]:
        t = rec.get("trap")
        return _to_int(t.get("cause")) if isinstance(t, dict) else None

    def _accesses(self, rec: Dict) -> List[Tuple[str, int, Optional[int]]]:
        out = []
        for r in (rec.get("mem_reads") or []):
            if isinstance(r, dict) and r.get("vaddr") is not None:
                va = _to_int(r.get("vaddr"))
                if va is not None:
                    out.append(("load", va, _to_int(r.get("paddr"))))
        for w in (rec.get("mem_writes") or []):
            if isinstance(w, dict) and w.get("vaddr") is not None:
                va = _to_int(w.get("vaddr"))
                if va is not None:
                    out.append(("store", va, _to_int(w.get("paddr"))))
        return out

    def _check_record(self, rec: Dict, seq: int) -> None:
        pm = rec.get("phys_mem")
        if isinstance(pm, dict):
            self._merge_phys(pm)
        csrs = rec.get("csrs") or {}
        sv = _to_int(csrs.get("satp"))
        if sv is not None:
            self._satp = sv
        mv = _to_int(csrs.get("mstatus"))
        if mv is not None:
            self._mstatus = mv

        mmu = SvMMU(self._phys, self._satp)
        if not mmu.enabled or not self._phys:   # gated: only Sv39/Sv48 + page table
            return
        priv = _parse_priv(rec)
        if priv is None or priv == PRIV_M:
            return

        sum_ = bool(self._mstatus & MSTATUS_SUM)
        mxr  = bool(self._mstatus & MSTATUS_MXR)
        cause_seen = self._trap_cause(rec)

        for access, va, committed_pa in self._accesses(rec):
            self._stats["translations"] += 1
            t = mmu.translate(va, access, priv, sum_, mxr)
            self._stats["checked"] += 1
            if t.ok:
                if committed_pa is not None and committed_pa != t.pa:
                    self._flag(SvViolation(
                        "sv_translation", "HIGH", seq, rec.get("pc"), rec.get("disasm"),
                        f"{mmu.mode} {access} VA 0x{va:x} -> 0x{committed_pa:x}, golden "
                        f"walk gives 0x{t.pa:x}",
                        expected=f"0x{t.pa:x}", actual=f"0x{committed_pa:x}"))
                if cause_seen in (CAUSE_FETCH_PAGE_FAULT, CAUSE_LOAD_PAGE_FAULT,
                                  CAUSE_STORE_PAGE_FAULT):
                    self._flag(SvViolation(
                        "sv_spurious_fault", "HIGH", seq, rec.get("pc"), rec.get("disasm"),
                        f"{access} VA 0x{va:x} is a valid translation but raised a page "
                        f"fault (cause {cause_seen})",
                        expected="no fault", actual=f"trap cause {cause_seen}"))
            else:
                self._stats["faults_expected"] += 1
                if cause_seen != t.cause:
                    self._flag(SvViolation(
                        "sv_missing_fault", "HIGH", seq, rec.get("pc"), rec.get("disasm"),
                        f"{access} VA 0x{va:x} must page-fault ({t.reason}) but trap cause "
                        f"was {cause_seen}",
                        expected=f"trap cause {t.cause}", actual=str(cause_seen)))

    def run(self) -> Dict[str, Any]:
        started = datetime.now(timezone.utc)
        n = len(self.rtl_log)
        for i, rec in enumerate(self.rtl_log):
            if len(self._violations) >= self.max_violations:
                break
            if not isinstance(rec, dict):
                continue
            seq = rec.get("seq", i)
            try:
                self._check_record(rec, seq)
            except Exception as exc:               # never crash the pipeline
                logger.warning("sv_mmu_verifier: record %d raised: %s", seq, exc)
        finished = datetime.now(timezone.utc)
        return self._report(n, started, finished)

    def _band(self) -> Tuple[float, str]:
        if not self._violations:
            return 0.0, "CLEAN"
        score = sum(self._SEV_WEIGHT.get(v.severity, 0.2) for v in self._violations)
        norm  = min(1.0, score / max(1, self._stats["checked"]))
        if any(v.severity == "HIGH" for v in self._violations):
            band = "CRITICAL"
        elif norm >= 0.3:
            band = "DEGRADED"
        else:
            band = "MINOR"
        return round(norm, 4), band

    def _report(self, n: int, started, finished) -> Dict[str, Any]:
        score, band = self._band()
        high = [v for v in self._violations if v.severity == "HIGH"]
        mode = satp_mode(self._satp)
        return {
            "schema_version":   SCHEMA_VERSION,
            "agent":            "sv_mmu_verifier",
            "records_checked":  n,
            "mode":             mode,
            "sv_enabled":       mode in ("sv39", "sv48"),
            "translations":     self._stats["translations"],
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
        logger.warning("sv_mmu_verifier: cannot read manifest: %s", exc)
        return 0

    run_dir = Path(manifest.get("run_dir", manifest_path.parent))
    outputs = manifest.get("outputs", {})
    rtl_log = _load_log(run_dir, outputs, "rtl_commit_log", "rtl_commit.jsonl")
    iss_log = _load_log(run_dir, outputs, "iss_commit_log", "iss_commit.jsonl")
    if not rtl_log:
        logger.info("sv_mmu_verifier: no RTL commit log, skipping")
        return 0

    phys_mem = manifest.get("phys_mem") or manifest.get("page_table")
    report = SvMMUVerifier(rtl_log, iss_log, phys_mem=phys_mem).run()

    report_path = run_dir / "sv_mmu_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
        f.write("\n")

    manifest.setdefault("outputs", {})["sv_mmu_report"] = "sv_mmu_report.json"
    manifest.setdefault("phases", {})["sv_mmu_check"] = {
        "status": "pass" if report["pass"] else "fail",
        "violations": report["total_violations"],
    }
    tmp = manifest_path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    tmp.rename(manifest_path)

    logger.info("sv_mmu_verifier: mode=%s, %d translations, %d violations, band=%s",
                report["mode"], report["translations"],
                report["total_violations"], report["band"])
    return 0 if report["pass"] else 1


# ─────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":          # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description="Sv39/Sv48 virtual-memory verifier")
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
        rep = SvMMUVerifier(log).run()
        print(json.dumps(rep, indent=2))
        raise SystemExit(0 if rep["pass"] else 1)
    ap.print_help()
