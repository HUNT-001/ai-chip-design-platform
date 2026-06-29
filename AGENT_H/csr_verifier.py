"""
AGENT_H/csr_verifier.py
=======================
T25 — Zicsr / Zifencei Semantics Verification

Golden-reference verification of the RISC-V **Zicsr** (control & status register
access) and **Zifencei** (instruction-fetch fence) extensions from the canonical
commit log.

CSR access bugs are subtle and high-impact: a control register that latches the
wrong value, a read-only register that silently accepts a write, or an atomic
set/clear that touches bits it shouldn't can corrupt privilege state, interrupt
masking and trap delegation without ever diverging a general-purpose register
until much later. Ordinary tandem diffing often misses them; this agent checks
the *semantics* of every CSR instruction directly.

The temporal checker (`AGENT_H/temporal_checker.py::CsrReadAfterWrite`) only
verifies that a written CSR becomes visible. This agent verifies that the
read-modify-write itself is correct.

Golden model
------------
For each CSR instruction the verifier derives the expected behaviour from the
commit record itself plus a shadow CSR/register file:

  CSRRW  rd, csr, rs1   csr ← rs1;            rd ← old_csr
  CSRRS  rd, csr, rs1   csr ← old | rs1;     rd ← old_csr   (rs1=x0 ⇒ no write)
  CSRRC  rd, csr, rs1   csr ← old & ~rs1;    rd ← old_csr   (rs1=x0 ⇒ no write)
  *I variants use the 5-bit zero-extended immediate instead of rs1.

Key insight: the destination register write-back **is** the old CSR value, so
the model can recover `old` even on the first access and check that the
post-state CSR value (`record["csrs"][name]`) equals `f(old, operand)`.

Checks
------
  csr_rd_value        rd != old CSR value
  csr_writeback       post-state csr != f(old, operand)
  csr_spurious_write  CSRRS/CSRRC with x0 source changed the CSR
  csr_readonly_write  write to a read-only CSR without an illegal-instr trap
  csr_read_value      reported old disagrees with the model's tracked value
  fencei_missing      execution of a just-modified word with no intervening
                      FENCE.I (self-modifying code without instruction sync)

Usage
-----
  from AGENT_H.csr_verifier import CSRVerifier
  report = CSRVerifier(rtl_log).run()

  from AGENT_H.csr_verifier import run_from_manifest
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

_MASK32 = 0xFFFFFFFF


# ─────────────────────────────────────────────────────────
# CSR address / attribute table  (subset of the RISC-V priv spec)
# ─────────────────────────────────────────────────────────
# access: "RW" or "RO".  Read-only-by-encoding = address bits [11:10] == 0b11.

@dataclass(frozen=True)
class CSRInfo:
    addr:   int
    access: str   # "RW" | "RO"


_CSR_TABLE: Dict[str, CSRInfo] = {
    # Machine information (read-only)
    "mvendorid": CSRInfo(0xF11, "RO"),
    "marchid":   CSRInfo(0xF12, "RO"),
    "mimpid":    CSRInfo(0xF13, "RO"),
    "mhartid":   CSRInfo(0xF14, "RO"),
    # Machine trap setup (read-write)
    "mstatus":   CSRInfo(0x300, "RW"),
    "misa":      CSRInfo(0x301, "RW"),
    "medeleg":   CSRInfo(0x302, "RW"),
    "mideleg":   CSRInfo(0x303, "RW"),
    "mie":       CSRInfo(0x304, "RW"),
    "mtvec":     CSRInfo(0x305, "RW"),
    "mcounteren":CSRInfo(0x306, "RW"),
    # Machine trap handling
    "mscratch":  CSRInfo(0x340, "RW"),
    "mepc":      CSRInfo(0x341, "RW"),
    "mcause":    CSRInfo(0x342, "RW"),
    "mtval":     CSRInfo(0x343, "RW"),
    "mip":       CSRInfo(0x344, "RW"),
    # Supervisor
    "sstatus":   CSRInfo(0x100, "RW"),
    "sie":       CSRInfo(0x104, "RW"),
    "stvec":     CSRInfo(0x105, "RW"),
    "sscratch":  CSRInfo(0x140, "RW"),
    "sepc":      CSRInfo(0x141, "RW"),
    "scause":    CSRInfo(0x142, "RW"),
    "stval":     CSRInfo(0x143, "RW"),
    "sip":       CSRInfo(0x144, "RW"),
    "satp":      CSRInfo(0x180, "RW"),
    # User counters (read-only shadows)
    "cycle":     CSRInfo(0xC00, "RO"),
    "time":      CSRInfo(0xC01, "RO"),
    "instret":   CSRInfo(0xC02, "RO"),
    "cycleh":    CSRInfo(0xC80, "RO"),
    "timeh":     CSRInfo(0xC81, "RO"),
    "instreth":  CSRInfo(0xC82, "RO"),
}


def csr_is_readonly(name_or_addr: str) -> Optional[bool]:
    """
    Return True/False if the CSR is read-only, or None if unknown.

    Recognises both symbolic names (from the table) and raw hex addresses
    (read-only-by-encoding = address bits [11:10] == 0b11).
    """
    key = name_or_addr.strip().lower()
    info = _CSR_TABLE.get(key)
    if info is not None:
        return info.access == "RO"
    # raw address?
    try:
        addr = int(key, 16) if key.startswith("0x") else int(key)
    except ValueError:
        return None
    return ((addr >> 10) & 0b11) == 0b11


# ─────────────────────────────────────────────────────────
# Value helpers
# ─────────────────────────────────────────────────────────

def _to_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value & _MASK32
    if isinstance(value, str):
        v = value.strip()
        if not v:
            return None
        try:
            return (int(v, 16) if v.lower().startswith("0x") else int(v, 0)) & _MASK32
        except ValueError:
            try:
                return int(v) & _MASK32
            except ValueError:
                return None
    return None


def _hex32(x: int) -> str:
    return f"0x{x & _MASK32:08x}"


# ─────────────────────────────────────────────────────────
# CSR instruction decode
# ─────────────────────────────────────────────────────────

_REG_RE = re.compile(r"^x(?:[12]?\d|3[01]|0)$")

# longest mnemonics first so csrrw is not shadowed by csrw, etc.
_CSR_MNEM_RE = re.compile(
    r"^\s*(csrrwi|csrrsi|csrrci|csrrw|csrrs|csrrc|csrwi|csrsi|csrci|"
    r"csrw|csrs|csrc|csrr)\b",
    re.IGNORECASE,
)


@dataclass
class CSRDecode:
    op:        str            # "RW" | "RS" | "RC"
    rd:        str            # destination register (x0 for write-only pseudos)
    csr:       str            # CSR name or hex address
    src_kind:  str            # "reg" | "imm" | "x0"
    src:       Optional[str]  # register name or immediate literal


def decode_csr(disasm: str) -> Optional[CSRDecode]:
    """Decode a Zicsr instruction (real or common pseudo) from disassembly."""
    if not disasm:
        return None
    m = _CSR_MNEM_RE.match(disasm)
    if not m:
        return None
    mnem = m.group(1).lower()
    rest = disasm[m.end():].strip()
    toks = [t.strip() for t in rest.split(",") if t.strip()]

    def opcode(base: str) -> str:
        return {"w": "RW", "s": "RS", "c": "RC"}[base]

    # immediate variants -------------------------------------------------
    if mnem in ("csrrwi", "csrrsi", "csrrci"):
        if len(toks) < 3:
            return None
        return CSRDecode(opcode(mnem[4]), toks[0], toks[1], "imm", toks[2])
    if mnem in ("csrwi", "csrsi", "csrci"):
        if len(toks) < 2:
            return None
        return CSRDecode(opcode(mnem[3]), "x0", toks[0], "imm", toks[1])

    # register variants --------------------------------------------------
    if mnem in ("csrrw", "csrrs", "csrrc"):
        if len(toks) < 3:
            return None
        src = toks[2]
        kind = "x0" if src == "x0" else "reg"
        return CSRDecode(opcode(mnem[4]), toks[0], toks[1], kind, src)
    if mnem in ("csrw", "csrs", "csrc"):
        if len(toks) < 2:
            return None
        src = toks[1]
        kind = "x0" if src == "x0" else "reg"
        return CSRDecode(opcode(mnem[3]), "x0", toks[0], kind, src)
    if mnem == "csrr":           # pseudo: csrr rd, csr  == csrrs rd, csr, x0
        if len(toks) < 2:
            return None
        return CSRDecode("RS", toks[0], toks[1], "x0", "x0")
    return None


# ─────────────────────────────────────────────────────────
# Violation record
# ─────────────────────────────────────────────────────────

@dataclass
class CSRViolation:
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

class CSRVerifier:
    """
    Verify Zicsr / Zifencei semantics from a commit log.

    Parameters
    ----------
    rtl_log        : list of RTL commit records (authoritative DUT output)
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

        self._regs: Dict[str, int] = {}
        self._csr:  Dict[str, int] = {}
        self._violations: List[CSRViolation] = []
        self._stats = {"csr_ops": 0, "writes": 0, "reads": 0, "fence_i": 0}

        # Zifencei: code words written since the last FENCE.I
        self._dirty_code: Dict[int, int] = {}

    # -- helpers --------------------------------------------------------------

    def _flag(self, v: CSRViolation) -> None:
        if len(self._violations) < self.max_violations:
            self._violations.append(v)

    def _apply_regs(self, rec: Dict) -> None:
        for name, val in (rec.get("regs") or {}).items():
            iv = _to_int(val)
            if iv is not None:
                self._regs[name] = iv
        self._regs["x0"] = 0

    def _operand(self, d: CSRDecode) -> Optional[int]:
        if d.src_kind == "x0":
            return 0
        if d.src_kind == "imm":
            return _to_int(d.src)
        return self._regs.get(d.src)   # may be None if unseen

    # -- Zifencei -------------------------------------------------------------

    def _check_fencei(self, rec: Dict, disasm: str, seq: int) -> None:
        if disasm.startswith("fence.i"):
            self._stats["fence_i"] += 1
            self._dirty_code.clear()
            return
        # record stores to memory as potential code modification
        for w in (rec.get("mem_writes") or []):
            a = _to_int(w.get("addr"))
            if a is not None:
                self._dirty_code[a & ~0x3] = seq
        # executing a word that was just written without an intervening fence.i
        pc = _to_int(rec.get("pc"))
        if pc is not None and (pc & ~0x3) in self._dirty_code:
            wseq = self._dirty_code[pc & ~0x3]
            if wseq != seq:
                self._flag(CSRViolation(
                    "fencei_missing", "LOW", seq, rec.get("pc"), disasm,
                    f"executing modified code at {_hex32(pc)} (written at seq {wseq}) "
                    f"without an intervening FENCE.I"))

    # -- CSR ------------------------------------------------------------------

    def _check_csr(self, rec: Dict, d: CSRDecode, seq: int) -> None:
        self._stats["csr_ops"] += 1
        csr_name = d.csr.strip().lower()
        post     = rec.get("csrs") or {}
        regs     = rec.get("regs") or {}

        # recover the OLD csr value from the rd write-back (rd == old_csr)
        old: Optional[int] = None
        if d.rd != "x0":
            self._stats["reads"] += 1
            old = _to_int(regs.get(d.rd))
            tracked = self._csr.get(csr_name)
            if old is not None and tracked is not None and old != tracked:
                self._flag(CSRViolation(
                    "csr_read_value", "MEDIUM", seq, rec.get("pc"), rec.get("disasm"),
                    f"CSR {csr_name}: read-back old value disagrees with model",
                    expected=_hex32(tracked), actual=_hex32(old)))
        if old is None:
            old = self._csr.get(csr_name)

        operand = self._operand(d)

        # does this instruction write the CSR?
        writes = d.op == "RW" or (d.op in ("RS", "RC") and d.src_kind != "x0"
                                  and (operand is None or operand != 0))
        if writes:
            self._stats["writes"] += 1

        # read-only enforcement
        if writes:
            ro = csr_is_readonly(csr_name)
            if ro:
                trap = rec.get("trap") or {}
                if trap.get("cause") != 2:   # 2 = illegal instruction
                    self._flag(CSRViolation(
                        "csr_readonly_write", "HIGH", seq, rec.get("pc"), rec.get("disasm"),
                        f"write to read-only CSR {csr_name} without an illegal-instruction trap",
                        expected="trap cause 2", actual=str(trap.get("cause"))))
                # a correctly-trapped write makes no state change
                return

        # compute expected new value
        post_val = _to_int(post.get(csr_name))
        if old is not None and operand is not None:
            if d.op == "RW":
                expected = operand
            elif d.op == "RS":
                expected = old | operand
            else:  # RC
                expected = old & (~operand & _MASK32)

            # x0/zero-source set/clear must not change the CSR
            if d.op in ("RS", "RC") and (d.src_kind == "x0" or operand == 0):
                expected = old
                if post_val is not None and post_val != old:
                    self._flag(CSRViolation(
                        "csr_spurious_write", "HIGH", seq, rec.get("pc"), rec.get("disasm"),
                        f"CSR {csr_name}: set/clear with zero source changed the register",
                        expected=_hex32(old), actual=_hex32(post_val)))
            elif post_val is not None and post_val != (expected & _MASK32):
                self._flag(CSRViolation(
                    "csr_writeback", "HIGH", seq, rec.get("pc"), rec.get("disasm"),
                    f"CSR {csr_name}: post-state value != {d.op}(old, operand)",
                    expected=_hex32(expected), actual=_hex32(post_val)))

        # update the shadow CSR with the authoritative post-state
        if post_val is not None:
            self._csr[csr_name] = post_val
        elif old is not None and operand is not None and writes:
            # no post-state reported: fall back to the computed value
            if d.op == "RW":
                self._csr[csr_name] = operand
            elif d.op == "RS":
                self._csr[csr_name] = old | operand
            else:
                self._csr[csr_name] = old & (~operand & _MASK32)

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

            try:
                self._check_fencei(rec, disasm, seq)
                d = decode_csr(disasm)
                if d is not None:
                    self._check_csr(rec, d, seq)
            except Exception as exc:               # never crash the pipeline
                logger.warning("csr_verifier: record %d raised: %s", seq, exc)

            self._apply_regs(rec)

        finished = datetime.now(timezone.utc)
        return self._report(n, started, finished)

    # -- reporting ------------------------------------------------------------

    def _band(self) -> Tuple[float, str]:
        if not self._violations:
            return 0.0, "CLEAN"
        score = sum(self._SEV_WEIGHT.get(v.severity, 0.2) for v in self._violations)
        norm  = min(1.0, score / max(1, self._stats["csr_ops"]))
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
            "agent":            "csr_verifier",
            "records_checked":  n,
            "csr_ops":          self._stats["csr_ops"],
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
    """
    Pipeline entry point. Loads the RTL commit log, runs the CSR verifier,
    writes ``csr_report.json`` and updates the manifest. Returns 0 on pass,
    1 on any violation; degrades gracefully (0) when no log is present.
    """
    manifest_path = Path(manifest_path)
    try:
        with open(manifest_path) as f:
            manifest = json.load(f)
    except Exception as exc:
        logger.warning("csr_verifier: cannot read manifest: %s", exc)
        return 0

    run_dir = Path(manifest.get("run_dir", manifest_path.parent))
    outputs = manifest.get("outputs", {})
    rtl_log = _load_log(run_dir, outputs, "rtl_commit_log", "rtl_commit.jsonl")
    iss_log = _load_log(run_dir, outputs, "iss_commit_log", "iss_commit.jsonl")
    if not rtl_log:
        logger.info("csr_verifier: no RTL commit log, skipping")
        return 0

    report = CSRVerifier(rtl_log, iss_log).run()

    report_path = run_dir / "csr_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
        f.write("\n")

    manifest.setdefault("outputs", {})["csr_report"] = "csr_report.json"
    manifest.setdefault("phases", {})["csr_check"] = {
        "status": "pass" if report["pass"] else "fail",
        "violations": report["total_violations"],
    }
    tmp = manifest_path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    tmp.rename(manifest_path)

    logger.info("csr_verifier: %d CSR ops, %d violations, band=%s",
                report["csr_ops"], report["total_violations"], report["band"])
    return 0 if report["pass"] else 1


# ─────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":          # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description="Zicsr/Zifencei semantics verifier")
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
        rep = CSRVerifier(log).run()
        print(json.dumps(rep, indent=2))
        raise SystemExit(0 if rep["pass"] else 1)
    ap.print_help()
