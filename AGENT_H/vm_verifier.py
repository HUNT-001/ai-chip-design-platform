"""
AGENT_H/vm_verifier.py
======================
T30 — Sv32 Virtual-Memory Verification

Verifies RISC-V **Sv32** virtual-memory translation from the canonical commit
log.  This is the layer directly above privilege/PMP (``privilege_verifier``)
and is the gate for full Linux-class cores: a wrong page-table walk, a missing
or spurious page fault, or a permission check that ignores the U / SUM / MXR
rules is a security-critical functional bug that value-level tandem diffing does
not reliably surface.

The technically-novel core is a **golden software Sv32 MMU** — a spec-faithful
two-level page-table walker (`Sv32MMU`) that, given a physical-memory image and
``satp``, translates a virtual address for a given access type and privilege and
returns either a physical address or the exact page-fault cause.  `VMVerifier`
runs this golden model against the trace and compares.

Conservative gating (no false positives, no flow disruption)
------------------------------------------------------------
Everything is gated on information actually present in the trace, so the agent
is a clean no-op on bare-metal / M-mode / no-MMU runs:

  * translation checks run only when ``satp`` selects Sv32 **and** a physical
    page-table image is available (``phys_mem`` on the manifest or a record);
  * M-mode accesses are skipped (Sv32 translation applies to S/U-mode);
  * a memory access is checked only when it carries a virtual address
    (``vaddr``); the committed physical address is taken from ``paddr`` when
    present.

Optional trace contract (all additive — never breaks the base schema)
---------------------------------------------------------------------
  record["csrs"]["satp"]         Sv32 enable + root page number
  record["priv"|"mode"|...]      current privilege (see privilege_verifier)
  record["phys_mem"]             {hex_addr: hex_word}  physical page-table image
  record["mstatus"] bits         SUM (18) / MXR (19) consulted from csrs
  mem_reads/mem_writes entries:   {"vaddr": "0x..", "paddr": "0x..", ...}
  record["trap"]                  page-fault cause 12 (fetch) / 13 (load) / 15 (store)

Checks
------
  vm_translation        committed physical address != golden translation
  vm_missing_fault      access should page-fault but did not (or wrong cause)
  vm_spurious_fault     a valid translation raised a page fault

Usage
-----
  from AGENT_H.vm_verifier import Sv32MMU, VMVerifier
  report = VMVerifier(rtl_log, phys_mem=page_table_image).run()

  from AGENT_H.vm_verifier import run_from_manifest
  run_from_manifest(Path(run_dir) / "run_manifest.json")
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

_M32 = 0xFFFFFFFF

# Sv32 geometry
PAGESIZE = 4096
PTESIZE  = 4
LEVELS   = 2

# PTE flag bit masks
PTE_V = 1 << 0
PTE_R = 1 << 1
PTE_W = 1 << 2
PTE_X = 1 << 3
PTE_U = 1 << 4
PTE_G = 1 << 5
PTE_A = 1 << 6
PTE_D = 1 << 7

# page-fault causes
CAUSE_FETCH_PAGE_FAULT = 12
CAUSE_LOAD_PAGE_FAULT  = 13
CAUSE_STORE_PAGE_FAULT = 15

# privilege levels
PRIV_U, PRIV_S, PRIV_M = 0, 1, 3

# mstatus bits
MSTATUS_SUM = 1 << 18
MSTATUS_MXR = 1 << 19


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


# ─────────────────────────────────────────────────────────
# Golden Sv32 MMU
# ─────────────────────────────────────────────────────────

@dataclass
class Translation:
    ok:    bool
    pa:    Optional[int]    = None     # physical address (when ok)
    cause: Optional[int]    = None     # page-fault cause (when not ok)
    level: Optional[int]    = None     # leaf level (1 = superpage, 0 = 4KB)
    pte:   Optional[int]    = None
    reason: str            = ""


class Sv32MMU:
    """
    Golden RISC-V Sv32 page-table walker.

    Parameters
    ----------
    phys_mem  : {physical_word_address: 32-bit value} physical memory image
                (must contain the page-table entries that the walk reads)
    satp      : satp CSR value (MODE[31], ASID[30:22], PPN[21:0])
    """

    def __init__(self, phys_mem: Dict[int, int], satp: int) -> None:
        self.mem  = phys_mem
        self.satp = satp & _M32

    @property
    def enabled(self) -> bool:
        return ((self.satp >> 31) & 1) == 1      # MODE == 1 -> Sv32

    @property
    def root_ppn(self) -> int:
        return self.satp & 0x3FFFFF              # PPN[21:0]

    def _read_pte(self, addr: int) -> int:
        return self.mem.get(addr & _M32, 0) & _M32

    @staticmethod
    def _vpn(va: int, i: int) -> int:
        # VPN[1] = va[31:22], VPN[0] = va[21:12]
        return (va >> (12 + 10 * i)) & 0x3FF

    def translate(
        self,
        va:     int,
        access: str,                 # "fetch" | "load" | "store"
        priv:   int,
        sum_:   bool = False,
        mxr:    bool = False,
    ) -> Translation:
        """Translate a virtual address; return a Translation result."""
        va &= _M32
        cause = fault_cause_for(access)

        if not self.enabled or priv == PRIV_M:
            # no translation: identity map (caller should gate on .enabled)
            return Translation(ok=True, pa=va, level=None, reason="bare/M-mode")

        a = self.root_ppn * PAGESIZE
        for i in (1, 0):                          # LEVELS-1 .. 0
            pte_addr = a + self._vpn(va, i) * PTESIZE
            pte = self._read_pte(pte_addr)

            if not (pte & PTE_V) or ((pte & PTE_W) and not (pte & PTE_R)):
                # invalid, or reserved encoding W=1,R=0
                return Translation(False, cause=cause, pte=pte, level=i,
                                   reason="invalid or reserved PTE")

            if (pte & PTE_R) or (pte & PTE_X):
                # leaf PTE
                return self._finish_leaf(va, pte, i, access, priv, sum_, mxr, cause)

            # pointer to next level
            a = ((pte >> 10) & 0x3FFFFF) * PAGESIZE
            if i == 0:
                return Translation(False, cause=cause, pte=pte, level=i,
                                   reason="pointer PTE at last level")

        return Translation(False, cause=cause, reason="walk fell through")

    def _finish_leaf(self, va, pte, level, access, priv, sum_, mxr, cause) -> Translation:
        # --- permission check ---
        r = bool(pte & PTE_R)
        w = bool(pte & PTE_W)
        x = bool(pte & PTE_X)
        u = bool(pte & PTE_U)

        if access == "fetch":
            if not x:
                return Translation(False, cause=cause, pte=pte, level=level,
                                   reason="no execute permission")
            if priv == PRIV_U and not u:
                return Translation(False, cause=cause, pte=pte, level=level,
                                   reason="U-mode fetch of supervisor page")
            if priv == PRIV_S and u:
                return Translation(False, cause=cause, pte=pte, level=level,
                                   reason="S-mode fetch of user page")
        elif access == "load":
            readable = r or (mxr and x)
            if not readable:
                return Translation(False, cause=cause, pte=pte, level=level,
                                   reason="no read permission")
            if priv == PRIV_U and not u:
                return Translation(False, cause=cause, pte=pte, level=level,
                                   reason="U-mode load of supervisor page")
            if priv == PRIV_S and u and not sum_:
                return Translation(False, cause=cause, pte=pte, level=level,
                                   reason="S-mode load of user page without SUM")
        else:  # store
            if not w:
                return Translation(False, cause=cause, pte=pte, level=level,
                                   reason="no write permission")
            if priv == PRIV_U and not u:
                return Translation(False, cause=cause, pte=pte, level=level,
                                   reason="U-mode store to supervisor page")
            if priv == PRIV_S and u and not sum_:
                return Translation(False, cause=cause, pte=pte, level=level,
                                   reason="S-mode store to user page without SUM")

        # --- superpage alignment (leaf at level 1) ---
        if level == 1 and ((pte >> 10) & 0x3FF) != 0:
            return Translation(False, cause=cause, pte=pte, level=level,
                               reason="misaligned superpage (PPN[0] != 0)")

        # --- physical address ---
        offset = va & 0xFFF
        if level == 1:
            ppn1 = (pte >> 20) & 0xFFF
            pa = (ppn1 << 22) | (((va >> 12) & 0x3FF) << 12) | offset
        else:
            ppn = (pte >> 10) & 0x3FFFFF
            pa = (ppn << 12) | offset
        return Translation(True, pa=pa & _M32, level=level, pte=pte, reason="ok")


# ─────────────────────────────────────────────────────────
# Violation record
# ─────────────────────────────────────────────────────────

@dataclass
class VMViolation:
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


class VMVerifier:
    """
    Verify Sv32 virtual-memory translation from a commit log.

    Parameters
    ----------
    rtl_log        : list of RTL commit records
    iss_log        : optional ISS commit records (reserved for cross-check)
    phys_mem       : optional {addr: word} physical page-table image (may also
                     be supplied per-record via a ``phys_mem`` field)
    max_violations : stop collecting after this many violations
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
        self._violations: List[VMViolation] = []
        self._stats = {"translations": 0, "checked": 0, "faults_expected": 0}

    def _merge_phys(self, m: Dict[Any, Any]) -> None:
        for k, v in m.items():
            ak = _to_int(k)
            av = _to_int(v)
            if ak is not None and av is not None:
                self._phys[ak & _M32] = av & _M32

    def _flag(self, v: VMViolation) -> None:
        if len(self._violations) < self.max_violations:
            self._violations.append(v)

    def _trap_cause(self, rec: Dict) -> Optional[int]:
        t = rec.get("trap")
        return _to_int(t.get("cause")) if isinstance(t, dict) else None

    def _accesses(self, rec: Dict) -> List[Tuple[str, int, Optional[int]]]:
        """Return [(access, vaddr, committed_paddr)] for entries carrying vaddr."""
        out: List[Tuple[str, int, Optional[int]]] = []
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
        # merge any per-record page-table image first
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

        mmu = Sv32MMU(self._phys, self._satp)
        if not mmu.enabled or not self._phys:
            return                          # gated: no Sv32 or no page-table image

        priv = _parse_priv(rec)
        if priv is None or priv == PRIV_M:
            return                          # translation applies to S/U only

        sum_ = bool(self._mstatus & MSTATUS_SUM)
        mxr  = bool(self._mstatus & MSTATUS_MXR)
        cause_seen = self._trap_cause(rec)

        for access, va, committed_pa in self._accesses(rec):
            self._stats["translations"] += 1
            t = mmu.translate(va, access, priv, sum_, mxr)
            self._stats["checked"] += 1

            if t.ok:
                if committed_pa is not None and committed_pa != t.pa:
                    self._flag(VMViolation(
                        "vm_translation", "HIGH", seq, rec.get("pc"), rec.get("disasm"),
                        f"{access} VA 0x{va:08x} translated to 0x{committed_pa:08x}, "
                        f"golden Sv32 walk gives 0x{t.pa:08x}",
                        expected=f"0x{t.pa:08x}", actual=f"0x{committed_pa:08x}"))
                if cause_seen in (CAUSE_FETCH_PAGE_FAULT, CAUSE_LOAD_PAGE_FAULT,
                                  CAUSE_STORE_PAGE_FAULT):
                    self._flag(VMViolation(
                        "vm_spurious_fault", "HIGH", seq, rec.get("pc"), rec.get("disasm"),
                        f"{access} VA 0x{va:08x} is a valid translation but raised a "
                        f"page fault (cause {cause_seen})",
                        expected="no fault", actual=f"trap cause {cause_seen}"))
            else:
                self._stats["faults_expected"] += 1
                if cause_seen != t.cause:
                    self._flag(VMViolation(
                        "vm_missing_fault", "HIGH", seq, rec.get("pc"), rec.get("disasm"),
                        f"{access} VA 0x{va:08x} must page-fault ({t.reason}) but trap "
                        f"cause was {cause_seen}",
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
                logger.warning("vm_verifier: record %d raised: %s", seq, exc)

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
        return {
            "schema_version":   SCHEMA_VERSION,
            "agent":            "vm_verifier",
            "records_checked":  n,
            "sv32_enabled":     ((self._satp >> 31) & 1) == 1,
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
        logger.warning("vm_verifier: cannot read manifest: %s", exc)
        return 0

    run_dir = Path(manifest.get("run_dir", manifest_path.parent))
    outputs = manifest.get("outputs", {})
    rtl_log = _load_log(run_dir, outputs, "rtl_commit_log", "rtl_commit.jsonl")
    iss_log = _load_log(run_dir, outputs, "iss_commit_log", "iss_commit.jsonl")
    if not rtl_log:
        logger.info("vm_verifier: no RTL commit log, skipping")
        return 0

    phys_mem = manifest.get("phys_mem") or manifest.get("page_table")
    report = VMVerifier(rtl_log, iss_log, phys_mem=phys_mem).run()

    report_path = run_dir / "vm_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
        f.write("\n")

    manifest.setdefault("outputs", {})["vm_report"] = "vm_report.json"
    manifest.setdefault("phases", {})["vm_check"] = {
        "status": "pass" if report["pass"] else "fail",
        "violations": report["total_violations"],
    }
    tmp = manifest_path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    tmp.rename(manifest_path)

    logger.info("vm_verifier: Sv32=%s, %d translations, %d violations, band=%s",
                report["sv32_enabled"], report["translations"],
                report["total_violations"], report["band"])
    return 0 if report["pass"] else 1


# ─────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":          # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description="Sv32 virtual-memory verifier")
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
        rep = VMVerifier(log).run()
        print(json.dumps(rep, indent=2))
        raise SystemExit(0 if rep["pass"] else 1)
    ap.print_help()
