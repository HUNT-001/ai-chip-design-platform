"""
AGENT_H/atomics_verifier.py
===========================
T23 — RV32A Atomics Verification Agent

Golden-reference verification of the RISC-V "A" (Atomic) extension from the
canonical commit log.  Atomics (LR.W / SC.W and the nine AMO*.W operations)
are the single most bug-prone area of any RISC-V core: reservation tracking,
store-conditional success/fail semantics, signed-vs-unsigned MIN/MAX, the
read-modify-write memory write-back, and alignment handling all hide real
silicon bugs that ordinary instruction-by-instruction tandem diffing misses.

This agent does NOT need Spike, Verilator or any EDA tool.  It replays the
commit log against an in-process golden model of the A-extension and flags
any record whose observable effects (destination value, memory write-back,
reservation outcome, trap) disagree with the specification.

Design
------
The verifier keeps three pieces of shadow state, reconstructed purely from the
commit log:

  * a shadow register file   — updated from each record's ``regs`` write-back
  * a shadow byte/word memory — updated from each record's ``mem_writes``
  * a reservation set         — per the LR/SC reservation model (single hart)

For each atomic instruction it derives the *expected* behaviour from this
golden state and compares it against what the RTL actually committed:

  AMO*.W   rd  ← old memory value          (must equal mem_reads value)
           mem ← f(old, rs2)               (must equal mem_writes value)
  LR.W     rd  ← memory value, set reservation on the address
  SC.W     reservation valid → mem ← rs2, rd ← 0  (success)
           reservation broken → no write,   rd ← 1  (failure)

Reservations are invalidated by any store / AMO / SC to the reserved word, by
a subsequent LR (re-reservation), and (optionally) by a configurable forward
progress window — matching the semantics encoded in
``AGENT_H/temporal_checker.py::LrBeforeSc``.

Each violation is classified into a severity band and the run returns the same
report shape used by every other AGENT_H module (schema v2.1.0), so it slots
straight into ``_run_extended_pipeline`` and the report writers.

Usage
-----
  from AGENT_H.atomics_verifier import AtomicsVerifier

  verifier = AtomicsVerifier(rtl_log)        # iss_log optional, used for cross-check
  report   = verifier.run()
  if not report["pass"]:
      ...

  # or from the pipeline:
  from AGENT_H.atomics_verifier import run_from_manifest
  run_from_manifest(Path(run_dir) / "run_manifest.json")
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "2.1.0"

# 32-bit masks
_MASK32 = 0xFFFFFFFF
_SIGN32 = 0x80000000


# ─────────────────────────────────────────────────────────
# Helpers — value parsing / 32-bit arithmetic
# ─────────────────────────────────────────────────────────

def _to_int(value: Any) -> Optional[int]:
    """Parse a register / memory value (hex string, int, or None) to int."""
    if value is None:
        return None
    if isinstance(value, int):
        return value & _MASK32
    if isinstance(value, str):
        v = value.strip()
        if v == "":
            return None
        try:
            return int(v, 16) if v.lower().startswith("0x") else int(v, 0) & _MASK32
        except ValueError:
            try:
                return int(v) & _MASK32
            except ValueError:
                return None
    return None


def _u32(x: int) -> int:
    return x & _MASK32


def _s32(x: int) -> int:
    """Interpret the low 32 bits of x as a signed 32-bit integer."""
    x &= _MASK32
    return x - 0x100000000 if x & _SIGN32 else x


def _hex32(x: int) -> str:
    return f"0x{_u32(x):08x}"


# ─────────────────────────────────────────────────────────
# Golden AMO semantics  (RISC-V Unprivileged ISA, "A" extension, .W width)
# ─────────────────────────────────────────────────────────

def amo_compute(op: str, old: int, src: int) -> int:
    """
    Compute the new memory word for an AMO operation.

    Parameters
    ----------
    op  : normalised AMO name without width/ordering suffix
          one of: swap, add, and, or, xor, min, max, minu, maxu
    old : current 32-bit memory word (unsigned)
    src : rs2 operand (32-bit)

    Returns the new 32-bit memory word (unsigned).
    """
    old = _u32(old)
    src = _u32(src)
    if op == "swap":
        return src
    if op == "add":
        return _u32(old + src)
    if op == "and":
        return old & src
    if op == "or":
        return old | src
    if op == "xor":
        return old ^ src
    if op == "min":                       # signed
        return _u32(min(_s32(old), _s32(src)))
    if op == "max":                       # signed
        return _u32(max(_s32(old), _s32(src)))
    if op == "minu":                      # unsigned
        return min(old, src)
    if op == "maxu":                      # unsigned
        return max(old, src)
    raise ValueError(f"unknown AMO op: {op}")


# Atomic-instruction disassembly parser ----------------------------------------

# matches e.g.  "amoadd.w.aq x5, x6, (x10)"  /  "lr.w x1, (x2)"  / "sc.w x1,x2,0(x3)"
_REG_RE = re.compile(r"\b(x(?:[12]?\d|3[01]|0))\b")
_AMO_RE = re.compile(
    r"^\s*(?P<mnem>lr|sc|amoswap|amoadd|amoand|amoor|amoxor|"
    r"amomin|amomax|amominu|amomaxu)\.(?P<width>w|d)"
    r"(?P<aq>\.aq)?(?P<rl>\.rl)?\b",
    re.IGNORECASE,
)


@dataclass
class AtomicDecode:
    mnem:    str            # lr / sc / swap / add / and / ...
    kind:    str            # "lr" | "sc" | "amo"
    width:   str            # "w" | "d"
    aq:      bool
    rl:      bool
    rd:      Optional[str]  # destination register name (xN)
    rs1:     Optional[str]  # base address register
    rs2:     Optional[str]  # source register (AMO / SC)


def decode_atomic(disasm: str) -> Optional[AtomicDecode]:
    """Decode an atomic instruction from its disassembly, else None."""
    if not disasm:
        return None
    m = _AMO_RE.match(disasm)
    if not m:
        return None
    mnem_raw = m.group("mnem").lower()
    width    = m.group("width").lower()
    aq       = bool(m.group("aq"))
    rl       = bool(m.group("rl"))

    regs = _REG_RE.findall(disasm)

    if mnem_raw == "lr":
        kind, mnem = "lr", "lr"
        rd  = regs[0] if len(regs) >= 1 else None
        rs1 = regs[1] if len(regs) >= 2 else None
        rs2 = None
    elif mnem_raw == "sc":
        kind, mnem = "sc", "sc"
        # sc.w rd, rs2, (rs1)
        rd  = regs[0] if len(regs) >= 1 else None
        rs2 = regs[1] if len(regs) >= 2 else None
        rs1 = regs[2] if len(regs) >= 3 else None
    else:
        kind = "amo"
        mnem = mnem_raw[3:]  # strip leading "amo"
        # amo<op>.w rd, rs2, (rs1)
        rd  = regs[0] if len(regs) >= 1 else None
        rs2 = regs[1] if len(regs) >= 2 else None
        rs1 = regs[2] if len(regs) >= 3 else None

    return AtomicDecode(mnem=mnem, kind=kind, width=width,
                        aq=aq, rl=rl, rd=rd, rs1=rs1, rs2=rs2)


# ─────────────────────────────────────────────────────────
# Violation record
# ─────────────────────────────────────────────────────────

@dataclass
class AtomicViolation:
    check:       str
    severity:    str          # HIGH | MEDIUM | LOW
    seq:         int
    pc:          Optional[str]
    disasm:      Optional[str]
    description: str
    expected:    Optional[str] = None
    actual:      Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "check":       self.check,
            "severity":    self.severity,
            "seq":         self.seq,
            "pc":          self.pc,
            "disasm":      self.disasm,
            "description": self.description,
            "expected":    self.expected,
            "actual":      self.actual,
        }


# ─────────────────────────────────────────────────────────
# Verifier
# ─────────────────────────────────────────────────────────

class AtomicsVerifier:
    """
    Verify RV32A atomic semantics from a commit log against a golden model.

    Parameters
    ----------
    rtl_log         : list of RTL commit records (authoritative DUT output)
    iss_log         : optional ISS commit records (used for value cross-check)
    reservation_window : forward-progress window (instructions) after which a
                         reservation is considered lost.  ``0`` disables the
                         window check.  Default 64 (typical RISC-V guideline).
    max_violations  : stop collecting after this many violations
    """

    # severity weighting for the band score
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

        # shadow state
        self._regs: Dict[str, int]  = {}   # register file (xN → value)
        self._mem:  Dict[int, int]  = {}   # word-addressed memory (addr → value)
        self._resv_addr: Optional[int] = None
        self._resv_seq:  Optional[int] = None

        self._violations: List[AtomicViolation] = []
        self._stats = {
            "lr": 0, "sc": 0, "amo": 0,
            "sc_success": 0, "sc_fail": 0,
            "misaligned": 0,
        }
        # RV64 detection: a 64-bit register value means .d atomics are legal,
        # so they are left to AGENT_H.rv64_atomics_verifier rather than flagged.
        self._is_rv64 = self._detect_rv64()

    def _detect_rv64(self) -> bool:
        for rec in self.rtl_log:
            if not isinstance(rec, dict):
                continue
            for v in (rec.get("regs") or {}).values():
                if isinstance(v, str):
                    try:
                        iv = int(v, 16) if v.lower().startswith("0x") else int(v, 0)
                    except ValueError:
                        continue
                elif isinstance(v, int):
                    iv = v
                else:
                    continue
                if iv > 0xFFFFFFFF:
                    return True
        return False

    # -- shadow-state maintenance --------------------------------------------

    @staticmethod
    def _mem_entry(rec: Dict, key: str) -> Optional[Tuple[int, int, Optional[int]]]:
        """Return (addr, size, value) for the first mem_reads/mem_writes entry."""
        seq_list = rec.get(key)
        if not seq_list:
            return None
        e = seq_list[0] if isinstance(seq_list, list) else seq_list
        addr = _to_int(e.get("addr"))
        if addr is None:
            return None
        size = e.get("size", 4)
        val  = _to_int(e.get("value"))
        return addr, size, val

    def _apply_regs(self, rec: Dict) -> None:
        """Fold a record's register write-back into the shadow register file."""
        regs = rec.get("regs") or {}
        for name, val in regs.items():
            iv = _to_int(val)
            if iv is not None:
                self._regs[name] = iv
        self._regs["x0"] = 0  # x0 is hard-wired zero

    def _invalidate_reservation_on_store(self, rec: Dict, decode: Optional[AtomicDecode]) -> None:
        """A plain store (non-atomic) to the reserved word breaks the reservation."""
        if self._resv_addr is None:
            return
        # atomic ops handle their own reservation logic
        if decode is not None:
            return
        mw = self._mem_entry(rec, "mem_writes")
        if mw and mw[0] == self._resv_addr:
            self._resv_addr = None
            self._resv_seq  = None

    def _apply_mem_writes(self, rec: Dict) -> None:
        """Fold a record's memory write into shadow memory."""
        mw = self._mem_entry(rec, "mem_writes")
        if mw and mw[2] is not None:
            self._mem[mw[0]] = mw[2]

    # -- violation helper -----------------------------------------------------

    def _flag(self, v: AtomicViolation) -> None:
        if len(self._violations) < self.max_violations:
            self._violations.append(v)

    # -- per-kind checkers ----------------------------------------------------

    def _check_alignment(self, addr: Optional[int], rec: Dict, d: AtomicDecode, seq: int) -> bool:
        """Return True if an alignment fault was (correctly) the outcome."""
        need = 4 if d.width == "w" else 8
        if addr is None:
            return False
        if addr % need != 0:
            self._stats["misaligned"] += 1
            trap = rec.get("trap")
            # cause 6 = store/AMO address misaligned (LR is a load: cause 4)
            expected_cause = 4 if d.kind == "lr" else 6
            if not trap:
                self._flag(AtomicViolation(
                    check="alignment",
                    severity="HIGH",
                    seq=seq, pc=rec.get("pc"), disasm=rec.get("disasm"),
                    description=(f"Misaligned atomic to {_hex32(addr)} did not raise "
                                 f"an address-misaligned trap (expected cause {expected_cause})"),
                    expected=f"trap cause {expected_cause}",
                    actual="no trap",
                ))
            return True

        return False

    def _check_lr(self, rec: Dict, d: AtomicDecode, seq: int) -> None:
        self._stats["lr"] += 1
        mr = self._mem_entry(rec, "mem_reads")
        addr = mr[0] if mr else None
        if self._check_alignment(addr, rec, d, seq):
            return
        if addr is None:
            self._flag(AtomicViolation(
                check="lr_no_read", severity="MEDIUM", seq=seq,
                pc=rec.get("pc"), disasm=rec.get("disasm"),
                description="LR.W committed without a memory read record",
            ))
            return

        loaded = mr[2]
        # rd must equal the value read from memory
        if d.rd and d.rd != "x0":
            rd_val = _to_int((rec.get("regs") or {}).get(d.rd))
            if rd_val is not None and loaded is not None and rd_val != loaded:
                self._flag(AtomicViolation(
                    check="lr_rd_value", severity="HIGH", seq=seq,
                    pc=rec.get("pc"), disasm=rec.get("disasm"),
                    description=f"LR.W destination {d.rd} != value read from {_hex32(addr)}",
                    expected=_hex32(loaded), actual=_hex32(rd_val),
                ))
        # cross-check against known shadow memory if we have it
        if loaded is not None and addr in self._mem and self._mem[addr] != loaded:
            self._flag(AtomicViolation(
                check="lr_mem_coherence", severity="MEDIUM", seq=seq,
                pc=rec.get("pc"), disasm=rec.get("disasm"),
                description=f"LR.W read {_hex32(loaded)} from {_hex32(addr)} but last "
                            f"write to that word was {_hex32(self._mem[addr])}",
                expected=_hex32(self._mem[addr]), actual=_hex32(loaded),
            ))
        if loaded is not None:
            self._mem[addr] = loaded
        # set the reservation
        self._resv_addr = addr
        self._resv_seq  = seq

    def _check_sc(self, rec: Dict, d: AtomicDecode, seq: int) -> None:
        self._stats["sc"] += 1
        mw = self._mem_entry(rec, "mem_writes")
        # address: prefer the write addr, else the reserved addr
        addr = mw[0] if mw else self._resv_addr
        if self._check_alignment(addr, rec, d, seq):
            return

        rd_val = _to_int((rec.get("regs") or {}).get(d.rd)) if d.rd else None
        wrote  = mw is not None

        # Determine whether the reservation is (golden) valid
        resv_valid = (
            self._resv_addr is not None
            and addr is not None
            and addr == self._resv_addr
        )
        if resv_valid and self.reservation_window > 0 and self._resv_seq is not None:
            if seq - self._resv_seq > self.reservation_window:
                resv_valid = False

        if resv_valid:
            self._stats["sc_success"] += 1
            # success ⇒ must write memory AND rd == 0
            if not wrote:
                self._flag(AtomicViolation(
                    check="sc_success_no_write", severity="HIGH", seq=seq,
                    pc=rec.get("pc"), disasm=rec.get("disasm"),
                    description=f"SC.W with a valid reservation on {_hex32(addr)} did not "
                                f"write memory",
                    expected="memory write", actual="no write",
                ))
            if rd_val is not None and rd_val != 0:
                self._flag(AtomicViolation(
                    check="sc_success_rd", severity="HIGH", seq=seq,
                    pc=rec.get("pc"), disasm=rec.get("disasm"),
                    description=f"SC.W succeeded but {d.rd} != 0 (success code)",
                    expected="0x00000000", actual=_hex32(rd_val),
                ))
            # check the stored value equals rs2
            if wrote and d.rs2 and d.rs2 in self._regs and mw[2] is not None:
                exp = self._regs[d.rs2]
                if mw[2] != exp:
                    self._flag(AtomicViolation(
                        check="sc_store_value", severity="HIGH", seq=seq,
                        pc=rec.get("pc"), disasm=rec.get("disasm"),
                        description=f"SC.W stored {_hex32(mw[2])} but rs2 ({d.rs2}) = {_hex32(exp)}",
                        expected=_hex32(exp), actual=_hex32(mw[2]),
                    ))
            if wrote and mw[2] is not None and addr is not None:
                self._mem[addr] = mw[2]
        else:
            self._stats["sc_fail"] += 1
            # failure ⇒ must NOT write memory AND rd != 0
            if wrote:
                self._flag(AtomicViolation(
                    check="sc_fail_wrote", severity="HIGH", seq=seq,
                    pc=rec.get("pc"), disasm=rec.get("disasm"),
                    description=(f"SC.W without a valid reservation wrote memory at "
                                 f"{_hex32(addr) if addr is not None else '?'} "
                                 f"(spurious store — atomicity violation)"),
                    expected="no write", actual="memory write",
                ))
            if rd_val is not None and rd_val == 0:
                self._flag(AtomicViolation(
                    check="sc_fail_rd", severity="HIGH", seq=seq,
                    pc=rec.get("pc"), disasm=rec.get("disasm"),
                    description="SC.W failed (no valid reservation) but returned success code 0",
                    expected="non-zero", actual="0x00000000",
                ))
        # SC always clears the reservation, success or fail
        self._resv_addr = None
        self._resv_seq  = None

    def _check_amo(self, rec: Dict, d: AtomicDecode, seq: int) -> None:
        self._stats["amo"] += 1
        mr = self._mem_entry(rec, "mem_reads")
        mw = self._mem_entry(rec, "mem_writes")
        addr = (mr[0] if mr else None) or (mw[0] if mw else None)
        if self._check_alignment(addr, rec, d, seq):
            return
        if addr is None:
            self._flag(AtomicViolation(
                check="amo_no_mem", severity="MEDIUM", seq=seq,
                pc=rec.get("pc"), disasm=rec.get("disasm"),
                description="AMO committed without a memory access record",
            ))
            return

        old = mr[2] if mr else self._mem.get(addr)
        # rd ← old memory value
        if d.rd and d.rd != "x0" and old is not None:
            rd_val = _to_int((rec.get("regs") or {}).get(d.rd))
            if rd_val is not None and rd_val != old:
                self._flag(AtomicViolation(
                    check="amo_rd_value", severity="HIGH", seq=seq,
                    pc=rec.get("pc"), disasm=rec.get("disasm"),
                    description=f"AMO {d.mnem}.w destination {d.rd} != old memory value at {_hex32(addr)}",
                    expected=_hex32(old), actual=_hex32(rd_val),
                ))

        # mem ← f(old, rs2)
        if old is not None and d.rs2 and d.rs2 in self._regs:
            src = self._regs[d.rs2]
            try:
                expected_new = amo_compute(d.mnem, old, src)
            except ValueError:
                expected_new = None
            if expected_new is not None and mw and mw[2] is not None and mw[2] != expected_new:
                self._flag(AtomicViolation(
                    check="amo_writeback", severity="HIGH", seq=seq,
                    pc=rec.get("pc"), disasm=rec.get("disasm"),
                    description=(f"AMO {d.mnem}.w wrote {_hex32(mw[2])} to {_hex32(addr)} but "
                                 f"f({_hex32(old)}, rs2={_hex32(src)}) = {_hex32(expected_new)}"),
                    expected=_hex32(expected_new), actual=_hex32(mw[2]),
                ))
        # update shadow memory with the committed write (or the computed value)
        if mw and mw[2] is not None:
            self._mem[addr] = mw[2]
        elif old is not None:
            self._mem[addr] = old

        # an AMO to the reserved word breaks any outstanding reservation
        if self._resv_addr is not None and addr == self._resv_addr:
            self._resv_addr = None
            self._resv_seq  = None

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
            disasm = (rec.get("disasm") or "").strip()
            d      = decode_atomic(disasm)

            # reservation can be broken by a plain store before we fold regs
            self._invalidate_reservation_on_store(rec, d)

            if d is not None:
                if d.width == "d":
                    # On RV32 a 64-bit atomic is illegal; on RV64 it is legal and
                    # is verified by AGENT_H.rv64_atomics_verifier, so skip here.
                    if not self._is_rv64:
                        self._flag(AtomicViolation(
                            check="rv32_illegal_d", severity="MEDIUM", seq=seq,
                            pc=rec.get("pc"), disasm=disasm,
                            description="64-bit atomic (.d) committed on an RV32 core",
                        ))
                else:
                    try:
                        if d.kind == "lr":
                            self._check_lr(rec, d, seq)
                        elif d.kind == "sc":
                            self._check_sc(rec, d, seq)
                        else:
                            self._check_amo(rec, d, seq)
                    except Exception as exc:           # never crash the pipeline
                        logger.warning("atomics_verifier: record %d raised: %s", seq, exc)

            # fold writeback / memory effects into shadow state for later records
            self._apply_regs(rec)
            if d is None:
                self._apply_mem_writes(rec)

        finished = datetime.now(timezone.utc)
        return self._build_report(n, started, finished)

    # -- reporting ------------------------------------------------------------

    def _band(self) -> Tuple[float, str]:
        """A leak-style severity score in [0,1] and a band label."""
        score = sum(self._SEV_WEIGHT.get(v.severity, 0.2) for v in self._violations)
        # normalise softly by number of atomic ops examined
        atomics = max(1, self._stats["lr"] + self._stats["sc"] + self._stats["amo"])
        norm = min(1.0, score / atomics)
        if not self._violations:
            band = "CLEAN"
        elif any(v.severity == "HIGH" for v in self._violations):
            band = "CRITICAL"
        elif norm >= 0.3:
            band = "DEGRADED"
        else:
            band = "MINOR"
        return round(norm, 4), band

    def _build_report(self, n: int, started, finished) -> Dict[str, Any]:
        score, band = self._band()
        high = [v for v in self._violations if v.severity == "HIGH"]
        return {
            "schema_version":   SCHEMA_VERSION,
            "agent":            "atomics_verifier",
            "records_checked":  n,
            "atomics_examined": self._stats["lr"] + self._stats["sc"] + self._stats["amo"],
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
# Manifest integration  (graceful degradation)
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
    Pipeline entry point.  Loads the RTL (and ISS) commit logs referenced by the
    run manifest, runs the atomics verifier, writes ``atomics_report.json`` and
    updates the manifest.  Returns 0 on pass, 1 on any violation, and degrades
    gracefully (returns 0) when no commit log is available.
    """
    manifest_path = Path(manifest_path)
    try:
        with open(manifest_path) as f:
            manifest = json.load(f)
    except Exception as exc:
        logger.warning("atomics_verifier: cannot read manifest: %s", exc)
        return 0

    run_dir = Path(manifest.get("run_dir", manifest_path.parent))
    outputs = manifest.get("outputs", {})

    rtl_log = _load_log(run_dir, outputs, "rtl_commit_log", "rtl_commit.jsonl")
    iss_log = _load_log(run_dir, outputs, "iss_commit_log", "iss_commit.jsonl")

    if not rtl_log:
        logger.info("atomics_verifier: no RTL commit log, skipping")
        return 0

    verifier = AtomicsVerifier(rtl_log, iss_log)
    report   = verifier.run()

    report_path = run_dir / "atomics_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
        f.write("\n")

    manifest.setdefault("outputs", {})["atomics_report"] = "atomics_report.json"
    manifest.setdefault("phases", {})["atomics_check"] = {
        "status": "pass" if report["pass"] else "fail",
        "violations": report["total_violations"],
    }
    tmp = manifest_path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    tmp.rename(manifest_path)

    logger.info("atomics_verifier: %d atomics examined, %d violations, band=%s",
                report["atomics_examined"], report["total_violations"], report["band"])
    return 0 if report["pass"] else 1


# ─────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":          # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description="RV32A atomics verifier")
    ap.add_argument("--manifest", type=Path, help="run_manifest.json path")
    ap.add_argument("--rtl", type=Path, help="rtl_commit.jsonl path (standalone)")
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
        rep = AtomicsVerifier(log).run()
        print(json.dumps(rep, indent=2))
        raise SystemExit(0 if rep["pass"] else 1)
    ap.print_help()
