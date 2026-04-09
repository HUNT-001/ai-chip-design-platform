"""
sim/spike_parser.py
===================
Agent C — Spike Log Parser

Converts raw Spike stderr/stdout into schema-compliant commit-log records
(matching schemas/commitlog.schema.json).

Spike emits two distinct log formats depending on flags and version:

  FORMAT A  (-l only, all Spike versions)
  ────────────────────────────────────────
  core   0: 0x80000000 (0x00000297) auipc   t0, 0x0

  FORMAT B  (--log-commits / --enable-commitlog, Spike >= 1.1)
  ──────────────────────────────────────────────────────────────
  core   0: 3 0x80000000 (0x00000297) x5  0x80000000
  core   0: 3 0x80000004 (0x00002223) mem 0x80002000 0x00000000
  core   0: exception load_access_fault, epc 0x80000010

  FORMAT B extended (some builds emit both instruction + writeback on same line)
  ────────────────────────────────────────────────────────────────────────────────
  core   0: 3 0x80000000 (0x00000297) auipc   t0, 0x0 x5 0x80000000

The parser auto-detects the format and merges multi-line commit records
(FORMAT B emits a writeback line *for the previous instruction*).

Privilege encoding in FORMAT B:
  0 = U, 1 = S, 3 = M (Spike uses 2-bit privilege field)
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from typing import Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────
# AVA Schema v2.0.0 constants
# ─────────────────────────────────────────────────────────
SCHEMA_VERSION = "2.0.0"

# Field name map: internal name → wire name emitted in JSONL.
# Parser logic uses internal names throughout; only serialisation changes.
_FIELD_RENAMES = {
    # internal      → wire (AVA 2.0.0 contract)
    "source":       "src",        # "iss" / "rtl" / "formal"
    "reg_writes":   "regs",       # [{rd, value}]  — values preserved
    "csr_writes":   "csrs",       # [{addr, name?, value}]
    "mem_access":   "mem",        # {type, addr, size, value}
}

# ─────────────────────────────────────────────────────────
# ABI register name → integer index
# ─────────────────────────────────────────────────────────
_ABI_TO_IDX: dict[str, int] = {
    "zero": 0,  "ra": 1,   "sp": 2,   "gp": 3,
    "tp":   4,  "t0": 5,   "t1": 6,   "t2": 7,
    "s0":   8,  "fp": 8,   "s1": 9,   "a0": 10,
    "a1":   11, "a2": 12,  "a3": 13,  "a4": 14,
    "a5":   15, "a6": 16,  "a7": 17,  "s2": 18,
    "s3":   19, "s4": 20,  "s5": 21,  "s6": 22,
    "s7":   23, "s8": 24,  "s9": 25,  "s10": 26,
    "s11":  27, "t3": 28,  "t4": 29,  "t5": 30,
    "t6":   31,
}

# Known CSR addresses → mnemonic (partial; extend as needed)
_CSR_NAMES: dict[int, str] = {
    0x300: "mstatus",  0x301: "misa",     0x304: "mie",
    0x305: "mtvec",    0x341: "mepc",     0x342: "mcause",
    0x343: "mtval",    0x344: "mip",      0xF14: "mhartid",
    0x180: "satp",     0x100: "sstatus",  0x104: "sie",
    0x105: "stvec",    0x141: "sepc",     0x142: "scause",
    0x143: "stval",    0x144: "sip",      0xC00: "cycle",
    0xC02: "instret",
}

_PRIV_MAP = {"0": "U", "1": "S", "3": "M"}

def _priv_str(raw: str) -> str:
    return _PRIV_MAP.get(raw.strip(), "M")

def _hex(val: int, width: int = 8) -> str:
    """Return 0x-prefixed lowercase hex string."""
    return f"0x{val & ((1 << (width * 4)) - 1):0{width}x}"

def _reg_idx(name: str) -> Optional[int]:
    """
    Resolve register name to integer index.
    Accepts: x0-x31 (numeric) or ABI names (zero, ra, sp, …)
    """
    name = name.strip().lower()
    if name.startswith("x"):
        try:
            idx = int(name[1:])
            if 0 <= idx <= 31:
                return idx
        except ValueError:
            pass
    return _ABI_TO_IDX.get(name)


# ─────────────────────────────────────────────────────────
# Regex patterns
# ─────────────────────────────────────────────────────────

# FORMAT A: core   0: 0x80000000 (0x00000297) auipc   t0, 0x0
_RE_FMT_A = re.compile(
    r"^core\s+\d+:\s+"
    r"(0x[0-9a-fA-F]+)\s+"          # group 1: pc
    r"\((0x[0-9a-fA-F]+)\)"          # group 2: instr encoding
    r"(?:\s+(.*))?$"                 # group 3: optional disasm
)

# FORMAT B instruction line:
# core   0: 3 0x80000000 (0x00000297) x5  0x80000000
# core   0: 3 0x80000000 (0x00000297) auipc t0, 0x0
# also handles trailing reg writeback on same line as disasm (some builds)
_RE_FMT_B_INSTR = re.compile(
    r"^core\s+\d+:\s+"
    r"([0-3])\s+"                    # group 1: priv
    r"(0x[0-9a-fA-F]+)\s+"          # group 2: pc
    r"\((0x[0-9a-fA-F]+)\)"          # group 3: instr
    r"(?:\s+(.*))?$"                 # group 4: rest (disasm or reg/mem)
)

# FORMAT B writeback (register): x5  0x80000000
_RE_REG_WRITE = re.compile(
    r"^(x\d{1,2}|zero|ra|sp|gp|tp|t[0-6]|s\d{1,2}|a[0-7]|fp)"
    r"\s+(0x[0-9a-fA-F]+)$",
    re.IGNORECASE,
)

# FORMAT B writeback (memory): mem 0x80002000 0x00000000
_RE_MEM_ACCESS = re.compile(
    r"^mem\s+(0x[0-9a-fA-F]+)(?:\s+(0x[0-9a-fA-F]+))?$",
    re.IGNORECASE,
)

# FORMAT B CSR writeback: csr 0x300 0x00001800
_RE_CSR_WRITE = re.compile(
    r"^csr\s+(0x[0-9a-fA-F]{3})\s+(0x[0-9a-fA-F]+)$",
    re.IGNORECASE,
)

# Exception/trap line: core   0: exception load_access_fault, epc 0x80000010
_RE_EXCEPTION = re.compile(
    r"^core\s+\d+:\s+exception\s+(\S+),\s+epc\s+(0x[0-9a-fA-F]+)$",
    re.IGNORECASE,
)

# Interrupt: core   0: interrupt X
_RE_INTERRUPT = re.compile(
    r"^core\s+\d+:\s+(interrupt|trap)\s+(\S+)$",
    re.IGNORECASE,
)


@dataclass
class RawCommit:
    """Intermediate parsed commit record before JSON serialisation."""
    seq: int
    pc: str
    instr: str
    priv: str = "M"
    disasm: str = ""
    reg_writes: List[dict] = field(default_factory=list)
    csr_writes: List[dict] = field(default_factory=list)
    mem_access: Optional[dict] = None
    trap: Optional[dict] = None

    def to_jsonl_dict(self, source: str = "iss") -> dict:
        """
        Serialise to a JSONL record conforming to AVA schema v2.0.0.

        Mandatory fields (always present):
            schema_version, seq, pc, instr, src, hart, fpregs

        Optional fields (present when non-empty):
            priv, disasm, regs, csrs, mem, trap
        """
        d: dict = {
            # ── AVA v2.0.0 mandatory ─────────────────────────────────────
            "schema_version": SCHEMA_VERSION,
            "hart":           0,
            "fpregs":         None,   # RV32IM has no F extension; future use
            # ── Core commit fields ───────────────────────────────────────
            "seq":   self.seq,
            "pc":    self.pc,
            "instr": self.instr,
            "src":   source,           # renamed from "source"
        }
        if self.priv:
            d["priv"] = self.priv
        if self.disasm:
            d["disasm"] = self.disasm
        if self.reg_writes:
            d["regs"] = self.reg_writes     # renamed from "reg_writes"; {rd, value} preserved
        if self.csr_writes:
            d["csrs"] = self.csr_writes     # renamed from "csr_writes"
        if self.mem_access:
            d["mem"] = self.mem_access      # renamed from "mem_access"
        if self.trap:
            d["trap"] = self.trap
        return d


# ─────────────────────────────────────────────────────────
# Format auto-detection
# ─────────────────────────────────────────────────────────

def detect_format(lines: List[str]) -> str:
    """
    Inspect first 200 non-empty lines and return 'A', 'B', or 'unknown'.

    FORMAT B has a privilege digit between 'core N:' and '0x<pc>'.
    FORMAT A goes directly from 'core N:' to '0x<pc>'.
    """
    fmt_a_count = 0
    fmt_b_count = 0
    for line in lines[:200]:
        line = line.rstrip()
        if not line or "core" not in line:
            continue
        if _RE_FMT_B_INSTR.match(line):
            fmt_b_count += 1
        elif _RE_FMT_A.match(line):
            fmt_a_count += 1
    if fmt_b_count >= fmt_a_count:
        return "B"
    if fmt_a_count > 0:
        return "A"
    return "unknown"


# ─────────────────────────────────────────────────────────
# FORMAT A parser  (simple -l only log)
# ─────────────────────────────────────────────────────────

def _parse_format_a(lines: List[str]) -> Iterator[RawCommit]:
    """
    FORMAT A only carries PC + instruction word + disasm.
    No register writeback info is available.
    """
    seq = 0
    for line in lines:
        line = line.rstrip()
        m = _RE_FMT_A.match(line)
        if not m:
            continue
        pc, instr, disasm = m.group(1), m.group(2), (m.group(3) or "").strip()
        # Normalise to 0x-prefixed 8-hex-digit
        pc    = _hex(int(pc,    16), 8)
        instr = _hex(int(instr, 16), 8)
        yield RawCommit(seq=seq, pc=pc, instr=instr, disasm=disasm)
        seq += 1


# ─────────────────────────────────────────────────────────
# FORMAT B parser  (--log-commits output)
# ─────────────────────────────────────────────────────────

def _parse_rest_b(rest: str, commit: RawCommit) -> None:
    """
    Parse the 'rest' portion of a FORMAT B line, which may be:
      - a disasm string:              auipc   t0, 0x0
      - a register writeback:        x5  0x80000000
      - a memory access:             mem 0x80002000 0x00000000
      - a CSR writeback:             csr 0x300 0x00001800
      - disasm + writeback inline:   auipc   t0, 0x0 x5 0x80000000
    """
    rest = rest.strip()
    if not rest:
        return

    # Try reg-write first (most common writeback)
    m = _RE_REG_WRITE.match(rest)
    if m:
        reg_name, value = m.group(1), m.group(2)
        rd = _reg_idx(reg_name)
        if rd is not None and rd != 0:   # x0 writes are silent
            commit.reg_writes.append({
                "rd":    rd,
                "value": _hex(int(value, 16), 8),
            })
        return

    # Try memory access
    m = _RE_MEM_ACCESS.match(rest)
    if m:
        addr  = _hex(int(m.group(1), 16), 8)
        value = _hex(int(m.group(2), 16), 8) if m.group(2) else "0x00000000"
        # Direction is inferred by caller context; default to load
        commit.mem_access = {"type": "load", "addr": addr, "size": 4, "value": value}
        return

    # Try CSR write
    m = _RE_CSR_WRITE.match(rest)
    if m:
        addr_int = int(m.group(1), 16)
        csr_rec: dict = {
            "addr":  f"0x{addr_int:03x}",
            "value": _hex(int(m.group(2), 16), 8),
        }
        name = _CSR_NAMES.get(addr_int)
        if name:
            csr_rec["name"] = name
        commit.csr_writes.append(csr_rec)
        return

    # Treat as disasm; but also check for inline trailing writeback
    # e.g.  "auipc   t0, 0x0 x5 0x80000000"
    # Strategy: split on last run of whitespace before what looks like "xN 0x..."
    inline_wb = re.search(
        r"\s+(x\d{1,2}|zero|ra|sp|gp|tp|t\d|s\d{1,2}|a\d|fp)\s+(0x[0-9a-fA-F]+)\s*$",
        rest, re.IGNORECASE
    )
    if inline_wb:
        disasm_part = rest[:inline_wb.start()].strip()
        reg_name, value = inline_wb.group(1), inline_wb.group(2)
        commit.disasm = disasm_part
        rd = _reg_idx(reg_name)
        if rd is not None and rd != 0:
            commit.reg_writes.append({
                "rd":    rd,
                "value": _hex(int(value, 16), 8),
            })
    else:
        commit.disasm = rest


def _parse_format_b(lines: List[str]) -> Iterator[RawCommit]:
    """
    FORMAT B state machine.

    Spike can emit instruction commit records in several sub-layouts:

    Sub-layout 1 — writeback inline (most common with --log-commits):
        core   0: 3 0x80000000 (0x00000297) x5  0x80000000

    Sub-layout 2 — disasm line followed by writeback line with SAME pc+instr:
        core   0: 3 0x80000000 (0x00000297) auipc   t0, 0x0
        core   0: 3 0x80000000 (0x00000297) x5  0x80000000

    Sub-layout 3 — no writeback (e.g. branches, stores without reg dst):
        core   0: 3 0x80000008 (0x00002223) mem 0x00000004 0x00000000

    The KEY rule: if a new instruction line has the same (pc, instr) as the
    currently buffered (pending) commit, it is a CONTINUATION (additional
    writeback / side-effect) for that same retired instruction, NOT a new one.

    Only flush pending and start a new commit when the new line carries a
    genuinely different (pc, instr) pair.
    """
    seq      = 0
    pending: Optional[RawCommit] = None

    def flush(c: Optional[RawCommit]) -> Iterator[RawCommit]:
        if c is not None:
            yield c

    for line in lines:
        line = line.rstrip()
        if not line:
            continue

        # ── Exception / interrupt (attach to pending, no new commit) ─────
        m_exc = _RE_EXCEPTION.match(line)
        if m_exc:
            cause_name, epc = m_exc.group(1), m_exc.group(2)
            if pending is not None:
                pending.trap = {
                    "cause":        _hex(_exception_name_to_cause(cause_name), 8),
                    "tval":         epc,
                    "is_interrupt": False,
                }
            continue

        m_int = _RE_INTERRUPT.match(line)
        if m_int and pending is not None:
            pending.trap = {
                "cause":        "0x80000000",
                "tval":         "0x00000000",
                "is_interrupt": True,
            }
            continue

        # ── Does this line look like an instruction header? ───────────────
        m = _RE_FMT_B_INSTR.match(line)
        if m:
            priv_raw = m.group(1)
            pc_raw   = _hex(int(m.group(2), 16), 8)
            instr_raw= _hex(int(m.group(3), 16), 8)
            rest     = m.group(4) or ""

            # ── CONTINUATION check ───────────────────────────────────────
            # If (pc, instr) matches the pending commit this is just another
            # writeback line for the same retired instruction (sub-layout 2).
            if (
                pending is not None
                and pending.pc    == pc_raw
                and pending.instr == instr_raw
            ):
                _parse_rest_b(rest, pending)
                continue

            # ── New instruction — flush previous ─────────────────────────
            yield from flush(pending)
            pending = RawCommit(
                seq=seq, pc=pc_raw, instr=instr_raw,
                priv=_priv_str(priv_raw),
            )
            seq += 1
            _parse_rest_b(rest, pending)
            continue

        # ── Bare writeback / side-effect continuation line ────────────────
        # Lines that don't match the instruction header but are non-empty
        # and belong to the current commit (reg write, mem, csr).
        if pending is not None:
            # Strip "core   N: " prefix if present (some Spike builds repeat it)
            stripped = re.sub(r"^core\s+\d+:\s+", "", line).strip()
            if stripped:
                _parse_rest_b(stripped, pending)

    yield from flush(pending)


# ─────────────────────────────────────────────────────────
# Exception name → mcause mapping  (RISC-V spec Table 3.6)
# ─────────────────────────────────────────────────────────

_EXC_NAMES: dict[str, int] = {
    "misaligned_fetch":          0x0,
    "fetch_access":              0x1,
    "illegal_instruction":       0x2,
    "breakpoint":                0x3,
    "misaligned_load":           0x4,
    "load_access":               0x5,
    "load_access_fault":         0x5,
    "misaligned_store":          0x6,
    "store_access":              0x7,
    "store_access_fault":        0x7,
    "user_ecall":                0x8,
    "supervisor_ecall":          0x9,
    "machine_ecall":             0xb,
    "fetch_page_fault":          0xc,
    "load_page_fault":           0xd,
    "store_page_fault":          0xf,
}

def _exception_name_to_cause(name: str) -> int:
    key = name.lower().replace("-", "_")
    return _EXC_NAMES.get(key, 0xFF)


# ─────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────

def parse_spike_log(
    log_text: str,
    source:   str = "iss",
    fmt:      Optional[str] = None,
) -> List[dict]:
    """
    Parse *all* of a Spike log and return a list of commit-log dicts
    conforming to schemas/commitlog.schema.json.

    Parameters
    ----------
    log_text : str
        Full text of Spike's stderr/stdout log.
    source : str
        'iss' (default), 'rtl', or 'formal'. Stored in every record.
    fmt : str or None
        Force 'A' or 'B'; auto-detected if None.

    Returns
    -------
    list[dict]
        Ordered list of commit-log records (already JSON-serialisable).
    """
    lines = log_text.splitlines()
    if fmt is None:
        fmt = detect_format(lines)
        logger.info("Auto-detected Spike log format: %s", fmt)

    if fmt == "B":
        commits_iter = _parse_format_b(lines)
    else:
        if fmt == "unknown":
            logger.warning("Could not detect Spike log format; falling back to FORMAT A")
        commits_iter = _parse_format_a(lines)

    return [c.to_jsonl_dict(source=source) for c in commits_iter]


def parse_spike_log_streaming(
    log_text: str,
    source:   str = "iss",
    fmt:      Optional[str] = None,
) -> Iterator[dict]:
    """Streaming variant — yields one dict at a time (lower memory for huge runs)."""
    lines = log_text.splitlines()
    if fmt is None:
        fmt = detect_format(lines)
    parser = _parse_format_b(lines) if fmt == "B" else _parse_format_a(lines)
    for commit in parser:
        yield commit.to_jsonl_dict(source=source)
