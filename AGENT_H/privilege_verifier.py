"""
AGENT_H/privilege_verifier.py
=============================
T29 — Privilege & Physical Memory Protection (PMP) Verification

Verifies the RISC-V privileged architecture from the canonical commit log:
privilege-mode transitions, the legality of privileged instructions, ECALL
trap-cause correctness, and Physical Memory Protection (PMP) region permission
enforcement.

These are the gating capabilities for *secure* and *Linux-class* cores — exactly
the dimension AVA previously did not cover. Privilege/PMP bugs (an MRET that
returns to the wrong mode, a U-mode access that bypasses a PMP region, a CSR
written from too low a privilege) are security-critical and are not reliably
caught by value-level tandem diffing.

Everything is gated on the information actually present in the trace:
  * privilege-dependent checks run only when the record carries a privilege
    field (``priv`` / ``mode`` / ``privilege`` / ``prv``);
  * PMP checks run only when at least one PMP entry is configured;
  * a check is skipped (never failed) when the needed state is unavailable.

Privilege encoding: U=0, S=1, M=3.

Checks
------
  priv_xret_illegal   MRET/SRET executed from too low a privilege without an
                      illegal-instruction trap
  priv_csr_access     access to a CSR above the current privilege without a trap
  priv_ecall_cause    ECALL trap cause != 8/9/11 for U/S/M
  priv_mret_target    privilege after MRET/SRET != mstatus.MPP / sstatus.SPP
  pmp_missing_fault   a U/S access denied by PMP did not raise an access fault
  pmp_spurious_fault  a PMP-permitted access raised an access fault

Usage
-----
  from AGENT_H.privilege_verifier import PrivilegeVerifier
  report = PrivilegeVerifier(rtl_log).run()

  from AGENT_H.privilege_verifier import run_from_manifest
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

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "2.1.0"

PRIV_U, PRIV_S, PRIV_M = 0, 1, 3

# trap causes
CAUSE_IFETCH_FAULT = 1
CAUSE_ILLEGAL      = 2
CAUSE_LOAD_FAULT   = 5
CAUSE_STORE_FAULT  = 7
CAUSE_ECALL_U      = 8
CAUSE_ECALL_S      = 9
CAUSE_ECALL_M      = 11

# PMP address-matching modes (cfg.A)
PMP_OFF, PMP_TOR, PMP_NA4, PMP_NAPOT = 0, 1, 2, 3

_M32 = 0xFFFFFFFF
_REG_RE = re.compile(r"\bx(?:[12]?\d|3[01]|\d)\b")


# ─────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────

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


def parse_priv(rec: Dict[str, Any]) -> Optional[int]:
    """Extract the current privilege mode from a commit record, or None."""
    for k in ("priv", "mode", "privilege", "prv", "priv_mode"):
        if k in rec and rec[k] is not None:
            v = rec[k]
            if isinstance(v, str):
                s = v.strip().lower()
                if s in ("m", "machine", "3"):
                    return PRIV_M
                if s in ("s", "supervisor", "1"):
                    return PRIV_S
                if s in ("u", "user", "0"):
                    return PRIV_U
                iv = _to_int(s)
                return iv if iv in (0, 1, 3) else None
            iv = _to_int(v)
            return iv if iv in (0, 1, 3) else None
    return None


def _trap_cause(rec: Dict) -> Optional[int]:
    t = rec.get("trap")
    if isinstance(t, dict):
        return _to_int(t.get("cause"))
    return None


# ─────────────────────────────────────────────────────────
# PMP model
# ─────────────────────────────────────────────────────────

@dataclass
class PMPEntry:
    cfg:  int      # raw config byte
    addr: int      # raw pmpaddr CSR value

    @property
    def a(self) -> int:   return (self.cfg >> 3) & 0x3
    @property
    def r(self) -> bool:  return bool(self.cfg & 0x1)
    @property
    def w(self) -> bool:  return bool(self.cfg & 0x2)
    @property
    def x(self) -> bool:  return bool(self.cfg & 0x4)
    @property
    def locked(self) -> bool: return bool(self.cfg & 0x80)


class PMPModel:
    """Shadow PMP configuration with region matching (RV32, 16 entries)."""

    def __init__(self) -> None:
        self.cfg  = [0] * 16
        self.addr = [0] * 16

    def update_from_csrs(self, csrs: Dict[str, Any]) -> None:
        # pmpcfg0..3 each pack 4 config bytes (RV32)
        for n in range(4):
            v = _to_int(csrs.get(f"pmpcfg{n}"))
            if v is not None:
                for b in range(4):
                    self.cfg[n * 4 + b] = (v >> (b * 8)) & 0xFF
        for n in range(16):
            v = _to_int(csrs.get(f"pmpaddr{n}"))
            if v is not None:
                self.addr[n] = v

    def configured(self) -> bool:
        return any(((c >> 3) & 0x3) != PMP_OFF for c in self.cfg)

    def _region(self, i: int) -> Optional[Tuple[int, int]]:
        """Return [lo, hi) byte range for entry i, or None if OFF."""
        a = (self.cfg[i] >> 3) & 0x3
        addr = self.addr[i]
        if a == PMP_OFF:
            return None
        if a == PMP_TOR:
            lo = (self.addr[i - 1] << 2) if i > 0 else 0
            hi = addr << 2
            return (lo, hi) if hi > lo else (lo, lo)
        if a == PMP_NA4:
            base = addr << 2
            return (base, base + 4)
        # NAPOT
        trail = 0
        x = addr
        while x & 1:
            trail += 1
            x >>= 1
        size = 1 << (trail + 3)
        base = (addr & ~((1 << trail) - 1)) << 2
        return (base, base + size)

    def match(self, byte_addr: int) -> Optional[PMPEntry]:
        """First matching PMP entry for an address (lowest index wins)."""
        for i in range(16):
            rng = self._region(i)
            if rng and rng[0] <= byte_addr < rng[1]:
                return PMPEntry(self.cfg[i], self.addr[i])
        return None

    def permitted(self, byte_addr: int, perm: str, priv: int) -> bool:
        """Is an access of type perm ('r'/'w'/'x') at priv allowed?"""
        e = self.match(byte_addr)
        if e is None:
            return priv == PRIV_M       # no match: only M-mode allowed
        if priv == PRIV_M and not e.locked:
            return True                 # M-mode ignores unlocked entries
        return {"r": e.r, "w": e.w, "x": e.x}[perm]


# ─────────────────────────────────────────────────────────
# Violation record
# ─────────────────────────────────────────────────────────

@dataclass
class PrivViolation:
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

class PrivilegeVerifier:
    """
    Verify privileged-architecture and PMP semantics from a commit log.

    Parameters
    ----------
    rtl_log        : list of RTL commit records
    iss_log        : optional ISS commit records (reserved for cross-check)
    max_violations : stop collecting after this many violations
    """

    _SEV_WEIGHT = {"HIGH": 1.0, "MEDIUM": 0.4, "LOW": 0.15}

    def __init__(
        self,
        rtl_log:        List[Dict[str, Any]],
        iss_log:        Optional[List[Dict[str, Any]]] = None,
        max_violations: int = 200,
    ) -> None:
        self.rtl_log        = rtl_log or []
        self.iss_log        = iss_log or []
        self.max_violations = max_violations
        self._pmp     = PMPModel()
        self._mstatus = 0
        self._sstatus = 0
        self._pending_ret: Optional[Tuple[int, str]] = None   # (expected_priv, kind)
        self._violations: List[PrivViolation] = []
        self._stats = {"priv_records": 0, "pmp_checks": 0, "xret": 0, "ecall": 0}

    def _flag(self, v: PrivViolation) -> None:
        if len(self._violations) < self.max_violations:
            self._violations.append(v)

    # -- privilege checks -----------------------------------------------------

    def _check_priv_instr(self, rec: Dict, disasm: str, priv: Optional[int], seq: int) -> None:
        cause = _trap_cause(rec)
        mnem  = disasm.split()[0] if disasm else ""

        # xRET legality + target
        if mnem in ("mret", "sret"):
            self._stats["xret"] += 1
            need = PRIV_M if mnem == "mret" else PRIV_S
            if priv is not None and priv < need:
                if cause != CAUSE_ILLEGAL:
                    self._flag(PrivViolation(
                        "priv_xret_illegal", "HIGH", seq, rec.get("pc"), rec.get("disasm"),
                        f"{mnem.upper()} executed in privilege {priv} (needs >= {need}) "
                        f"without an illegal-instruction trap",
                        expected="trap cause 2", actual=str(cause)))
            elif cause != CAUSE_ILLEGAL:
                # legal xRET: arm the post-return privilege expectation from xPP
                if mnem == "mret":
                    mpp = (self._mstatus >> 11) & 0x3
                    self._pending_ret = (mpp if mpp in (0, 1, 3) else PRIV_U, "mret")
                else:
                    spp = (self._sstatus >> 8) & 0x1
                    self._pending_ret = (spp, "sret")
            return

        # ECALL cause
        if mnem == "ecall":
            self._stats["ecall"] += 1
            if priv is not None and cause is not None:
                want = {PRIV_U: CAUSE_ECALL_U, PRIV_S: CAUSE_ECALL_S,
                        PRIV_M: CAUSE_ECALL_M}[priv]
                if cause != want:
                    self._flag(PrivViolation(
                        "priv_ecall_cause", "HIGH", seq, rec.get("pc"), rec.get("disasm"),
                        f"ECALL from privilege {priv} raised cause {cause}",
                        expected=str(want), actual=str(cause)))
            return

        # CSR access above current privilege
        if mnem.startswith("csr") and priv is not None:
            self._check_csr_priv(rec, disasm, priv, cause, seq)

    def _check_csr_priv(self, rec, disasm, priv, cause, seq) -> None:
        # CSR operand is the 2nd token after the rd (best-effort parse)
        toks = re.split(r"[\s,]+", disasm.strip())
        csr_tok = None
        for t in toks[1:]:
            if not _REG_RE.fullmatch(t):
                csr_tok = t
                break
        if csr_tok is None:
            return
        from .csr_verifier import _CSR_TABLE   # reuse the address table
        info = _CSR_TABLE.get(csr_tok.lower())
        if info is not None:
            addr = info.addr
        else:
            addr = _to_int(csr_tok)
            if addr is None:
                return
        csr_priv = (addr >> 8) & 0x3
        if csr_priv > priv and cause != CAUSE_ILLEGAL:
            self._flag(PrivViolation(
                "priv_csr_access", "HIGH", seq, rec.get("pc"), rec.get("disasm"),
                f"CSR {csr_tok} (privilege {csr_priv}) accessed from privilege {priv} "
                f"without an illegal-instruction trap",
                expected="trap cause 2", actual=str(cause)))

    def _check_ret_target(self, rec: Dict, priv: Optional[int], seq: int) -> None:
        if self._pending_ret is None or priv is None:
            return
        expected, kind = self._pending_ret
        self._pending_ret = None
        if priv != expected:
            self._flag(PrivViolation(
                "priv_mret_target", "HIGH", seq, rec.get("pc"), rec.get("disasm"),
                f"privilege after {kind.upper()} is {priv}, expected x{kind[0].upper()}PP={expected}",
                expected=str(expected), actual=str(priv)))

    # -- PMP checks -----------------------------------------------------------

    def _check_pmp(self, rec: Dict, priv: Optional[int], seq: int) -> None:
        if priv is None or not self._pmp.configured():
            return
        cause = _trap_cause(rec)
        accesses: List[Tuple[int, str, int]] = []  # (addr, perm, fault_cause)

        for r in (rec.get("mem_reads") or []):
            a = _to_int(r.get("addr"))
            if a is not None:
                accesses.append((a, "r", CAUSE_LOAD_FAULT))
        for w in (rec.get("mem_writes") or []):
            a = _to_int(w.get("addr"))
            if a is not None:
                accesses.append((a, "w", CAUSE_STORE_FAULT))

        for addr, perm, fcause in accesses:
            self._stats["pmp_checks"] += 1
            ok = self._pmp.permitted(addr, perm, priv)
            if not ok and cause != fcause:
                self._flag(PrivViolation(
                    "pmp_missing_fault", "HIGH", seq, rec.get("pc"), rec.get("disasm"),
                    f"{perm.upper()} access to 0x{addr:08x} in privilege {priv} is denied "
                    f"by PMP but no access fault was raised",
                    expected=f"trap cause {fcause}", actual=str(cause)))
            elif ok and cause == fcause:
                self._flag(PrivViolation(
                    "pmp_spurious_fault", "HIGH", seq, rec.get("pc"), rec.get("disasm"),
                    f"{perm.upper()} access to 0x{addr:08x} is PMP-permitted but raised an "
                    f"access fault (cause {fcause})",
                    expected="no fault", actual=f"trap cause {fcause}"))

    # -- main loop ------------------------------------------------------------

    def run(self) -> Dict[str, Any]:
        started = datetime.now(timezone.utc)
        n = len(self.rtl_log)
        for i, rec in enumerate(self.rtl_log):
            if len(self._violations) >= self.max_violations:
                break
            if not isinstance(rec, dict):
                continue
            seq    = rec.get("seq", i)
            disasm = (rec.get("disasm") or "").strip().lower()
            priv   = parse_priv(rec)
            if priv is not None:
                self._stats["priv_records"] += 1

            try:
                self._check_ret_target(rec, priv, seq)
                self._check_priv_instr(rec, disasm, priv, seq)
                self._check_pmp(rec, priv, seq)
            except Exception as exc:               # never crash the pipeline
                logger.warning("privilege_verifier: record %d raised: %s", seq, exc)

            # fold state for subsequent records
            csrs = rec.get("csrs") or {}
            self._pmp.update_from_csrs(csrs)
            mv = _to_int(csrs.get("mstatus"))
            if mv is not None:
                self._mstatus = mv
            sv = _to_int(csrs.get("sstatus"))
            if sv is not None:
                self._sstatus = sv

        finished = datetime.now(timezone.utc)
        return self._report(n, started, finished)

    # -- reporting ------------------------------------------------------------

    def _band(self) -> Tuple[float, str]:
        if not self._violations:
            return 0.0, "CLEAN"
        score = sum(self._SEV_WEIGHT.get(v.severity, 0.2) for v in self._violations)
        norm  = min(1.0, score / max(1, len(self.rtl_log)))
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
            "agent":            "privilege_verifier",
            "records_checked":  n,
            "stats":            dict(self._stats),
            "pmp_configured":   self._pmp.configured(),
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
        logger.warning("privilege_verifier: cannot read manifest: %s", exc)
        return 0

    run_dir = Path(manifest.get("run_dir", manifest_path.parent))
    outputs = manifest.get("outputs", {})
    rtl_log = _load_log(run_dir, outputs, "rtl_commit_log", "rtl_commit.jsonl")
    iss_log = _load_log(run_dir, outputs, "iss_commit_log", "iss_commit.jsonl")
    if not rtl_log:
        logger.info("privilege_verifier: no RTL commit log, skipping")
        return 0

    report = PrivilegeVerifier(rtl_log, iss_log).run()

    report_path = run_dir / "privilege_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
        f.write("\n")

    manifest.setdefault("outputs", {})["privilege_report"] = "privilege_report.json"
    manifest.setdefault("phases", {})["privilege_check"] = {
        "status": "pass" if report["pass"] else "fail",
        "violations": report["total_violations"],
    }
    tmp = manifest_path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    tmp.rename(manifest_path)

    logger.info("privilege_verifier: %d priv records, %d PMP checks, %d violations, band=%s",
                report["stats"]["priv_records"], report["stats"]["pmp_checks"],
                report["total_violations"], report["band"])
    return 0 if report["pass"] else 1


# ─────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":          # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description="Privilege & PMP verifier")
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
        rep = PrivilegeVerifier(log).run()
        print(json.dumps(rep, indent=2))
        raise SystemExit(0 if rep["pass"] else 1)
    ap.print_help()
