#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bug_hypothesis.py — Autonomous Bug Cause Inference
===================================================
Generates ranked natural-language hypotheses explaining WHY a commit-log
mismatch occurred, based on the mismatch type, diverging instruction, CSR
address, memory operation, and surrounding context.

Designed to be called from the AVA pipeline immediately after
``compare_commitlogs.py`` produces a ``CompareResult``, and to embed its
output in ``bugreport.json["hypotheses"]``.

Architecture
------------
- ``HypothesisEngine`` is the main entry point. It holds a rule database
  (``HYPOTHESIS_DB``) keyed by ``(MismatchType_value, pattern)``.
- Each rule specifies a confidence score (0.0–1.0) and an optional
  ``requires`` guard (a callable that inspects the CommitEntry further).
- ``generate(result, entry)`` fires all matching rules, deduplicates,
  sorts by confidence, and returns the top-N as plain dicts ready for JSON.
- The engine is fully standalone: it imports only the public types from
  compare_commitlogs and stdlib modules, so it can run without the
  comparator being on sys.path by passing dicts directly.

Usage from AVA pipeline::

    from compare_commitlogs import compare_logs
    from bug_hypothesis import HypothesisEngine

    result = compare_logs("rtl.jsonl", "iss.jsonl", seed=42)
    if not result.passed:
        engine = HypothesisEngine()
        hypotheses = engine.generate(result)
        report = result.to_bug_report()
        report["hypotheses"] = hypotheses
        Path("bugreport.json").write_text(json.dumps(report, indent=2))

Stand-alone CLI::

    python bug_hypothesis.py bugreport.json
    python bug_hypothesis.py --self-test
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

__version__ = "1.0.0"
__all__ = ["HypothesisEngine", "Hypothesis", "generate_hypotheses"]

# ── RV32 opcode / funct3 / funct7 constants ──────────────────────────────────
_OP_ARITH   = 0x33   # R-type: ADD SUB AND OR XOR SLL SRL SRA
_OP_ARITH_I = 0x13   # I-type: ADDI ANDI ORI XORI SLLI SRLI SRAI
_OP_MUL     = 0x33   # MUL/DIV/REM share opcode with ARITH; distinguished by funct7
_OP_LOAD    = 0x03
_OP_STORE   = 0x23
_OP_BRANCH  = 0x63
_OP_JAL     = 0x6F
_OP_JALR    = 0x67
_OP_LUI     = 0x37
_OP_AUIPC   = 0x17
_OP_SYSTEM  = 0x73   # CSR* / ECALL / EBREAK / MRET

_FUNCT7_MULDIV = 0x01   # M-extension
_FUNCT3_MUL    = 0x0
_FUNCT3_MULH   = 0x1
_FUNCT3_MULHSU = 0x2
_FUNCT3_MULHU  = 0x3
_FUNCT3_DIV    = 0x4
_FUNCT3_DIVU   = 0x5
_FUNCT3_REM    = 0x6
_FUNCT3_REMU   = 0x7

_FUNCT3_LB  = 0x0
_FUNCT3_LH  = 0x1
_FUNCT3_LW  = 0x2
_FUNCT3_LBU = 0x4
_FUNCT3_LHU = 0x5
_FUNCT3_SB  = 0x0
_FUNCT3_SH  = 0x1
_FUNCT3_SW  = 0x2

# Known CSR addresses
_CSR_MSTATUS  = 0x300
_CSR_MISA     = 0x301
_CSR_MIE      = 0x304
_CSR_MTVEC    = 0x305
_CSR_MEPC     = 0x341
_CSR_MCAUSE   = 0x342
_CSR_MTVAL    = 0x343
_CSR_MIP      = 0x344
_CSR_CYCLE    = 0xC00
_CSR_TIME     = 0xC01
_CSR_INSTRET  = 0xC02


# ═══════════════════════════════════════════════════════════════════════════════
# Data structures
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class Hypothesis:
    """One ranked hypothesis about why a mismatch occurred."""
    text:        str
    confidence:  float   # 0.0 – 1.0
    category:    str     # "hardware" | "forwarding" | "csr" | "memory" | "trap" | "meta"
    detail:      Optional[str] = None    # extra machine-readable context
    references:  List[str]     = field(default_factory=list)   # RISC-V spec sections

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None and v != []}


# ── Rule type: (text, confidence, category, detail?, references?, requires?) ──
_RuleSpec = Tuple[str, float, str, Optional[str], List[str], Optional[Callable]]

# Convenience constructors
def _rule(
    text: str,
    conf: float,
    cat:  str,
    detail:     Optional[str]      = None,
    refs:       Optional[List[str]] = None,
    requires:   Optional[Callable]  = None,
) -> _RuleSpec:
    return (text, conf, cat, detail, refs or [], requires)


# ═══════════════════════════════════════════════════════════════════════════════
# Instruction decode helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _opcode(instr: int) -> int:
    return instr & 0x7F

def _funct3(instr: int) -> int:
    return (instr >> 12) & 0x7

def _funct7(instr: int) -> int:
    return (instr >> 25) & 0x7F

def _is_muldiv(instr: int) -> bool:
    return _opcode(instr) == _OP_MUL and _funct7(instr) == _FUNCT7_MULDIV

def _is_mul(instr: int) -> bool:
    return _is_muldiv(instr) and _funct3(instr) == _FUNCT3_MUL

def _is_mulh(instr: int) -> bool:
    return _is_muldiv(instr) and _funct3(instr) in (_FUNCT3_MULH, _FUNCT3_MULHSU, _FUNCT3_MULHU)

def _is_div(instr: int) -> bool:
    return _is_muldiv(instr) and _funct3(instr) in (_FUNCT3_DIV, _FUNCT3_DIVU)

def _is_rem(instr: int) -> bool:
    return _is_muldiv(instr) and _funct3(instr) in (_FUNCT3_REM, _FUNCT3_REMU)

def _is_div_or_rem(instr: int) -> bool:
    return _is_div(instr) or _is_rem(instr)

def _is_load(instr: int) -> bool:
    return _opcode(instr) == _OP_LOAD

def _is_store(instr: int) -> bool:
    return _opcode(instr) == _OP_STORE

def _is_csr(instr: int) -> bool:
    return _opcode(instr) == _OP_SYSTEM and _funct3(instr) in (1, 2, 3, 5, 6, 7)

def _is_mret(instr: int) -> bool:
    return instr == 0x30200073

def _is_ecall(instr: int) -> bool:
    return instr == 0x00000073

def _is_signed_load(instr: int) -> bool:
    return _is_load(instr) and _funct3(instr) in (_FUNCT3_LH, _FUNCT3_LB)

def _is_unsigned_load(instr: int) -> bool:
    return _is_load(instr) and _funct3(instr) in (_FUNCT3_LHU, _FUNCT3_LBU)

def _is_subword_store(instr: int) -> bool:
    return _is_store(instr) and _funct3(instr) in (_FUNCT3_SB, _FUNCT3_SH)

def _csr_addr_from_instr(instr: int) -> int:
    """Extract CSR address from a CSR-type instruction."""
    return (instr >> 20) & 0xFFF

def _is_div_by_zero(entry: Dict[str, Any]) -> bool:
    """Heuristic: rs2_val is 0x0 at a div/rem instruction."""
    rs2 = entry.get("rs2_val")
    if rs2 is None:
        return False
    try:
        return int(rs2, 16) == 0
    except (ValueError, TypeError):
        return False

def _is_int_min_neg1(entry: Dict[str, Any]) -> bool:
    """Heuristic: INT_MIN / -1 overflow for signed DIV/REM."""
    rs1 = entry.get("rs1_val")
    rs2 = entry.get("rs2_val")
    if rs1 is None or rs2 is None:
        return False
    try:
        return int(rs1, 16) == 0x80000000 and int(rs2, 16) == 0xFFFFFFFF
    except (ValueError, TypeError):
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# Hypothesis rule database
# ═══════════════════════════════════════════════════════════════════════════════
# Keys are AVA MismatchType wire-values (strings).
# Each value is a list of _RuleSpec items evaluated against the diverging entry.
# ──────────────────────────────────────────────────────────────────────────────

HYPOTHESIS_DB: Dict[str, List[_RuleSpec]] = {

    # ── REG_MISMATCH ──────────────────────────────────────────────────────────
    "REGMISMATCH": [
        _rule(
            "MUL result forwarding path incorrect: product not propagated to "
            "writeback on the same cycle as completion",
            0.85, "forwarding",
            detail="Check MUL pipeline stage → writeback mux select",
            refs=["RISC-V ISA 2.2 §M.MUL"],
            requires=lambda e, _: _is_mul(e.get("instr", 0)),
        ),
        _rule(
            "MULH/MULHSU/MULHU upper-half result computed incorrectly: "
            "sign-extension or partial-product accumulation error",
            0.88, "hardware",
            detail="Verify upper 32 bits of 64-bit multiply accumulator",
            refs=["RISC-V ISA 2.2 §M.MULH"],
            requires=lambda e, _: _is_mulh(e.get("instr", 0)),
        ),
        _rule(
            "DIV/DIVU result wrong: likely divide-by-zero not returning "
            "all-ones (DIVU) or -1 (DIV) per RISC-V spec",
            0.92, "hardware",
            detail="RV spec: DIV by 0 → -1, DIVU by 0 → MAX_UINT",
            refs=["RISC-V ISA 2.2 §M.DIV"],
            requires=lambda e, _: _is_div(e.get("instr", 0)) and _is_div_by_zero(e),
        ),
        _rule(
            "DIV/REM INT_MIN overflow: signed division of 0x80000000 / -1 "
            "should return INT_MIN (DIV) or 0 (REM) per spec",
            0.95, "hardware",
            detail="Overflow sentinel: rd = rs1 for DIV, rd = 0 for REM",
            refs=["RISC-V ISA 2.2 §M.DIV overflow"],
            requires=lambda e, _: _is_div_or_rem(e.get("instr", 0)) and _is_int_min_neg1(e),
        ),
        _rule(
            "REM/REMU result incorrect: remainder sign convention wrong "
            "(sign follows dividend for REM, always positive for REMU)",
            0.80, "hardware",
            detail="REM: sign(quotient*divisor + remainder) == sign(dividend)",
            refs=["RISC-V ISA 2.2 §M.REM"],
            requires=lambda e, _: _is_rem(e.get("instr", 0)),
        ),
        _rule(
            "ALU result write-back stage mux selecting wrong lane "
            "(possibly forwarded vs. committed value conflict)",
            0.55, "forwarding",
            requires=lambda e, _: _opcode(e.get("instr", 0)) in (_OP_ARITH, _OP_ARITH_I),
        ),
        _rule(
            "Load-use hazard: register file written before load data "
            "returns from memory (pipeline stall logic incorrect)",
            0.70, "hardware",
            refs=["RISC-V ISA Vol. I §2.6"],
            requires=lambda e, ctx: _is_load(e.get("instr", 0)),
        ),
        _rule(
            "Signed load sign-extension incorrect: LH/LB upper bits not "
            "propagated from bit 15/7 respectively",
            0.82, "hardware",
            refs=["RISC-V ISA Vol. I §2.6 LOAD"],
            requires=lambda e, _: _is_signed_load(e.get("instr", 0)),
        ),
        _rule(
            "JAL/JALR return-address calculation off by one: rd should be "
            "PC+4 but pipeline computes PC+offset or PC+2",
            0.75, "hardware",
            refs=["RISC-V ISA Vol. I §2.5"],
            requires=lambda e, _: _opcode(e.get("instr", 0)) in (_OP_JAL, _OP_JALR),
        ),
    ],

    # ── PC_MISMATCH ───────────────────────────────────────────────────────────
    "PCMISMATCH": [
        _rule(
            "Branch target address computed incorrectly: sign-extended "
            "offset added to wrong base (should be PC, not PC+4)",
            0.78, "hardware",
            refs=["RISC-V ISA Vol. I §2.5 BRANCH"],
            requires=lambda e, _: _opcode(e.get("instr", 0)) == _OP_BRANCH,
        ),
        _rule(
            "JALR target misaligned: LSB of target address not cleared "
            "before loading into PC (spec requires clearing bit 0)",
            0.82, "hardware",
            refs=["RISC-V ISA Vol. I §2.5 JALR"],
            requires=lambda e, _: _opcode(e.get("instr", 0)) == _OP_JALR,
        ),
        _rule(
            "MRET not restoring MEPC correctly: possibly loading PC from "
            "wrong pipeline stage latch or wrong CSR address",
            0.88, "trap",
            refs=["RISC-V Priv. Spec §3.3.2 MRET"],
            requires=lambda e, _: _is_mret(e.get("instr", 0)),
        ),
        _rule(
            "Exception / interrupt vector base address wrong: MTVEC.BASE "
            "computation or MODE field mishandled",
            0.72, "trap",
            refs=["RISC-V Priv. Spec §3.1.7 MTVEC"],
            requires=lambda e, ctx: ctx.get("trap", False),
        ),
        _rule(
            "PC fetch from wrong pipeline stage after flush: branch "
            "misprediction recovery flushing wrong instructions",
            0.65, "hardware",
        ),
    ],

    # ── CSR_MISMATCH ──────────────────────────────────────────────────────────
    "CSRMISMATCH": [
        _rule(
            "mstatus.MPP field not saved correctly on trap entry: "
            "privilege mode encoding (00=U, 11=M) written to wrong bits",
            0.88, "csr",
            detail="mstatus[12:11] = MPP; check CSR write path on trap",
            refs=["RISC-V Priv. Spec §3.1.6 mstatus.MPP"],
            requires=lambda e, ctx: any(
                int(w.get("csr", "0"), 16) == _CSR_MSTATUS
                for w in (ctx.get("csr_writes") or [])
            ),
        ),
        _rule(
            "mstatus.MPIE not restored on MRET: bit 7 of mstatus should "
            "become 1 and MIE should be set from MPIE",
            0.85, "csr",
            refs=["RISC-V Priv. Spec §3.3.2 MRET mstatus update"],
            requires=lambda e, _: _is_mret(e.get("instr", 0)),
        ),
        _rule(
            "mcause encoding incorrect: exception code in bits[30:0] "
            "and interrupt bit [31] set wrongly",
            0.82, "trap",
            refs=["RISC-V Priv. Spec §3.1.16 mcause"],
            requires=lambda e, ctx: any(
                int(w.get("csr", "0"), 16) == _CSR_MCAUSE
                for w in (ctx.get("csr_writes") or [])
            ),
        ),
        _rule(
            "mepc not aligned to instruction boundary: spec requires "
            "mepc[1:0]=00 for non-C extensions",
            0.78, "csr",
            refs=["RISC-V Priv. Spec §3.1.15 mepc"],
            requires=lambda e, ctx: any(
                int(w.get("csr", "0"), 16) == _CSR_MEPC
                for w in (ctx.get("csr_writes") or [])
            ),
        ),
        _rule(
            "CSRRW/CSRRS/CSRRC atomic read-modify-write not atomic in "
            "implementation: old value captured after write completes",
            0.70, "csr",
            refs=["RISC-V ISA Vol. I §9 Zicsr"],
            requires=lambda e, _: _is_csr(e.get("instr", 0)),
        ),
        _rule(
            "Counter CSR (cycle/time/instret) increment logic incorrect "
            "or inhibited when it should not be",
            0.65, "csr",
            refs=["RISC-V Priv. Spec §3.1.11 Hardware counters"],
            requires=lambda e, ctx: any(
                int(w.get("csr", "0"), 16) in (_CSR_CYCLE, _CSR_TIME, _CSR_INSTRET)
                for w in (ctx.get("csr_writes") or [])
            ),
        ),
    ],

    # ── MEM_MISMATCH ──────────────────────────────────────────────────────────
    "MEMMISMATCH": [
        _rule(
            "Byte-enable strobe logic incorrect: wrong lanes enabled on "
            "sub-word store, causing adjacent bytes to be overwritten",
            0.88, "memory",
            refs=["RISC-V ISA Vol. I §2.6 STORE"],
            requires=lambda e, _: _is_subword_store(e.get("instr", 0)),
        ),
        _rule(
            "Signed halfword load sign-extension wrong: bit 15 not "
            "replicated into bits [31:16] of destination register",
            0.85, "hardware",
            refs=["RISC-V ISA Vol. I §2.6 LH"],
            requires=lambda e, _: _funct3(e.get("instr", 0)) == _FUNCT3_LH and _is_load(e.get("instr", 0)),
        ),
        _rule(
            "Signed byte load sign-extension wrong: bit 7 not "
            "replicated into bits [31:8]",
            0.85, "hardware",
            refs=["RISC-V ISA Vol. I §2.6 LB"],
            requires=lambda e, _: _funct3(e.get("instr", 0)) == _FUNCT3_LB and _is_load(e.get("instr", 0)),
        ),
        _rule(
            "Store data alignment: data not correctly shifted to match "
            "byte lane of unaligned address before write to memory bus",
            0.80, "memory",
        ),
        _rule(
            "AMO (atomic memory operation) read-modify-write not truly "
            "atomic: second read between reservation and write",
            0.72, "memory",
            refs=["RISC-V ISA Vol. I §A Atomics"],
            requires=lambda e, _: e.get("mem_op") == "amo",
        ),
        _rule(
            "Load data not returned within latency contract: forwarding "
            "from store buffer to load missed, stale value returned",
            0.68, "memory",
            requires=lambda e, _: _is_load(e.get("instr", 0)),
        ),
    ],

    # ── TRAP_MISMATCH ─────────────────────────────────────────────────────────
    "TRAPMISMATCH": [
        _rule(
            "Illegal instruction exception not raised when it should be: "
            "decode stage accepting reserved encoding",
            0.80, "trap",
            refs=["RISC-V Priv. Spec §3.2.1 mcause=2"],
        ),
        _rule(
            "ECALL not triggering trap: privilege-mode ECALL routing "
            "logic mapping to wrong exception target",
            0.85, "trap",
            refs=["RISC-V Priv. Spec §3.2.1 mcause=11"],
            requires=lambda e, _: _is_ecall(e.get("instr", 0)),
        ),
        _rule(
            "Interrupt not taken when MIE=1 and pending bit set: "
            "interrupt-enable masking logic or priority encoder fault",
            0.75, "trap",
            refs=["RISC-V Priv. Spec §3.1.9 mie/mip"],
            requires=lambda e, ctx: ctx.get("trap", False),
        ),
        _rule(
            "Trap handler not entered despite exception: MTVEC.BASE "
            "pointing to wrong address, fetch silently fails",
            0.70, "trap",
        ),
    ],

    # ── TRAP_CAUSE_MISMATCH ───────────────────────────────────────────────────
    "TRAPCAUSEMISMATCH": [
        _rule(
            "mcause exception code wrong: hardware encodes wrong cause "
            "number (e.g. load fault vs. store fault swapped)",
            0.82, "trap",
            refs=["RISC-V Priv. Spec Table 3.6 mcause"],
        ),
        _rule(
            "Interrupt bit [31] of mcause set when it should not be "
            "(or vice versa): synchronous vs. asynchronous confusion",
            0.78, "trap",
            refs=["RISC-V Priv. Spec §3.1.16 mcause bit 31"],
        ),
        _rule(
            "mepc not pointing to faulting instruction: pipeline recording "
            "PC of next instruction instead of trapping instruction",
            0.85, "trap",
            refs=["RISC-V Priv. Spec §3.1.15 mepc"],
        ),
        _rule(
            "mtval not populated for load/store faults: bad address not "
            "forwarded to the CSR write path on exception entry",
            0.75, "trap",
            refs=["RISC-V Priv. Spec §3.1.17 mtval"],
        ),
    ],

    # ── ALIGNMENTERROR ────────────────────────────────────────────────────────
    "ALIGNMENTERROR": [
        _rule(
            "RTL not generating misaligned load/store exception when "
            "address is not naturally aligned to access size",
            0.90, "hardware",
            refs=["RISC-V ISA Vol. I §2.6 misaligned exceptions"],
        ),
        _rule(
            "ISS treating unaligned access as legal (may be emulating a "
            "platform with hardware misalignment support)",
            0.70, "meta",
            detail="Consider --no-align-check if target platform supports HW misalignment",
        ),
    ],

    # ── X0WRITTEN ─────────────────────────────────────────────────────────────
    "X0WRITTEN": [
        _rule(
            "Register file write-enable logic does not suppress writes to "
            "x0: rd==0 should gate off the write-enable signal",
            0.95, "hardware",
            refs=["RISC-V ISA Vol. I §2.1 x0 hardwired zero"],
        ),
        _rule(
            "CSR read-write to x0 destination not suppressed: CSRRW x0 "
            "should perform the CSR write but not read into x0",
            0.80, "csr",
            refs=["RISC-V ISA Vol. I §9 Zicsr CSRRW"],
            requires=lambda e, _: _is_csr(e.get("instr", 0)),
        ),
    ],

    # ── SEQGAP ────────────────────────────────────────────────────────────────
    "SEQGAP": [
        _rule(
            "RTL simulator commit-log writer skipped retired instructions "
            "(possibly filtered out no-ops or pipeline bubbles)",
            0.75, "meta",
            detail="Check if RTL log writer has a filter on instruction types",
        ),
        _rule(
            "ISS and RTL have different definitions of 'committed': "
            "one logs speculative, the other logs retired instructions",
            0.70, "meta",
        ),
        _rule(
            "Exception handler executed extra instructions not present in "
            "the other side's log (trap taken on one side only)",
            0.65, "trap",
        ),
    ],

    # ── LENGTHMISMATCH ────────────────────────────────────────────────────────
    "LENGTHMISMATCH": [
        _rule(
            "RTL simulation terminated early due to an unhandled exception "
            "or watchdog timeout, truncating the commit log",
            0.78, "meta",
        ),
        _rule(
            "ISS ran more instructions because it silently ignored an "
            "illegal instruction that trapped in RTL",
            0.72, "trap",
        ),
        _rule(
            "Log file corruption or incomplete write: the shorter log may "
            "have been truncated by a crash mid-run",
            0.55, "meta",
        ),
    ],

    # ── SCHEMAINVALID ─────────────────────────────────────────────────────────
    "SCHEMAINVALID": [
        _rule(
            "Log writer emitting out-of-spec values: register index > 31 "
            "or invalid privilege mode string — indicates a logging bug",
            0.90, "meta",
            detail="Fix the RTL/ISS commit-log writer, not the comparator",
        ),
        _rule(
            "Mixed JSONL schemas between runs: log writer was updated but "
            "the comparator schema version was not synchronised",
            0.70, "meta",
        ),
    ],

    # ── BINARYHASHMISMATCH ────────────────────────────────────────────────────
    "BINARYHASHMISMATCH": [
        _rule(
            "Commit log does not match the expected SHA-256 pinned in the "
            "manifest: file was replaced or re-generated after pinning",
            0.95, "meta",
            detail="Re-run the simulation and update manifest['expected_sha256']",
        ),
        _rule(
            "Log file was modified by a post-processing script after "
            "simulation, invalidating the hash",
            0.75, "meta",
        ),
    ],

    # ── INSTRMISMATCH ─────────────────────────────────────────────────────────
    "INSTRMISMATCH": [
        _rule(
            "Instruction fetch from wrong address: I-cache returning stale "
            "line or TLB mapping incorrect after context switch",
            0.75, "hardware",
        ),
        _rule(
            "Self-modifying code: store to instruction memory not flushing "
            "I-cache, so fetch sees old instruction word",
            0.68, "memory",
            refs=["RISC-V ISA Vol. I §1.6 FENCE.I"],
        ),
        _rule(
            "Compressed (RVC) instruction decoded as 32-bit: C-extension "
            "detection logic misidentifying 16-bit instruction boundary",
            0.72, "hardware",
            refs=["RISC-V ISA Vol. I §16 RVC"],
        ),
    ],
}


# ═══════════════════════════════════════════════════════════════════════════════
# Context helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_context(mismatch_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten the RTL commit entry and surrounding context into one dict
    for use as the ``ctx`` argument to rule ``requires`` callables."""
    entry: Dict[str, Any] = dict(mismatch_dict.get("rtl_entry") or {})
    # Pull instr as int for easy opcode inspection
    instr_hex = entry.get("instr", "0x0") or "0x0"
    try:
        entry["instr"] = int(instr_hex, 16)
    except (ValueError, TypeError):
        entry["instr"] = 0
    return entry


# ═══════════════════════════════════════════════════════════════════════════════
# Engine
# ═══════════════════════════════════════════════════════════════════════════════

class HypothesisEngine:
    """Generates ranked hypotheses from a bug report or CompareResult.

    Parameters
    ----------
    max_hypotheses : int
        Maximum number of hypotheses to return per mismatch (default 5).
    min_confidence : float
        Filter out hypotheses below this confidence threshold (default 0.5).
    """

    def __init__(
        self,
        max_hypotheses: int   = 5,
        min_confidence: float = 0.5,
    ) -> None:
        self._max = max_hypotheses
        self._min = min_confidence

    def generate_from_report(self, report: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Generate hypotheses from a ``bug_report.json`` dict.

        Returns a list of dicts sorted by confidence descending.
        """
        mismatches = report.get("mismatches", [])
        if not mismatches:
            return []
        return self._score_mismatch(mismatches[0])

    def generate(self, result: Any) -> List[Dict[str, Any]]:
        """Generate hypotheses from a ``CompareResult`` object.

        Accepts either the real CompareResult dataclass or a plain dict
        from ``to_bug_report()``.
        """
        # Handle both CompareResult objects and plain dicts
        if hasattr(result, "mismatches"):
            if not result.mismatches:
                return []
            m = result.mismatches[0]
            mismatch_dict = m.to_dict() if hasattr(m, "to_dict") else m
        elif isinstance(result, dict):
            return self.generate_from_report(result)
        else:
            return []

        return self._score_mismatch(mismatch_dict)

    def _score_mismatch(self, mismatch: Dict[str, Any]) -> List[Dict[str, Any]]:
        mtype = mismatch.get("mismatch_type", "")
        ctx   = _extract_context(mismatch)
        instr = ctx.get("instr", 0)

        rules: List[_RuleSpec] = HYPOTHESIS_DB.get(mtype, [])

        # Also add generic hypotheses if mtype has no specific rules
        if not rules:
            rules = [_rule(
                f"No specific hypothesis database entry for {mtype}; "
                "manual analysis required",
                0.50, "meta",
            )]

        scored: List[Hypothesis] = []
        seen_texts: set = set()

        for (text, conf, cat, detail, refs, requires) in rules:
            # Apply requires guard if present
            if requires is not None:
                try:
                    if not requires(ctx, mismatch):
                        continue
                except Exception:
                    continue   # guard crashed — skip rule
            if conf < self._min:
                continue
            if text in seen_texts:
                continue
            seen_texts.add(text)
            scored.append(Hypothesis(
                text=text, confidence=conf, category=cat,
                detail=detail, references=refs,
            ))

        # Sort by confidence descending, then alphabetically for stability
        scored.sort(key=lambda h: (-h.confidence, h.text))
        top = scored[: self._max]

        return [h.to_dict() for h in top]


# ═══════════════════════════════════════════════════════════════════════════════
# Convenience function (matches interface in the AVA prompt spec)
# ═══════════════════════════════════════════════════════════════════════════════

def generate_hypotheses(
    result:        Any,
    max_results:   int   = 5,
    min_confidence: float = 0.50,
) -> List[Dict[str, Any]]:
    """Top-level convenience wrapper.

    Accepts a ``CompareResult`` object or a ``bug_report.json`` dict.

    Usage::

        hypotheses = generate_hypotheses(result)
        report["hypotheses"] = hypotheses
    """
    return HypothesisEngine(
        max_hypotheses=max_results,
        min_confidence=min_confidence,
    ).generate(result)


# ═══════════════════════════════════════════════════════════════════════════════
# Self-test
# ═══════════════════════════════════════════════════════════════════════════════

def _run_self_tests(verbose: bool = False) -> bool:
    """Run built-in regression tests.  Returns True if all pass."""

    engine = HypothesisEngine(max_hypotheses=3, min_confidence=0.5)
    W = 60

    # Helper: build a minimal mismatch dict
    def _mm(mtype: str, instr: int = 0, **kwargs) -> Dict:
        return {
            "mismatch_type": mtype,
            "rtl_entry": {
                "pc": "0x00001000",
                "instr": f"0x{instr:08x}",
                **kwargs,
            },
            "iss_entry": {},
        }

    cases = [
        # (name, mismatch_dict, expect_category, expect_min_conf)
        ("MUL_REGMISMATCH",
         _mm("REGMISMATCH", 0x02208533),   # MUL x10,x1,x2
         "forwarding", 0.80),

        ("DIV_BY_ZERO",
         _mm("REGMISMATCH", 0x0220C533,   # DIV
             rs2_val="0x00000000"),
         "hardware", 0.90),

        ("INT_MIN_NEG1_DIV",
         _mm("REGMISMATCH", 0x0220C533,
             rs1_val="0x80000000", rs2_val="0xFFFFFFFF"),
         "hardware", 0.90),

        ("MRET_PC",
         _mm("PCMISMATCH", 0x30200073),   # MRET
         "trap", 0.80),

        ("CSR_MSTATUS",
         _mm("CSRMISMATCH", 0x30079073,   # CSRRW x0, mstatus, x15
             csr_writes=[{"csr": "0x300", "val": "0x00001800"}]),
         "csr", 0.70),

        ("X0_WRITTEN",
         _mm("X0WRITTEN", 0x00000033),
         "hardware", 0.90),

        ("ALIGN_ERROR",
         _mm("ALIGNMENTERROR"),
         "hardware", 0.80),

        ("SCHEMA_INVALID",
         _mm("SCHEMAINVALID"),
         "meta", 0.80),

        ("BINARY_HASH",
         _mm("BINARYHASHMISMATCH"),
         "meta", 0.90),

        ("LENGTH_MISMATCH",
         _mm("LENGTHMISMATCH"),
         "meta", 0.50),

        ("SEQ_GAP",
         _mm("SEQGAP"),
         "meta", 0.50),
    ]

    print(f"\n{'═'*W}")
    print(f"  bug_hypothesis.py v{__version__} — self-test ({len(cases)} cases)")
    print(f"{'═'*W}")

    passed = failed = 0
    for name, mismatch, exp_cat, exp_conf in cases:
        hyps = engine._score_mismatch(mismatch)
        ok = True
        why = ""
        if not hyps:
            ok = False; why = "no hypotheses returned"
        elif hyps[0]["confidence"] < exp_conf:
            ok = False
            why = f"top confidence {hyps[0]['confidence']:.2f} < expected {exp_conf}"
        elif hyps[0]["category"] != exp_cat:
            ok = False
            why = (f"top category {hyps[0]['category']!r} != "
                   f"expected {exp_cat!r}")

        if ok:
            print(f"  ✓  {name}")
            if verbose and hyps:
                print(f"       [{hyps[0]['confidence']:.2f}] {hyps[0]['text'][:70]}")
            passed += 1
        else:
            print(f"  ✗  {name}  — {why}")
            if verbose and hyps:
                for h in hyps[:2]:
                    print(f"       [{h['confidence']:.2f}] {h['text'][:70]}")
            failed += 1

    # Verify all 11 AVA codes have at least one rule
    ava_codes = {
        "PCMISMATCH", "REGMISMATCH", "CSRMISMATCH", "MEMMISMATCH",
        "TRAPMISMATCH", "LENGTHMISMATCH", "SEQGAP", "X0WRITTEN",
        "ALIGNMENTERROR", "SCHEMAINVALID", "BINARYHASHMISMATCH",
    }
    missing_rules = ava_codes - set(HYPOTHESIS_DB.keys())
    if missing_rules:
        print(f"  ✗  Hypothesis DB missing rules for: {sorted(missing_rules)}")
        failed += 1
    else:
        print(f"  ✓  All 11 AVA codes have hypothesis rules")
        passed += 1

    print(f"{'─'*W}")
    print(f"  Results: {passed} passed, {failed} failed")
    print(f"{'═'*W}\n")
    return failed == 0


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    p = argparse.ArgumentParser(
        prog="bug_hypothesis.py",
        description=(
            f"bug_hypothesis.py v{__version__} — "
            "Generate ranked bug hypotheses from a bugreport.json"
        ),
    )
    p.add_argument("bugreport", nargs="?", default=None,
                   help="Path to bug_report.json produced by compare_commitlogs.py")
    p.add_argument("--max", type=int, default=5, metavar="N",
                   help="Maximum hypotheses to return (default 5)")
    p.add_argument("--min-confidence", type=float, default=0.50, metavar="F",
                   help="Minimum confidence threshold 0.0-1.0 (default 0.50)")
    p.add_argument("--json", action="store_true",
                   help="Output as JSON array instead of human-readable text")
    p.add_argument("--self-test", action="store_true",
                   help="Run built-in regression tests and exit")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)

    if args.self_test:
        return 0 if _run_self_tests(verbose=args.verbose) else 1

    if not args.bugreport:
        p.error("bugreport path is required (or use --self-test)")

    rpath = Path(args.bugreport)
    if not rpath.exists():
        print(f"ERROR: file not found: {rpath}", file=sys.stderr)
        return 2

    try:
        report = json.loads(rpath.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"ERROR reading {rpath}: {exc}", file=sys.stderr)
        return 2

    hypotheses = generate_hypotheses(
        report,
        max_results=args.max,
        min_confidence=args.min_confidence,
    )

    if not hypotheses:
        print("No hypotheses generated (no mismatches in report or "
              "all rules below confidence threshold).")
        return 0

    if args.json:
        print(json.dumps(hypotheses, indent=2))
        return 0

    mtype = (report.get("mismatches") or [{}])[0].get("mismatch_type", "?")
    print(f"\nHypotheses for {mtype} mismatch "
          f"(step {(report.get('mismatches') or [{}])[0].get('step','?')}):")
    print("─" * 72)
    for i, h in enumerate(hypotheses, 1):
        conf_bar = "█" * round(h["confidence"] * 10)
        print(f"\n  [{i}] [{conf_bar:<10}] {h['confidence']:.0%} "
              f"— {h['category'].upper()}")
        print(f"      {h['text']}")
        if h.get("detail"):
            print(f"      ↳ {h['detail']}")
        if h.get("references"):
            print(f"      📖 {', '.join(h['references'])}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
