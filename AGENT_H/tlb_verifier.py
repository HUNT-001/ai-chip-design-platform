"""
AGENT_H/tlb_verifier.py
=======================
T31 — TLB Coherence & sfence.vma Verification

Verifies Translation-Lookaside-Buffer behaviour from the canonical commit log,
building directly on the golden Sv32 MMU (`AGENT_H/vm_verifier.Sv32MMU`).

A TLB caches virtual→physical translations.  The architecturally-observable
correctness rules are:

  * A translation served by the TLB must be one the page tables *could* have
    produced — either the current walk, or a value cached **before** the page
    table changed and **not yet invalidated**.  Anything else is a fabricated
    translation.
  * After an `sfence.vma` that covers an entry, the next access must reflect the
    *current* page tables; serving the old (now-invalidated) value is a stale
    hit — the classic "forgot to flush the TLB" bug.

Staleness *before* an `sfence.vma` is architecturally permitted, so the model
only flags a stale value once a covering `sfence.vma` has retired — this is what
keeps the checker free of false positives.

Checks
------
  tlb_stale_after_sfence   served an invalidated entry after a covering sfence
  tlb_incoherent           served a translation that is neither the current walk
                           nor a legitimately-cached, non-invalidated entry
                           (covers fabricated translations and ASID leakage)

Conservative gating (no false positives)
----------------------------------------
  * runs only when ``satp`` selects Sv32 and a physical page-table image is
    available (same contract as the VM verifier);
  * only successful golden walks are compared (page-fault correctness is the VM
    verifier's job);
  * an `sfence.vma` with operands whose register values are not recoverable from
    the trace invalidates *nothing* (under-invalidate rather than risk a false
    stale-hit report); a full-flush `sfence.vma` is always handled precisely.

Usage
-----
  from AGENT_H.tlb_verifier import TLBVerifier
  report = TLBVerifier(rtl_log, phys_mem=page_table_image).run()

  from AGENT_H.tlb_verifier import run_from_manifest
  run_from_manifest(Path(run_dir) / "run_manifest.json")
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .vm_verifier import Sv32MMU, PTE_G, _to_int

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "2.1.0"
_M32 = 0xFFFFFFFF

PRIV_U, PRIV_S, PRIV_M = 0, 1, 3
MSTATUS_SUM = 1 << 18
MSTATUS_MXR = 1 << 19

# minimal ABI register-name map for sfence.vma operands
_ABI_X = {"zero": 0, "ra": 1, "sp": 2, "gp": 3, "tp": 4}
for _i in range(8):
    _ABI_X[f"a{_i}"] = 10 + _i
for _i in range(8):
    _ABI_X[f"t{_i}"] = (5 + _i) if _i < 3 else (28 + _i - 3)
for _i in range(12):
    _ABI_X[f"s{_i}"] = (8 + _i) if _i < 2 else (18 + _i - 2)

_XTOK = re.compile(r"\bx(\d{1,2})\b")


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


def _reg_index(tok: str) -> Optional[int]:
    tok = tok.strip().lower()
    m = _XTOK.fullmatch(tok)
    if m:
        n = int(m.group(1))
        return n if 0 <= n <= 31 else None
    return _ABI_X.get(tok)


# ─────────────────────────────────────────────────────────
# TLB entry / violation
# ─────────────────────────────────────────────────────────

@dataclass
class TLBEntry:
    pa:          int
    global_:     bool
    invalidated: bool = False


@dataclass
class TLBViolation:
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

class TLBVerifier:
    """
    Verify TLB coherence and sfence.vma behaviour from a commit log.

    Parameters
    ----------
    rtl_log        : list of RTL commit records
    iss_log        : optional ISS commit records (reserved for cross-check)
    phys_mem       : optional {addr: word} page-table image (also accepted
                     per-record via a ``phys_mem`` field)
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
        self._xregs: Dict[int, int] = {0: 0}
        # golden TLB: (asid, vpn) -> TLBEntry
        self._tlb: Dict[Tuple[int, int], TLBEntry] = {}
        self._violations: List[TLBViolation] = []
        self._stats = {"translations": 0, "fills": 0, "sfence": 0,
                       "permitted_stale": 0}

    def _merge_phys(self, m: Dict[Any, Any]) -> None:
        for k, v in m.items():
            ak, av = _to_int(k), _to_int(v)
            if ak is not None and av is not None:
                self._phys[ak & _M32] = av & _M32

    @property
    def _asid(self) -> int:
        return (self._satp >> 22) & 0x1FF

    def _flag(self, v: TLBViolation) -> None:
        if len(self._violations) < self.max_violations:
            self._violations.append(v)

    # -- sfence.vma scoped invalidation ---------------------------------------

    def _do_sfence(self, disasm: str) -> None:
        self._stats["sfence"] += 1
        toks = re.split(r"[\s,]+", disasm.strip())[1:]   # operands after mnemonic
        rs1_tok = toks[0] if len(toks) > 0 else "x0"
        rs2_tok = toks[1] if len(toks) > 1 else "x0"

        def _val(tok: str) -> Tuple[Optional[int], bool]:
            """(value, known). value None means register x0 (=> 'all')."""
            idx = _reg_index(tok)
            if idx is None:
                return None, False          # unparseable operand
            if idx == 0:
                return None, True            # x0 => wildcard
            return self._xregs.get(idx), (idx in self._xregs)

        addr, addr_known = _val(rs1_tok)
        asid, asid_known = _val(rs2_tok)

        # If an operand register is named but its value is unknown, we cannot
        # scope the flush safely → invalidate nothing (conservative).
        if (rs1_tok not in ("x0", "zero") and not addr_known) or \
           (rs2_tok not in ("x0", "zero") and not asid_known):
            return

        target_vpn = (addr >> 12) if addr is not None else None
        for (e_asid, e_vpn), entry in self._tlb.items():
            addr_match = (target_vpn is None) or (e_vpn == target_vpn)
            if asid is None:                 # rs2 = x0 → all ASIDs incl. global
                asid_match = True
            else:
                asid_match = (e_asid == asid) and (not entry.global_)
            if addr_match and asid_match:
                entry.invalidated = True

    # -- per-record ------------------------------------------------------------

    def _accesses(self, rec: Dict) -> List[Tuple[str, int, Optional[int]]]:
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

        disasm = (rec.get("disasm") or "").strip().lower()
        if disasm.split(" ")[0] == "sfence.vma" or disasm.startswith("sfence.vma"):
            self._do_sfence(disasm)
            self._fold_regs(rec)
            return

        mmu = Sv32MMU(self._phys, self._satp)
        if not mmu.enabled or not self._phys:
            self._fold_regs(rec)
            return
        priv = _parse_priv(rec)
        if priv is None or priv == PRIV_M:
            self._fold_regs(rec)
            return

        sum_ = bool(self._mstatus & MSTATUS_SUM)
        mxr  = bool(self._mstatus & MSTATUS_MXR)
        asid = self._asid

        for access, va, served in self._accesses(rec):
            if served is None:
                continue
            t = mmu.translate(va, access, priv, sum_, mxr)
            if not t.ok:
                continue                      # fault correctness is VM's job
            self._stats["translations"] += 1
            vpn = (va >> 12) & 0xFFFFF
            key = (asid, vpn)
            served &= _M32

            if served == t.pa:
                # legitimate fill / refresh of the current translation
                self._tlb[key] = TLBEntry(pa=served,
                                          global_=bool((t.pte or 0) & PTE_G),
                                          invalidated=False)
                self._stats["fills"] += 1
                continue

            # served != current walk → must be an explainable cached entry
            entry = self._tlb.get(key) or self._global_entry(vpn)
            if entry is not None and entry.pa == served:
                if entry.invalidated:
                    self._flag(TLBViolation(
                        "tlb_stale_after_sfence", "HIGH", seq, rec.get("pc"),
                        rec.get("disasm"),
                        f"{access} VA 0x{va:08x} served stale PA 0x{served:08x} after a "
                        f"covering sfence.vma; current walk gives 0x{t.pa:08x}",
                        expected=f"0x{t.pa:08x}", actual=f"0x{served:08x}"))
                else:
                    self._stats["permitted_stale"] += 1   # legal pre-sfence staleness
            else:
                self._flag(TLBViolation(
                    "tlb_incoherent", "HIGH", seq, rec.get("pc"), rec.get("disasm"),
                    f"{access} VA 0x{va:08x} served PA 0x{served:08x} which is neither the "
                    f"current walk (0x{t.pa:08x}) nor a valid cached entry",
                    expected=f"0x{t.pa:08x}", actual=f"0x{served:08x}"))

        self._fold_regs(rec)

    def _global_entry(self, vpn: int) -> Optional[TLBEntry]:
        for (e_asid, e_vpn), entry in self._tlb.items():
            if e_vpn == vpn and entry.global_:
                return entry
        return None

    def _fold_regs(self, rec: Dict) -> None:
        for name, val in (rec.get("regs") or {}).items():
            idx = _reg_index(name)
            iv = _to_int(val)
            if idx is not None and iv is not None:
                self._xregs[idx] = iv & _M32
        self._xregs[0] = 0

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
            try:
                self._check_record(rec, seq)
            except Exception as exc:               # never crash the pipeline
                logger.warning("tlb_verifier: record %d raised: %s", seq, exc)
        finished = datetime.now(timezone.utc)
        return self._report(n, started, finished)

    def _band(self) -> Tuple[float, str]:
        if not self._violations:
            return 0.0, "CLEAN"
        score = sum(self._SEV_WEIGHT.get(v.severity, 0.2) for v in self._violations)
        norm  = min(1.0, score / max(1, self._stats["translations"]))
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
            "agent":            "tlb_verifier",
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
        logger.warning("tlb_verifier: cannot read manifest: %s", exc)
        return 0

    run_dir = Path(manifest.get("run_dir", manifest_path.parent))
    outputs = manifest.get("outputs", {})
    rtl_log = _load_log(run_dir, outputs, "rtl_commit_log", "rtl_commit.jsonl")
    iss_log = _load_log(run_dir, outputs, "iss_commit_log", "iss_commit.jsonl")
    if not rtl_log:
        logger.info("tlb_verifier: no RTL commit log, skipping")
        return 0

    phys_mem = manifest.get("phys_mem") or manifest.get("page_table")
    report = TLBVerifier(rtl_log, iss_log, phys_mem=phys_mem).run()

    report_path = run_dir / "tlb_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
        f.write("\n")

    manifest.setdefault("outputs", {})["tlb_report"] = "tlb_report.json"
    manifest.setdefault("phases", {})["tlb_check"] = {
        "status": "pass" if report["pass"] else "fail",
        "violations": report["total_violations"],
    }
    tmp = manifest_path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    tmp.rename(manifest_path)

    logger.info("tlb_verifier: %d translations, %d sfence, %d violations, band=%s",
                report["translations"], report["stats"]["sfence"],
                report["total_violations"], report["band"])
    return 0 if report["pass"] else 1


# ─────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":          # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description="TLB coherence / sfence.vma verifier")
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
        rep = TLBVerifier(log).run()
        print(json.dumps(rep, indent=2))
        raise SystemExit(0 if rep["pass"] else 1)
    ap.print_help()
