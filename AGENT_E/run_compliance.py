#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_compliance.py — Agent E: RISC-V Architectural Compliance Runner
====================================================================
RISCOF / riscv-arch-test style compliance verification integrated with
the AVA verification platform (ava.py).

Architecture
------------
  ┌─────────────────────────────────────────────────────────────┐
  │  ComplianceRunner.run()                                     │
  │  ┌──────────┐  ┌──────────────┐  ┌──────────┐  ┌────────┐ │
  │  │  Collect  │→│ Build (///)  │→│  Execute  │→│ Report │ │
  │  │  Tests    │  │  + Cache     │  │  Golden  │  │  JSON  │ │
  │  └──────────┘  └──────────────┘  │  + DUT   │  │  HTML  │ │
  │                                  │  Compare  │  │  JUnit │ │
  │                                  └──────────┘  └────────┘ │
  └─────────────────────────────────────────────────────────────┘

Key guarantees
--------------
* All 8 embedded RV32IM tests are complete and correct (HTIF tohost exit,
  correct signature layout, correct M-extension corner-case semantics).
* Parallel build and execution via ThreadPoolExecutor.
* SHA-256 build cache: a test is never recompiled if source is unchanged.
* Retry logic with exponential back-off around all subprocess calls.
* Pluggable DUT backends: Spike-fallback | external sim | Verilator (Agent B).
* Atomic file writes (write-to-temp then rename) for crash safety.
* JUnit XML output for CI/CD integration (GitHub Actions, Jenkins, etc.).
* RISCOF plugin-compatible API via `run_compliance_for_ava()`.

Usage
-----
    # Self-test (Spike golden == DUT → all PASS)
    python run_compliance.py --isa RV32IM

    # Real DUT via Agent B Verilator harness
    python run_compliance.py --isa RV32IM --dut-sim sim/run_rtl.py

    # Add tests from official riscv-arch-test checkout
    python run_compliance.py --isa RV32IM --arch-test-repo ~/riscv-arch-test

    # Parallel workers, verbose, custom output
    python run_compliance.py --isa RV32IM -j 8 -v --out-dir results/ci_run_1

Definition of done: ≥ 5 tests run end-to-end with signature comparison.
"""

from __future__ import annotations

__all__ = [
    "ComplianceRunner",
    "RunConfig",
    "TestRecord",
    "RunReport",
    "TestResult",
    "run_compliance_for_ava",
    "ComplianceError",
    "ToolNotFoundError",
    "BuildError",
    "SimulationError",
    "SignatureError",
]

import argparse
import concurrent.futures
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Dict, Generator, List, Optional, Sequence, Tuple

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

log = logging.getLogger("agent_e")


def _configure_logging(verbose: bool, log_file: Optional[Path] = None) -> None:
    """Configure root logger; idempotent."""
    level = logging.DEBUG if verbose else logging.INFO
    fmt   = "%(asctime)s [%(levelname)-8s] %(name)s — %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(level=level, format=fmt, handlers=handlers, force=True)
    log.setLevel(level)


# ─────────────────────────────────────────────────────────────────────────────
# Exception hierarchy
# ─────────────────────────────────────────────────────────────────────────────

class ComplianceError(RuntimeError):
    """Base class for all Agent-E errors."""


class ToolNotFoundError(ComplianceError):
    """A required external tool (spike, gcc, …) was not found."""


class BuildError(ComplianceError):
    """Test source failed to compile."""


class SimulationError(ComplianceError):
    """A simulator (golden or DUT) failed or timed out."""


class SignatureError(ComplianceError):
    """Signature file is missing, empty, or malformed."""


# ─────────────────────────────────────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────────────────────────────────────

class TestResult(str, Enum):
    PASS    = "PASS"
    FAIL    = "FAIL"
    ERROR   = "ERROR"
    PENDING = "PENDING"
    SKIPPED = "SKIPPED"


class ErrorClass(str, Enum):
    NONE      = "none"
    BUILD     = "build"
    GOLDEN    = "golden_run"
    DUT       = "dut_run"
    SIG_PARSE = "signature_parse"
    SIG_CMP   = "signature_compare"
    TOOL      = "tool"
    TIMEOUT   = "timeout"


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RunConfig:
    """All tunable parameters for a compliance run."""
    isa:             str           = "RV32IM"
    spike_bin:       str           = "spike"
    dut_sim:         Optional[str] = None      # path to Agent-B adapter or wrapper
    arch_test_repo:  Optional[Path] = None     # riscv-arch-test checkout
    out_dir:         Path          = Path("compliance_results")
    workers:         int           = 4         # kept for backward compatibility
    build_workers:   int           = 0         # 0 = use workers; CPU-bound
    run_workers:     int           = 0         # 0 = use workers * 2; I/O-bound
    max_mismatches:  int           = 3         # stop after N mismatches (0 = unlimited)
    timeout_build_s: int           = 120
    timeout_run_s:   int           = 60
    retry_max:       int           = 2
    retry_delay_s:   float         = 0.5
    verbose:         bool          = False
    keep_build:      bool          = True      # keep ELFs/logs after run
    use_cache:       bool          = True      # skip rebuild if source unchanged

    def __post_init__(self) -> None:
        self.isa      = self.isa.upper()
        self.out_dir  = Path(self.out_dir)
        if self.arch_test_repo:
            self.arch_test_repo = Path(self.arch_test_repo)
        if self.workers < 1:
            raise ValueError("workers must be >= 1")
        # Resolve effective worker counts: build is CPU-bound, run is I/O-bound
        if self.build_workers < 1:
            self.build_workers = self.workers
        if self.run_workers < 1:
            self.run_workers = self.workers * 2


# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TestRecord:
    """Mutable state tracked for one test throughout its lifecycle."""
    name:         str
    isa_subset:   str
    source:       Path

    elf:          Optional[Path]  = None
    golden_sig:   Optional[Path]  = None
    dut_sig:      Optional[Path]  = None
    result:       TestResult      = TestResult.PENDING
    error_class:  ErrorClass      = ErrorClass.NONE
    error_msg:    str             = ""
    mismatch_idx: int             = -1
    golden_words: List[str]       = field(default_factory=list)
    dut_words:    List[str]       = field(default_factory=list)
    build_time_s: float           = 0.0
    run_time_s:   float           = 0.0
    cache_hit:    bool            = False

    def set_error(self, cls: ErrorClass, msg: str) -> None:
        self.result      = TestResult.ERROR
        self.error_class = cls
        self.error_msg   = msg

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # Path → str
        for k in ("source", "elf", "golden_sig", "dut_sig"):
            d[k] = str(d[k]) if d[k] else ""
        d["result"]      = self.result.value
        d["error_class"] = self.error_class.value
        return d


@dataclass(frozen=True)
class RunReport:
    """Immutable summary produced at the end of a compliance run."""
    timestamp:     str
    isa:           str
    spike_bin:     str
    spike_version: str
    toolchain:     str
    run_dir:       str
    tests:         Tuple[Dict, ...]
    summary:       Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp":     self.timestamp,
            "isa":           self.isa,
            "spike_bin":     self.spike_bin,
            "spike_version": self.spike_version,
            "toolchain":     self.toolchain,
            "run_dir":       self.run_dir,
            "tests":         list(self.tests),
            "summary":       self.summary,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Constants: file names
# ─────────────────────────────────────────────────────────────────────────────

REPORT_JSON   = "compliance_report.json"
REPORT_HTML   = "compliance_report.html"
REPORT_JUNIT  = "compliance_report_junit.xml"

# ─────────────────────────────────────────────────────────────────────────────
# Assembly / linker constants
# ─────────────────────────────────────────────────────────────────────────────

LINK_ADDRESS    = 0x8000_0000
SIG_ALIGN       = 8          # bytes
MAX_SIG_WORDS   = 16         # result words per test (= 64 bytes)
SIG_REGION_SZ   = MAX_SIG_WORDS * 4

# ─────────────────────────────────────────────────────────────────────────────
# Assembly macros
# ─────────────────────────────────────────────────────────────────────────────
# Correctness notes:
#   • RVTEST_PASS uses the HTIF tohost mechanism — NOT ecall/syscall.
#     Spike's --signature mode requires the program to write 1 to the
#     'tohost' symbol to signal successful completion.  Using ecall
#     would require a trap-handler stub which we don't provide.
#   • tohost/fromhost are defined in RVTEST_DATA_BEGIN (placed in the
#     .tohost section) so the linker can find them.
#   • SIG_INIT / WRITE_SIG use x29 as the moving signature pointer;
#     x29 (t4) is caller-saved and never touched by the code under test.
# ─────────────────────────────────────────────────────────────────────────────

_ASM_MACROS = r"""
/* ====================================================================
   Inlined riscv-arch-test-compatible macros — Agent E / run_compliance.py
   ==================================================================== */

/* ── Structural macros (C preprocessor, single-line) ─────────────── */

/* Start of test code — lands in .text.init */
#define RVTEST_CODE_BEGIN \
    .section .text.init,"ax",@progbits; .globl _start; _start:

#define RVTEST_CODE_END

/* Data section: defines tohost/fromhost for HTIF, then user data */
#define RVTEST_DATA_BEGIN \
    .section .tohost,"aw",@progbits; \
    .align 6; \
    .globl tohost;    tohost:    .dword 0; \
    .globl fromhost;  fromhost:  .dword 0; \
    .section .data.string,"aw",@progbits

#define RVTEST_DATA_END

/* ── GAS macros ───────────────────────────────────────────────────── */

/* Initialise x29 (t4) as signature write pointer */
.macro SIG_INIT
    la      x29, begin_signature
.endm

/* Write one 32-bit word to signature region and advance pointer */
.macro WRITE_SIG reg
    sw      \reg, 0(x29)
    addi    x29, x29, 4
.endm

/*
 * RVTEST_PASS — signal pass via HTIF tohost write.
 * Spike monitors the tohost address; writing 1 causes it to exit with
 * success and dump the signature file.  The infinite loop after the
 * store is intentional: it prevents PC run-off while Spike processes
 * the write.
 */
.macro RVTEST_PASS
    li      t0, 1
    la      t1, tohost
    sw      t0, 0(t1)
.Lpass_spin\@:
    j       .Lpass_spin\@
.endm

/*
 * RVTEST_FAIL code — write (code<<1)|1 to tohost, which Spike
 * reports as a failing test exit code.
 */
.macro RVTEST_FAIL code=99
    li      t0, ((\code) << 1) | 1
    la      t1, tohost
    sw      t0, 0(t1)
.Lfail_spin\@:
    j       .Lfail_spin\@
.endm
"""

# ─────────────────────────────────────────────────────────────────────────────
# Linker script
# ─────────────────────────────────────────────────────────────────────────────
# Layout (low → high address):
#   0x80000000  .text.init      (code)
#               .data.string    (test data, scratch words)
#               .tohost         (64-byte aligned; tohost @ base, fromhost @ +8)
#               .bss
#               begin_signature (SIG_ALIGN-aligned)
#               .signature      (MAX_SIG_WORDS * 4 bytes, zero-filled)
#               end_signature
#
# FILL(0x00000000) ensures unwritten signature words compare equal between
# golden and DUT when both load from the same ELF.
# ─────────────────────────────────────────────────────────────────────────────

_LINK_SCRIPT = """\
OUTPUT_ARCH(riscv)
ENTRY(_start)

SECTIONS {{
    . = {load:#010x};

    .text.init   :  {{ *(.text.init) *(.text*) }}
    .data.string : ALIGN(8) {{ *(.data.string) *(.data*) *(.rodata*) }}
    .tohost      : ALIGN(64) {{ *(.tohost) }}
    .bss         : ALIGN(8)  {{ *(.bss) *(COMMON) . = ALIGN(8); }}

    . = ALIGN({sig_align});
    begin_signature = .;
    .signature   : {{ FILL(0x00000000); . += {sig_size}; }}
    end_signature = .;

    /DISCARD/ : {{ *(.riscv.attributes) *(.comment) *(.note*) }}
}}
"""

# ─────────────────────────────────────────────────────────────────────────────
# Embedded compliance tests
# ─────────────────────────────────────────────────────────────────────────────
# Format: (test_name, isa_subset, code_body)
# code_body is placed between RVTEST_CODE_BEGIN and RVTEST_CODE_END.
# Data (if any) goes after RVTEST_DATA_BEGIN in the template.
#
# Semantic coverage:
#   ADD-01        RV32I  arithmetic, overflow wrapping
#   SUB-01        RV32I  subtraction, underflow wrapping
#   LOGICAL-01    RV32I  AND / OR / XOR / NOT (via xori)
#   SHIFT-01      RV32I  SLL / SRL / SRA corner cases (shift by 0, 31)
#   BRANCH-01     RV32I  BEQ/BNE/BLT/BGE/BLTU/BGEU both taken/not-taken
#   LOAD-STORE-01 RV32I  SW/LW, SB/LBU, SH/LHU/LH (sign extension)
#   MUL-01        RV32M  MUL/MULH/MULHSU/MULHU — sign combinations
#   DIV-01        RV32M  DIV/REM/DIVU/REMU — div-by-zero + overflow per spec
# ─────────────────────────────────────────────────────────────────────────────

_EMBEDDED_TESTS: List[Tuple[str, str, str, str]] = [
    # (name, subset, code_body, data_body)

    # ── ADD ─────────────────────────────────────────────────────────────────
    ("ADD-01", "RV32I", r"""
        SIG_INIT
        /* 5 + 3 = 8 */
        li      t0, 5
        li      t1, 3
        add     t2, t0, t1
        WRITE_SIG t2
        /* -1 + 1 = 0 */
        li      t0, -1
        li      t1, 1
        add     t2, t0, t1
        WRITE_SIG t2
        /* 0x7FFFFFFF + 1 = 0x80000000  (signed overflow wraps) */
        li      t0, 0x7FFFFFFF
        li      t1, 1
        add     t2, t0, t1
        WRITE_SIG t2
        /* 0 + 0 = 0 (using x0) */
        add     t2, x0, x0
        WRITE_SIG t2
        /* 0xFFFFFFFF + 0xFFFFFFFF = 0xFFFFFFFE (mod 2^32) */
        li      t0, -1
        li      t1, -1
        add     t2, t0, t1
        WRITE_SIG t2
        RVTEST_PASS
    """, ""),

    # ── SUB ─────────────────────────────────────────────────────────────────
    ("SUB-01", "RV32I", r"""
        SIG_INIT
        /* 10 - 3 = 7 */
        li      t0, 10
        li      t1, 3
        sub     t2, t0, t1
        WRITE_SIG t2
        /* 0 - 1 = 0xFFFFFFFF */
        li      t0, 0
        li      t1, 1
        sub     t2, t0, t1
        WRITE_SIG t2
        /* 0x80000000 - 1 = 0x7FFFFFFF */
        li      t0, 0x80000000
        li      t1, 1
        sub     t2, t0, t1
        WRITE_SIG t2
        /* x - x = 0 */
        li      t0, 0xCAFEBABE
        sub     t2, t0, t0
        WRITE_SIG t2
        /* 0 - 0x80000000 = 0x80000000 */
        li      t0, 0
        li      t1, 0x80000000
        sub     t2, t0, t1
        WRITE_SIG t2
        RVTEST_PASS
    """, ""),

    # ── LOGICAL: AND / OR / XOR / NOT ───────────────────────────────────────
    ("LOGICAL-01", "RV32I", r"""
        SIG_INIT
        /* AND: 0xFF & 0x0F = 0x0F */
        li      t0, 0xFF
        li      t1, 0x0F
        and     t2, t0, t1
        WRITE_SIG t2
        /* OR:  0xF0 | 0x0F = 0xFF */
        li      t0, 0xF0
        li      t1, 0x0F
        or      t2, t0, t1
        WRITE_SIG t2
        /* XOR: 0xAAAAAAAA ^ 0x55555555 = 0xFFFFFFFF */
        li      t0, 0xAAAAAAAA
        li      t1, 0x55555555
        xor     t2, t0, t1
        WRITE_SIG t2
        /* AND with x0 = 0 */
        li      t0, 0xDEADBEEF
        and     t2, t0, x0
        WRITE_SIG t2
        /* NOT via XORI -1: ~0xDEADBEEF = 0x21524110 */
        li      t0, 0xDEADBEEF
        xori    t2, t0, -1
        WRITE_SIG t2
        /* OR with -1 = 0xFFFFFFFF */
        li      t0, 0x12345678
        li      t1, -1
        or      t2, t0, t1
        WRITE_SIG t2
        RVTEST_PASS
    """, ""),

    # ── SHIFT ────────────────────────────────────────────────────────────────
    ("SHIFT-01", "RV32I", r"""
        SIG_INIT
        /* SLL: 1 << 4 = 16 */
        li      t0, 1
        li      t1, 4
        sll     t2, t0, t1
        WRITE_SIG t2
        /* SRL: 0x80000000 >> 1 = 0x40000000  (logical, no sign extend) */
        li      t0, 0x80000000
        li      t1, 1
        srl     t2, t0, t1
        WRITE_SIG t2
        /* SRA: 0x80000000 >> 1 = 0xC0000000  (arithmetic, sign extends) */
        li      t0, 0x80000000
        li      t1, 1
        sra     t2, t0, t1
        WRITE_SIG t2
        /* SLL by 0 = identity */
        li      t0, 0xDEADBEEF
        sll     t2, t0, x0
        WRITE_SIG t2
        /* SRL by 31 */
        li      t0, 0x80000000
        li      t1, 31
        srl     t2, t0, t1
        WRITE_SIG t2
        /* SRA by 31: 0x80000000 >> 31 = 0xFFFFFFFF */
        li      t0, 0x80000000
        li      t1, 31
        sra     t2, t0, t1
        WRITE_SIG t2
        /* Only low 5 bits of shift amount used: shift by 32 ≡ shift by 0 */
        li      t0, 1
        li      t1, 32
        sll     t2, t0, t1
        WRITE_SIG t2
        RVTEST_PASS
    """, ""),

    # ── BRANCH ───────────────────────────────────────────────────────────────
    ("BRANCH-01", "RV32I", r"""
        SIG_INIT
        /* BEQ taken: 5 == 5 → write 1 */
        li      t0, 5
        li      t1, 5
        beq     t0, t1, .Lbeq_taken
        li      t2, 0xBAD0
        j       .Lbeq_done
.Lbeq_taken:
        li      t2, 1
.Lbeq_done:
        WRITE_SIG t2

        /* BEQ not taken: 5 != 6 → write 2 */
        li      t0, 5
        li      t1, 6
        li      t2, 2
        beq     t0, t1, .Lbeq2_bad
        j       .Lbeq2_done
.Lbeq2_bad:
        li      t2, 0xBAD1
.Lbeq2_done:
        WRITE_SIG t2

        /* BNE taken: 3 != 7 → write 3 */
        li      t0, 3
        li      t1, 7
        bne     t0, t1, .Lbne_taken
        li      t2, 0xBAD2
        j       .Lbne_done
.Lbne_taken:
        li      t2, 3
.Lbne_done:
        WRITE_SIG t2

        /* BLT signed: -1 < 1 → taken → write 4 */
        li      t0, -1
        li      t1, 1
        blt     t0, t1, .Lblt_taken
        li      t2, 0xBAD3
        j       .Lblt_done
.Lblt_taken:
        li      t2, 4
.Lblt_done:
        WRITE_SIG t2

        /* BGE signed: 5 >= 5 → taken → write 5 */
        li      t0, 5
        li      t1, 5
        bge     t0, t1, .Lbge_taken
        li      t2, 0xBAD4
        j       .Lbge_done
.Lbge_taken:
        li      t2, 5
.Lbge_done:
        WRITE_SIG t2

        /* BLTU unsigned: 1 < 0xFFFFFFFF → taken → write 6 */
        li      t0, 1
        li      t1, -1
        bltu    t0, t1, .Lbltu_taken
        li      t2, 0xBAD5
        j       .Lbltu_done
.Lbltu_taken:
        li      t2, 6
.Lbltu_done:
        WRITE_SIG t2

        /* BGEU unsigned: 0xFFFFFFFF >= 1 → taken → write 7 */
        li      t0, -1
        li      t1, 1
        bgeu    t0, t1, .Lbgeu_taken
        li      t2, 0xBAD6
        j       .Lbgeu_done
.Lbgeu_taken:
        li      t2, 7
.Lbgeu_done:
        WRITE_SIG t2
        RVTEST_PASS
    """, ""),

    # ── LOAD / STORE ─────────────────────────────────────────────────────────
    # scratch_word lives in .data.string (declared in RVTEST_DATA_BEGIN).
    # We la-load its address at runtime — no section switching mid-test.
    ("LOAD-STORE-01", "RV32I", r"""
        SIG_INIT
        la      t3, scratch_word   /* t3 = &scratch_word in .data.string */

        /* SW / LW round-trip */
        li      t0, 0xDEADBEEF
        sw      t0, 0(t3)
        lw      t2, 0(t3)
        WRITE_SIG t2

        /* SB / LBU: store 0xAB, load unsigned → 0x000000AB */
        li      t0, 0xAB
        sb      t0, 0(t3)
        lbu     t2, 0(t3)
        WRITE_SIG t2

        /* SH / LHU: store 0x1234, load unsigned → 0x00001234 */
        li      t0, 0x1234
        sh      t0, 0(t3)
        lhu     t2, 0(t3)
        WRITE_SIG t2

        /* SH / LH: store 0x8001, load signed → 0xFFFF8001 */
        li      t0, 0x8001
        sh      t0, 0(t3)
        lh      t2, 0(t3)
        WRITE_SIG t2

        /* LB sign-extends: store 0xFF, load signed byte → 0xFFFFFFFF */
        li      t0, 0xFF
        sb      t0, 0(t3)
        lb      t2, 0(t3)
        WRITE_SIG t2
        RVTEST_PASS
    """, "    scratch_word: .word 0"),   # ← data body

    # ── MUL ─────────────────────────────────────────────────────────────────
    # RV32M multiply corner cases per ISA spec §M.
    # mulh  = signed   × signed   → high 32 bits
    # mulhu = unsigned × unsigned → high 32 bits
    # mulhsu= signed   × unsigned → high 32 bits
    ("MUL-01", "RV32M", r"""
        SIG_INIT
        /* 3 * 4 = 12 */
        li      t0, 3
        li      t1, 4
        mul     t2, t0, t1
        WRITE_SIG t2

        /* -1 * 1 = -1 = 0xFFFFFFFF (low 32 bits) */
        li      t0, -1
        li      t1, 1
        mul     t2, t0, t1
        WRITE_SIG t2

        /* 0 * any = 0 */
        li      t1, 0xDEADBEEF
        mul     t2, x0, t1
        WRITE_SIG t2

        /* MULH: 0x7FFFFFFF * 0x7FFFFFFF high word
           full = 0x3FFFFFFF_00000001 → high = 0x3FFFFFFF */
        li      t0, 0x7FFFFFFF
        li      t1, 0x7FFFFFFF
        mulh    t2, t0, t1
        WRITE_SIG t2

        /* MULHU: 0xFFFFFFFF * 0xFFFFFFFF high word
           full unsigned = 0xFFFFFFFE_00000001 → high = 0xFFFFFFFE */
        li      t0, -1
        li      t1, -1
        mulhu   t2, t0, t1
        WRITE_SIG t2

        /* MULHSU: signed 0x80000000 * unsigned 0xFFFFFFFF
           = -2^31 * (2^32-1) = -2^63+2^31 = 0x8000_0000_8000_0000
           → high = 0x80000000 */
        li      t0, 0x80000000
        li      t1, -1
        mulhsu  t2, t0, t1
        WRITE_SIG t2

        /* MUL -1 * -1 = 1 (low 32 bits of (-1)*(-1) = 1) */
        li      t0, -1
        li      t1, -1
        mul     t2, t0, t1
        WRITE_SIG t2
        RVTEST_PASS
    """, ""),

    # ── DIV / REM (signed) ───────────────────────────────────────────────────
    # All spec-mandated corner cases per RISC-V ISA §M.2.
    ("DIV-01", "RV32M", r"""
        SIG_INIT
        /* 20 / 5 = 4 */
        li      t0, 20
        li      t1, 5
        div     t2, t0, t1
        WRITE_SIG t2

        /* -20 / 5 = -4 */
        li      t0, -20
        li      t1, 5
        div     t2, t0, t1
        WRITE_SIG t2

        /* DIV by zero → -1  (0xFFFFFFFF)  [spec §M.2] */
        li      t0, 42
        div     t2, t0, x0
        WRITE_SIG t2

        /* REM by zero → dividend (42)  [spec §M.2] */
        rem     t2, t0, x0
        WRITE_SIG t2

        /* Signed overflow: INT_MIN / -1 → INT_MIN  [spec §M.2] */
        li      t0, 0x80000000
        li      t1, -1
        div     t2, t0, t1
        WRITE_SIG t2

        /* Signed overflow: INT_MIN rem -1 → 0  [spec §M.2] */
        rem     t2, t0, t1
        WRITE_SIG t2

        /* 17 / 5 = 3 */
        li      t0, 17
        li      t1, 5
        div     t2, t0, t1
        WRITE_SIG t2

        /* 17 rem 5 = 2 */
        rem     t2, t0, t1
        WRITE_SIG t2
        RVTEST_PASS
    """, ""),

    # ── DIVU / REMU (unsigned) ───────────────────────────────────────────────
    ("DIVU-REMU-01", "RV32M", r"""
        SIG_INIT
        /* DIVU: 20 / 5 = 4 */
        li      t0, 20
        li      t1, 5
        divu    t2, t0, t1
        WRITE_SIG t2

        /* DIVU: 0xFFFFFFFF / 2 = 0x7FFFFFFF */
        li      t0, -1
        li      t1, 2
        divu    t2, t0, t1
        WRITE_SIG t2

        /* DIVU by zero → 0xFFFFFFFF  [spec §M.2] */
        li      t0, 99
        divu    t2, t0, x0
        WRITE_SIG t2

        /* REMU by zero → dividend (99)  [spec §M.2] */
        remu    t2, t0, x0
        WRITE_SIG t2

        /* 17 remu 5 = 2 */
        li      t0, 17
        li      t1, 5
        remu    t2, t0, t1
        WRITE_SIG t2

        /* DIVU: 0 / 1 = 0 */
        divu    t2, x0, t1
        WRITE_SIG t2
        RVTEST_PASS
    """, ""),
]

# ─────────────────────────────────────────────────────────────────────────────
# Retry helper
# ─────────────────────────────────────────────────────────────────────────────

class RetryPolicy:
    """Retry a callable with exponential back-off."""

    def __init__(self, max_attempts: int = 2, base_delay_s: float = 0.5) -> None:
        self.max_attempts = max(1, max_attempts)
        self.base_delay   = base_delay_s

    def __call__(self, fn: Callable, *args: Any, **kwargs: Any) -> Any:
        last_exc: Exception = RuntimeError("no attempts made")
        for attempt in range(self.max_attempts):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                last_exc = exc
                if attempt < self.max_attempts - 1:
                    delay = self.base_delay * (2 ** attempt)
                    log.debug("Retry %d/%d after %.1fs: %s",
                              attempt + 1, self.max_attempts, delay, exc)
                    time.sleep(delay)
        raise last_exc


# ─────────────────────────────────────────────────────────────────────────────
# Build cache
# ─────────────────────────────────────────────────────────────────────────────

class BuildCache:
    """
    SHA-256 based cache: if the source file hasn't changed since the last
    build, return the previously compiled ELF immediately.
    """

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._index_path = cache_dir / "cache_index.json"
        self._index: Dict[str, str] = self._load_index()

    def _load_index(self) -> Dict[str, str]:
        if self._index_path.exists():
            try:
                return json.loads(self._index_path.read_text())
            except Exception:
                pass
        return {}

    def _save_index(self) -> None:
        _atomic_write(self._index_path, json.dumps(self._index, indent=2))

    @staticmethod
    def _hash_file(path: Path) -> str:
        h = hashlib.sha256()
        h.update(path.read_bytes())
        return h.hexdigest()

    def lookup(self, src: Path, elf_target: Path) -> bool:
        """Return True and copy cached ELF if src hasn't changed."""
        key        = str(src.resolve())
        current_h  = self._hash_file(src)
        cached_elf = self.cache_dir / f"{hashlib.sha256(key.encode()).hexdigest()[:32]}.elf"

        if self._index.get(key) == current_h and cached_elf.exists():
            shutil.copy2(cached_elf, elf_target)
            log.debug("Cache hit: %s", src.name)
            return True
        return False

    def store(self, src: Path, elf_path: Path) -> None:
        """Store a freshly built ELF in the cache."""
        key       = str(src.resolve())
        src_hash  = self._hash_file(src)
        cache_elf = self.cache_dir / f"{hashlib.sha256(key.encode()).hexdigest()[:32]}.elf"
        shutil.copy2(elf_path, cache_elf)
        self._index[key] = src_hash
        self._save_index()


# ─────────────────────────────────────────────────────────────────────────────
# Atomic file write
# ─────────────────────────────────────────────────────────────────────────────

def _atomic_write(path: Path, content: str, encoding: str = "utf-8") -> None:
    """Write content to path atomically via a sibling temp file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp_")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ─────────────────────────────────────────────────────────────────────────────
# Tool probing
# ─────────────────────────────────────────────────────────────────────────────

_SPIKE_VERSION_CACHE: Dict[str, Tuple[bool, str]] = {}

def probe_spike(spike_bin: str) -> Tuple[bool, str]:
    """Return (found, version_string). Cached after first call."""
    if spike_bin in _SPIKE_VERSION_CACHE:
        return _SPIKE_VERSION_CACHE[spike_bin]
    if not shutil.which(spike_bin):
        result = (False, "")
    else:
        try:
            out = subprocess.check_output(
                [spike_bin, "--version"],
                stderr=subprocess.STDOUT,
                timeout=5,
                text=True,
            )
            result = (True, out.strip().splitlines()[0])
        except subprocess.TimeoutExpired:
            result = (True, "timeout")
        except subprocess.CalledProcessError as exc:
            # Some Spike builds exit non-zero for --version
            text = (exc.output or "").strip()
            result = (True, text.splitlines()[0] if text else "unknown")
        except Exception:
            result = (True, "unknown")
    _SPIKE_VERSION_CACHE[spike_bin] = result
    return result


_TOOLCHAIN_CACHE: Optional[Tuple[Optional[str], Optional[str]]] = None

def probe_toolchain() -> Tuple[Optional[str], Optional[str]]:
    """Return (gcc_path, objdump_path). Cached after first call."""
    global _TOOLCHAIN_CACHE
    if _TOOLCHAIN_CACHE is not None:
        return _TOOLCHAIN_CACHE
    prefixes = [
        "riscv32-unknown-elf-",
        "riscv64-unknown-elf-",
        "riscv-none-elf-",
        "riscv-none-embed-",
    ]
    for pfx in prefixes:
        gcc     = shutil.which(f"{pfx}gcc")
        objdump = shutil.which(f"{pfx}objdump")
        if gcc and objdump:
            _TOOLCHAIN_CACHE = (gcc, objdump)
            return _TOOLCHAIN_CACHE
    _TOOLCHAIN_CACHE = (None, None)
    return _TOOLCHAIN_CACHE


# ─────────────────────────────────────────────────────────────────────────────
# Source / linker script generation
# ─────────────────────────────────────────────────────────────────────────────

def _generate_source(
    name: str,
    isa_subset: str,
    code_body: str,
    data_body: str,
) -> str:
    march = "rv32im" if isa_subset == "RV32M" else "rv32i"
    return textwrap.dedent(f"""\
        /* AUTO-GENERATED by Agent E (run_compliance.py) — DO NOT EDIT */
        /* test={name}  isa_subset={isa_subset}  arch={march} */

        {_ASM_MACROS}

        RVTEST_CODE_BEGIN

        {textwrap.dedent(code_body)}

        RVTEST_CODE_END

        RVTEST_DATA_BEGIN
        {textwrap.dedent(data_body)}
        RVTEST_DATA_END
    """)


def _write_linker_script(ld_path: Path) -> None:
    _atomic_write(
        ld_path,
        _LINK_SCRIPT.format(
            load     = LINK_ADDRESS,
            sig_align = SIG_ALIGN,
            sig_size  = SIG_REGION_SZ,
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Build
# ─────────────────────────────────────────────────────────────────────────────

def _march_for(isa_subset: str) -> str:
    return "rv32im" if isa_subset == "RV32M" else "rv32i"


def build_test(
    tc:        TestRecord,
    gcc:       str,
    ld_path:   Path,
    build_dir: Path,
    cache:     Optional[BuildCache],
    retry:     RetryPolicy,
    timeout:   int,
) -> bool:
    """
    Compile tc.source → ELF.  Returns True on success.
    Tries march with _zicsr suffix (GCC ≥ 12) then without (older GCC).
    """
    build_dir.mkdir(parents=True, exist_ok=True)
    elf_path = build_dir / f"{tc.name}.elf"
    tc.elf   = elf_path

    # Cache look-up
    if cache and cache.lookup(tc.source, elf_path):
        tc.cache_hit    = True
        tc.build_time_s = 0.0
        log.info("[%s] cache hit — skipping build", tc.name)
        return True

    march_base = _march_for(tc.isa_subset)
    mabi       = "ilp32"
    # Extra flags: -g for debug info, -O0 for predictable code layout
    common_flags = [
        "-nostdlib", "-static", "-ffreestanding",
        "-g", "-O0",
        f"-T{ld_path}",
        str(tc.source),
        "-o", str(elf_path),
    ]

    t0 = time.monotonic()
    last_proc: Optional[subprocess.CompletedProcess] = None

    def _try_build(march: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [gcc, f"-march={march}", f"-mabi={mabi}"] + common_flags,
            capture_output=True, text=True, timeout=timeout,
        )

    # Try _zicsr variant (required in GCC ≥ 12 for CSR access)
    for march in [f"{march_base}_zicsr", march_base]:
        try:
            proc = retry(_try_build, march)
        except subprocess.TimeoutExpired:
            tc.set_error(ErrorClass.BUILD, f"Build timed out after {timeout}s")
            return False
        except Exception as exc:
            tc.set_error(ErrorClass.BUILD, f"Build subprocess error: {exc}")
            return False
        last_proc = proc
        if proc.returncode == 0:
            break

    tc.build_time_s = time.monotonic() - t0

    if last_proc is None or last_proc.returncode != 0:
        stderr = (last_proc.stderr if last_proc else "").strip()
        tc.set_error(ErrorClass.BUILD, f"Compile failed:\n{stderr[:600]}")
        log.error("[%s] Build FAILED (%.2fs):\n%s", tc.name, tc.build_time_s, stderr[:300])
        return False

    if cache:
        cache.store(tc.source, elf_path)

    log.info("[%s] Built ELF in %.2fs (march=%s)", tc.name, tc.build_time_s, march_base)
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Signature handling
# ─────────────────────────────────────────────────────────────────────────────

def parse_signature(sig_path: Path) -> List[str]:
    """
    Parse a RISCOF-style signature file into a list of normalised 8-char
    lowercase hex strings.  Each non-comment line is one 32-bit word.
    Raises SignatureError on empty result.
    """
    if not sig_path.exists():
        raise SignatureError(f"Signature file not found: {sig_path}")

    words: List[str] = []
    for raw in sig_path.read_text(errors="replace").splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#") or raw.startswith("//"):
            continue
        # Strip "0x" prefix exactly (lstrip("0x") is wrong: it strips ALL
        # leading '0' and 'x' characters, mangling e.g. "0xabcd" correctly
        # but also "xx0001" into "1").
        clean = raw.lower()
        if clean.startswith("0x"):
            clean = clean[2:]
        clean = clean or "0"
        # Validate: must be 1-8 hex characters
        if not all(c in "0123456789abcdef" for c in clean):
            log.warning("Non-hex signature line skipped: %r", raw)
            continue
        words.append(clean.zfill(8))

    return words


def compare_signatures(
    golden: List[str],
    dut:    List[str],
    *,
    max_mismatches: Optional[int] = None,
) -> Tuple[bool, int, List[Tuple[int, str, str]]]:
    """
    Compare two word lists.

    Parameters
    ----------
    golden / dut      : lists of 8-char lowercase hex strings.
    max_mismatches    : stop collecting after this many mismatches (None = unlimited).
                        The runner uses max_mismatches=3 to bound log output on
                        severely broken DUTs; pass None for exhaustive comparison.

    Returns
    -------
    (passed, first_mismatch_idx, collected_mismatches)

    collected_mismatches is a list of (idx, golden_word, dut_word) capped at
    max_mismatches entries.  first_mismatch_idx is -1 when identical.

    Padding rule: a missing word on either side is treated as "00000000".
    """
    length = max(len(golden), len(dut)) if (golden or dut) else 0
    mismatches: List[Tuple[int, str, str]] = []
    first_idx = -1

    for i in range(length):
        g = golden[i] if i < len(golden) else "00000000"
        d = dut[i]    if i < len(dut)    else "00000000"
        if g != d:
            mismatches.append((i, g, d))
            if first_idx == -1:
                first_idx = i
            if max_mismatches is not None and len(mismatches) >= max_mismatches:
                break

    return (len(mismatches) == 0), first_idx, mismatches


# ─────────────────────────────────────────────────────────────────────────────
# DUT backend abstraction
# ─────────────────────────────────────────────────────────────────────────────

class DUTBackend:
    """Abstract base class for DUT simulators."""

    name: str = "abstract"

    def run(
        self,
        elf:     Path,
        sig:     Path,
        isa:     str,
        timeout: int,
    ) -> None:
        """
        Run the DUT on `elf` and write signature to `sig`.
        Raises SimulationError on failure.
        """
        raise NotImplementedError


class SpikeDUTBackend(DUTBackend):
    """Spike as DUT — used for self-testing the compliance flow."""

    name = "spike-fallback"

    def __init__(self, spike_bin: str) -> None:
        self.spike_bin = spike_bin

    def run(self, elf: Path, sig: Path, isa: str, timeout: int) -> None:
        spike_isa = _normalise_isa(isa)
        cmd = [
            self.spike_bin,
            f"--isa={spike_isa}",
            f"--signature={sig}",
            str(elf),
        ]
        log.debug("[%s DUT] %s", self.name, " ".join(cmd))
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise SimulationError(f"Spike DUT timed out ({timeout}s): {elf.name}")
        except FileNotFoundError:
            raise SimulationError(
                f"Spike binary not found: '{self.spike_bin}'. "
                "Install from https://github.com/riscv-software-src/riscv-isa-sim"
            )

        if not (sig.exists() and sig.stat().st_size > 0):
            raise SimulationError(
                f"Spike DUT produced no signature for {elf.name}.\n"
                f"  exit={proc.returncode}\n"
                f"  stderr: {proc.stderr[:300]}"
            )


class ExternalDUTBackend(DUTBackend):
    """
    Arbitrary external DUT wrapper (e.g. Agent B's sim/run_rtl.py).
    Contract: <script> --elf <elf> --sig <sig> --isa <isa>
    """

    name = "external"

    def __init__(self, script_path: str) -> None:
        self.script = script_path

    def run(self, elf: Path, sig: Path, isa: str, timeout: int) -> None:
        cmd = [sys.executable, self.script, "--elf", str(elf), "--sig", str(sig), "--isa", isa]
        log.debug("[external DUT] %s", " ".join(cmd))
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise SimulationError(f"External DUT timed out ({timeout}s): {elf.name}")

        if proc.returncode != 0:
            log.warning(
                "[external DUT] exit=%d stderr: %s",
                proc.returncode, proc.stderr[:200],
            )
        if not (sig.exists() and sig.stat().st_size > 0):
            raise SimulationError(
                f"External DUT produced no signature for {elf.name}.\n"
                f"  exit={proc.returncode}\n  stderr: {proc.stderr[:300]}"
            )


def _normalise_isa(isa: str) -> str:
    """Convert 'RV32IM' → 'rv32im' (Spike format, no _zicsr suffix)."""
    s = isa.lower().replace("_zicsr", "").replace("_", "")
    return s if s.startswith("rv") else f"rv32{s}"


def _make_dut_backend(cfg: RunConfig) -> DUTBackend:
    if cfg.dut_sim:
        return ExternalDUTBackend(cfg.dut_sim)
    return SpikeDUTBackend(cfg.spike_bin)


# ─────────────────────────────────────────────────────────────────────────────
# Golden runner (Spike)
# ─────────────────────────────────────────────────────────────────────────────

def run_golden(
    elf:       Path,
    sig:       Path,
    spike_bin: str,
    isa:       str,
    timeout:   int,
    retry:     RetryPolicy,
) -> None:
    """
    Run Spike with --signature to produce the golden reference.
    Raises SimulationError on failure.
    """
    spike_isa = _normalise_isa(isa)
    cmd = [spike_bin, f"--isa={spike_isa}", f"--signature={sig}", str(elf)]
    log.debug("[golden] %s", " ".join(cmd))

    def _run() -> subprocess.CompletedProcess:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    try:
        proc = retry(_run)
    except subprocess.TimeoutExpired:
        raise SimulationError(f"Spike golden timed out ({timeout}s): {elf.name}")

    if not (sig.exists() and sig.stat().st_size > 0):
        raise SimulationError(
            f"Spike golden produced no signature for {elf.name}.\n"
            f"  exit={proc.returncode}\n"
            f"  stdout: {proc.stdout[:200]}\n"
            f"  stderr: {proc.stderr[:300]}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Test discovery from riscv-arch-test repository
# ─────────────────────────────────────────────────────────────────────────────

def discover_arch_tests(repo: Path, isa: str) -> List[Tuple[str, str, str, str]]:
    """
    Walk an riscv-arch-test checkout and return embedded-test-compatible
    tuples: (name, subset, code_body_placeholder, data_body_placeholder).

    Supports:
      v3 layout: riscv-test-suite/rv32i_m/{I,M}/src/*.S
      v2 layout: arch-test/{RV32I,RV32M}/src/*.S

    The returned code_body is a #include directive pointing at the source
    so the existing toolchain pipeline can build it.
    """
    if not repo or not repo.is_dir():
        return []

    use_m = "M" in isa.upper()
    results: List[Tuple[str, str, str, str]] = []

    layout_map = {
        "RV32I": [
            repo / "riscv-test-suite" / "rv32i_m" / "I",
            repo / "arch-test" / "RV32I",
        ],
        "RV32M": [
            repo / "riscv-test-suite" / "rv32i_m" / "M",
            repo / "arch-test" / "RV32M",
        ],
    }

    for subset, dirs in layout_map.items():
        if subset == "RV32M" and not use_m:
            continue
        for d in dirs:
            src_dir = d / "src"
            if not src_dir.is_dir():
                src_dir = d  # flat layout fallback
            for s in sorted(src_dir.glob("*.S")):
                # Placeholder bodies — the real source is the .S file itself
                results.append((s.stem, subset, f"    /* external: {s} */", ""))

    log.info("Discovered %d test(s) from repo %s", len(results), repo)
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Report generation
# ─────────────────────────────────────────────────────────────────────────────

def _result_badge(result: TestResult) -> str:
    colours = {
        TestResult.PASS:    ("#2da44e", "#ffffff"),
        TestResult.FAIL:    ("#cf222e", "#ffffff"),
        TestResult.ERROR:   ("#9a6700", "#ffffff"),
        TestResult.SKIPPED: ("#57606a", "#ffffff"),
        TestResult.PENDING: ("#0550ae", "#ffffff"),
    }
    bg, fg = colours.get(result, ("#555", "#fff"))
    return (
        f'<span style="background:{bg};color:{fg};padding:2px 10px;border-radius:4px;'
        f'font-size:0.82em;font-weight:700;letter-spacing:.4px">{result.value}</span>'
    )


def _sig_diff_html(
    golden: List[str],
    dut:    List[str],
    mismatches: List[Tuple[int, str, str]],
) -> str:
    if not golden and not dut:
        return "<em style='color:#aaa'>no signature data</em>"
    bad_idx = {m[0] for m in mismatches}
    rows: List[str] = []
    for i in range(max(len(golden), len(dut))):
        g  = golden[i] if i < len(golden) else "——"
        d  = dut[i]    if i < len(dut)    else "——"
        hl = ' style="background:#fff0f0"' if i in bad_idx else ""
        diff_sym = "✗" if i in bad_idx else ""
        rows.append(
            f'<tr{hl}>'
            f'<td style="text-align:right;color:#aaa;padding-right:6px">[{i}]</td>'
            f'<td><code>{g}</code></td>'
            f'<td><code>{d}</code></td>'
            f'<td style="color:#cf222e;font-weight:700">{diff_sym}</td>'
            f'</tr>'
        )
    return (
        '<table style="border-collapse:collapse;font-size:0.79em;width:100%">'
        '<thead><tr style="color:#57606a">'
        '<th style="text-align:right;padding-right:6px">idx</th>'
        '<th>Golden (Spike)</th><th>DUT</th><th></th>'
        '</tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table>'
    )


def render_html(report: RunReport) -> str:
    s     = report.summary
    p_pct = float(s.get("pass_rate_pct", 0))
    bar_c = "#2da44e" if p_pct == 100 else ("#cf222e" if p_pct == 0 else "#e6a817")

    tool_err_html = ""
    if s.get("tool_errors"):
        items = "".join(f"<li>{e}</li>" for e in s["tool_errors"])
        tool_err_html = (
            '<div style="background:#fff3cd;border:1px solid #e6a817;'
            'border-radius:6px;padding:12px 16px;margin-bottom:16px">'
            f'<strong>⚠ Tool errors</strong><ul style="margin:6px 0 0">{items}</ul>'
            '</div>'
        )

    rows: List[str] = []
    for t in report.tests:
        result    = TestResult(t["result"])
        mismatches: List[Tuple[int, str, str]] = [
            (idx, g, d)
            for idx, (g, d) in enumerate(
                zip(
                    t.get("golden_words", []),
                    t.get("dut_words", []) + [""] * MAX_SIG_WORDS,
                )
            )
            if g != (t.get("dut_words", ["00000000"] * MAX_SIG_WORDS)[idx]
                     if idx < len(t.get("dut_words", [])) else "00000000")
        ]
        sig_html = _sig_diff_html(
            t.get("golden_words", []),
            t.get("dut_words", []),
            mismatches,
        )
        rows.append(f"""
        <tr>
          <td><code style="font-size:0.9em">{t['name']}</code></td>
          <td style="color:#57606a;font-size:0.86em">{t['isa_subset']}</td>
          <td>{_result_badge(result)}</td>
          <td style="color:#57606a;font-size:0.82em">{t.get('error_msg') or '—'}</td>
          <td style="text-align:right;color:#57606a;font-size:0.83em">{t.get('build_time_s', 0):.2f}s</td>
          <td style="text-align:right;color:#57606a;font-size:0.83em">{t.get('run_time_s', 0):.2f}s</td>
        </tr>
        <tr>
          <td colspan="6" style="padding:4px 16px 14px 40px;background:#f6f8fa">
            {sig_html}
          </td>
        </tr>""")

    kpis = "".join(
        f'<div style="background:#f6f8fa;border-radius:6px;padding:14px;text-align:center">'
        f'<div style="font-size:1.9rem;font-weight:700;color:{c}">{v}</div>'
        f'<div style="font-size:0.78em;color:#57606a;margin-top:4px">{l}</div>'
        f'</div>'
        for v, c, l in [
            (s["total"],  "#24292f", "Total"),
            (s["pass"],   "#2da44e", "Pass"),
            (s["fail"],   "#cf222e", "Fail"),
            (s["error"],  "#9a6700", "Error"),
        ]
    )

    cache_hits  = sum(1 for t in report.tests if t.get("cache_hit"))
    total_build = sum(t.get("build_time_s", 0) for t in report.tests)
    total_run   = sum(t.get("run_time_s", 0)   for t in report.tests)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agent E — Compliance Report {report.timestamp[:10]}</title>
<style>
  *{{box-sizing:border-box}}
  body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
        margin:0;padding:28px 36px;background:#f0f2f5;color:#24292f;max-width:1200px}}
  h1{{font-size:1.5rem;margin:0 0 4px}}
  .meta{{color:#57606a;font-size:0.87em;margin-bottom:20px;line-height:1.6}}
  .card{{background:#fff;border:1px solid #d0d7de;border-radius:8px;
          padding:20px 24px;margin-bottom:20px;box-shadow:0 1px 3px rgba(0,0,0,.05)}}
  .card h2{{font-size:1rem;margin:0 0 14px;color:#24292f}}
  .kpi-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:14px}}
  .progress{{background:#e0e0e0;border-radius:4px;height:8px;margin:0 0 14px}}
  .fill{{height:8px;border-radius:4px;background:{bar_c};width:{p_pct:.1f}%}}
  .stats{{display:flex;gap:20px;flex-wrap:wrap;font-size:0.84em;color:#57606a}}
  table{{width:100%;border-collapse:collapse}}
  th{{text-align:left;padding:9px 12px;background:#f6f8fa;
      border-bottom:2px solid #d0d7de;font-size:0.82em;color:#57606a;white-space:nowrap}}
  td{{padding:9px 12px;border-bottom:1px solid #eaeef2;vertical-align:top}}
  code{{background:#f0f2f4;padding:1px 6px;border-radius:3px;font-size:0.88em;
        font-family:'SFMono-Regular',Consolas,'Liberation Mono',Menlo,monospace}}
  footer{{text-align:center;font-size:0.77em;color:#8c959f;margin-top:28px}}
</style>
</head>
<body>
<h1>⚙ Agent E — RISC-V Compliance Report</h1>
<div class="meta">
  ISA: <strong>{report.isa}</strong> &nbsp;·&nbsp;
  Spike: <code>{report.spike_version}</code> &nbsp;·&nbsp;
  GCC: <code>{Path(report.toolchain).name if report.toolchain != "not_found" else "not found"}</code>
  <br>Run dir: <code>{report.run_dir}</code> &nbsp;·&nbsp; {report.timestamp}
</div>

{tool_err_html}

<div class="card">
  <h2>Summary</h2>
  <div class="progress"><div class="fill"></div></div>
  <div class="kpi-grid">{kpis}</div>
  <div class="stats">
    <span>Pass rate: <strong>{p_pct:.1f}%</strong></span>
    <span>Cache hits: {cache_hits}/{len(report.tests)}</span>
    <span>Total build: {total_build:.1f}s</span>
    <span>Total run: {total_run:.1f}s</span>
  </div>
</div>

<div class="card">
  <h2>Test Results</h2>
  <table>
    <thead><tr>
      <th>Test</th><th>ISA Subset</th><th>Result</th>
      <th>Note</th><th>Build</th><th>Run</th>
    </tr></thead>
    <tbody>{"".join(rows)}</tbody>
  </table>
</div>

<footer>Generated by AVA Agent E · run_compliance.py · {datetime.now(timezone.utc).isoformat()}</footer>
</body>
</html>"""


def render_junit_xml(report: RunReport) -> str:
    """
    Produce a JUnit-compatible XML report for CI ingestion
    (GitHub Actions, Jenkins, GitLab CI, etc.).
    """
    ts      = report.timestamp
    tests   = report.tests
    n_fail  = report.summary["fail"] + report.summary["error"]
    n_skip  = report.summary.get("skipped", 0)

    suite = ET.Element("testsuite", {
        "name":      f"RISCV-Compliance-{report.isa}",
        "tests":     str(report.summary["total"]),
        "failures":  str(report.summary["fail"]),
        "errors":    str(report.summary["error"]),
        "skipped":   str(n_skip),
        "timestamp": ts,
        "hostname":  "agent_e",
    })

    for t in tests:
        case = ET.SubElement(suite, "testcase", {
            "classname": f"compliance.{t['isa_subset']}",
            "name":      t["name"],
            "time":      str(round(t.get("run_time_s", 0), 3)),
        })
        result = TestResult(t["result"])
        if result == TestResult.FAIL:
            fail = ET.SubElement(case, "failure", {
                "message": t.get("error_msg", "signature mismatch"),
                "type":    "SignatureMismatch",
            })
            if t.get("golden_words") and t.get("dut_words"):
                idx = t.get("mismatch_idx", -1)
                fail.text = (
                    f"First mismatch at word[{idx}]: "
                    f"golden={t['golden_words'][idx] if idx < len(t['golden_words']) else '??'} "
                    f"dut={t['dut_words'][idx]    if idx < len(t['dut_words'])    else '??'}"
                )
        elif result == TestResult.ERROR:
            ET.SubElement(case, "error", {
                "message": t.get("error_msg", "unknown error"),
                "type":    t.get("error_class", "error"),
            })
        elif result == TestResult.SKIPPED:
            ET.SubElement(case, "skipped")

    return ET.tostring(suite, encoding="unicode", xml_declaration=False)


# ─────────────────────────────────────────────────────────────────────────────
# Core runner
# ─────────────────────────────────────────────────────────────────────────────

class ComplianceRunner:
    """
    Orchestrates the full compliance pipeline:

      collect → build (parallel) → run golden+DUT (parallel) → compare → report

    All state is immutable after construction; results live in TestRecord objects.
    """

    def __init__(self, cfg: RunConfig) -> None:
        self.cfg         = cfg
        self.spike_found, self.spike_ver = probe_spike(cfg.spike_bin)
        self.gcc, self.objdump           = probe_toolchain()
        self.retry                       = RetryPolicy(cfg.retry_max, cfg.retry_delay_s)
        self.dut_backend                 = _make_dut_backend(cfg)
        log.info(
            "ComplianceRunner isa=%s spike=%s(%s) gcc=%s dut=%s workers=%d",
            cfg.isa,
            cfg.spike_bin,
            "ok" if self.spike_found else "MISSING",
            Path(self.gcc).name if self.gcc else "MISSING",
            self.dut_backend.name,
            cfg.workers,
        )

    # ── Public API ───────────────────────────────────────────────────────────

    def run(self) -> RunReport:
        """Execute the full compliance pipeline and return a RunReport."""
        ts      = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir = self.cfg.out_dir / ts
        run_dir.mkdir(parents=True, exist_ok=True)

        _configure_logging(self.cfg.verbose, run_dir / "compliance.log")
        log.info("=== Agent E run  isa=%s  dir=%s ===", self.cfg.isa, run_dir)

        tool_errors = self._validate_tools()
        if tool_errors:
            report = self._make_report(run_dir, [], tool_errors)
            self._write_reports(report, run_dir)
            return report

        records = self._collect(run_dir)
        log.info("%d test(s) collected", len(records))

        self._build_all(records, run_dir)
        self._run_all(records, run_dir)

        report = self._make_report(run_dir, records, [])
        self._write_reports(report, run_dir)

        s = report.summary
        log.info(
            "=== DONE  PASS=%d FAIL=%d ERROR=%d total=%d (%.1f%%) ===",
            s["pass"], s["fail"], s["error"], s["total"], s["pass_rate_pct"],
        )
        return report

    # ── Tool validation ──────────────────────────────────────────────────────

    def _validate_tools(self) -> List[str]:
        errors: List[str] = []
        if not self.spike_found:
            errors.append(
                f"Spike ISS not found: '{self.cfg.spike_bin}'. "
                "Install from https://github.com/riscv-software-src/riscv-isa-sim"
            )
        if not self.gcc:
            errors.append(
                "No RISC-V GCC toolchain found. "
                "Expected one of: riscv32-unknown-elf-gcc, riscv64-unknown-elf-gcc, "
                "riscv-none-elf-gcc.  Install from https://github.com/riscv-collab/riscv-gnu-toolchain"
            )
        if self.cfg.dut_sim and not Path(self.cfg.dut_sim).is_file():
            errors.append(f"DUT sim script not found: {self.cfg.dut_sim}")
        return errors

    # ── Test collection ──────────────────────────────────────────────────────

    def _collect(self, run_dir: Path) -> List[TestRecord]:
        src_dir = run_dir / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        use_m   = "M" in self.cfg.isa

        records: List[TestRecord] = []

        # 1) Embedded tests
        for name, subset, code_body, data_body in _EMBEDDED_TESTS:
            if subset == "RV32M" and not use_m:
                continue
            src = src_dir / f"{name}.S"
            _atomic_write(src, _generate_source(name, subset, code_body, data_body))
            records.append(TestRecord(name=name, isa_subset=subset, source=src))

        # 2) riscv-arch-test repo (optional)
        if self.cfg.arch_test_repo:
            for name, subset, code_body, data_body in discover_arch_tests(
                self.cfg.arch_test_repo, self.cfg.isa
            ):
                if subset == "RV32M" and not use_m:
                    continue
                # External tests: source IS the upstream .S file
                # We generate a thin wrapper that includes the original
                src = src_dir / f"ext_{name}.S"
                # For external tests, find the actual file referenced in code_body
                upstream = code_body.replace("    /* external: ", "").rstrip(" */")
                if Path(upstream).is_file():
                    src = Path(upstream)
                records.append(TestRecord(name=name, isa_subset=subset, source=src))

        return records

    # ── Build phase (separate CPU-bound pool) ───────────────────────────────

    def _build_all(self, records: List[TestRecord], run_dir: Path) -> None:
        build_dir  = run_dir / "build"
        ld_path    = run_dir / "compliance.ld"
        cache_dir  = self.cfg.out_dir / ".build_cache"
        _write_linker_script(ld_path)

        cache: Optional[BuildCache] = BuildCache(cache_dir) if self.cfg.use_cache else None

        def _build_one(tc: TestRecord) -> TestRecord:
            build_test(
                tc, self.gcc, ld_path, build_dir, cache,
                self.retry, self.cfg.timeout_build_s,
            )
            return tc

        n = len(records)
        log.info("Building %d test(s) — %d CPU-bound workers...", n, self.cfg.build_workers)
        with concurrent.futures.ThreadPoolExecutor(
            max_workers    = self.cfg.build_workers,
            thread_name_prefix = "build",
        ) as pool:
            futures = {pool.submit(_build_one, tc): tc for tc in records}
            for fut in concurrent.futures.as_completed(futures):
                tc = futures[fut]
                try:
                    fut.result()
                except Exception as exc:
                    tc.set_error(ErrorClass.BUILD, str(exc))

    # ── Run phase (separate I/O-bound pool) ──────────────────────────────────

    def _run_all(self, records: List[TestRecord], run_dir: Path) -> None:
        runnable = [tc for tc in records if tc.result == TestResult.PENDING]
        if not runnable:
            return

        def _run_one(tc: TestRecord) -> TestRecord:
            self._run_single(tc, run_dir)
            return tc

        log.info(
            "Running %d test(s) — %d I/O-bound workers...",
            len(runnable), self.cfg.run_workers,
        )
        with concurrent.futures.ThreadPoolExecutor(
            max_workers    = self.cfg.run_workers,
            thread_name_prefix = "run",
        ) as pool:
            futures = {pool.submit(_run_one, tc): tc for tc in runnable}
            for fut in concurrent.futures.as_completed(futures):
                tc = futures[fut]
                try:
                    fut.result()
                except Exception as exc:
                    tc.set_error(ErrorClass.DUT, f"Unexpected: {exc}")

    def _run_single(self, tc: TestRecord, run_dir: Path) -> None:
        """Full flow for one test: golden → DUT → compare."""
        sig_dir = run_dir / "signatures" / tc.name
        sig_dir.mkdir(parents=True, exist_ok=True)
        tc.golden_sig = sig_dir / "golden.sig"
        tc.dut_sig    = sig_dir / "dut.sig"

        t0 = time.monotonic()

        # Golden run
        try:
            run_golden(
                tc.elf, tc.golden_sig,
                self.cfg.spike_bin, self.cfg.isa,
                self.cfg.timeout_run_s, self.retry,
            )
        except SimulationError as exc:
            tc.set_error(ErrorClass.GOLDEN, str(exc))
            log.error("[%s] Golden FAILED: %s", tc.name, exc)
            return
        except Exception as exc:
            tc.set_error(ErrorClass.GOLDEN, f"Unexpected: {exc}")
            return

        # DUT run
        try:
            self.dut_backend.run(
                tc.elf, tc.dut_sig, self.cfg.isa, self.cfg.timeout_run_s,
            )
        except SimulationError as exc:
            tc.set_error(ErrorClass.DUT, str(exc))
            log.error("[%s] DUT FAILED: %s", tc.name, exc)
            return
        except Exception as exc:
            tc.set_error(ErrorClass.DUT, f"Unexpected: {exc}")
            return

        tc.run_time_s = time.monotonic() - t0

        # Parse signatures
        try:
            tc.golden_words = parse_signature(tc.golden_sig)
            tc.dut_words    = parse_signature(tc.dut_sig)
        except SignatureError as exc:
            tc.set_error(ErrorClass.SIG_PARSE, str(exc))
            return

        if not tc.golden_words:
            tc.set_error(
                ErrorClass.SIG_PARSE,
                "Empty golden signature — check begin_signature symbol in linker script",
            )
            return

        # Compare
        passed, first_mismatch, all_mismatches = compare_signatures(
            tc.golden_words, tc.dut_words,
            max_mismatches = self.cfg.max_mismatches if self.cfg.max_mismatches > 0 else None,
        )
        tc.mismatch_idx = first_mismatch

        if passed:
            tc.result = TestResult.PASS
            log.info(
                "[%s] PASS  words=%d  t=%.2fs%s",
                tc.name, len(tc.golden_words), tc.run_time_s,
                " (cache)" if tc.cache_hit else "",
            )
        else:
            tc.result      = TestResult.FAIL
            tc.error_class = ErrorClass.SIG_CMP
            g_w = tc.golden_words[first_mismatch] if first_mismatch < len(tc.golden_words) else "??"
            d_w = tc.dut_words[first_mismatch]    if first_mismatch < len(tc.dut_words)    else "??"
            tc.error_msg   = (
                f"Signature mismatch at word[{first_mismatch}]: "
                f"golden=0x{g_w}  dut=0x{d_w}  "
                f"({len(all_mismatches)} total diff(s))"
            )
            log.warning("[%s] FAIL  %s", tc.name, tc.error_msg)

    # ── Report assembly ──────────────────────────────────────────────────────

    def _make_report(
        self,
        run_dir:     Path,
        records:     List[TestRecord],
        tool_errors: List[str],
    ) -> RunReport:
        n       = len(records)
        n_pass  = sum(1 for t in records if t.result == TestResult.PASS)
        n_fail  = sum(1 for t in records if t.result == TestResult.FAIL)
        n_err   = sum(1 for t in records if t.result == TestResult.ERROR)
        n_skip  = sum(1 for t in records if t.result == TestResult.SKIPPED)

        return RunReport(
            timestamp     = datetime.now(timezone.utc).isoformat(),
            isa           = self.cfg.isa,
            spike_bin     = self.cfg.spike_bin,
            spike_version = self.spike_ver,
            toolchain     = self.gcc or "not_found",
            run_dir       = str(run_dir),
            tests         = tuple(tc.to_dict() for tc in records),
            summary       = {
                "total":         n,
                "pass":          n_pass,
                "fail":          n_fail,
                "error":         n_err,
                "skipped":       n_skip,
                "pass_rate_pct": round(n_pass / n * 100, 1) if n else 0.0,
                "tool_errors":   tool_errors,
                "run_dir":       str(run_dir),
            },
        )

    def _write_reports(self, report: RunReport, run_dir: Path) -> None:
        # JSON
        json_path = run_dir / REPORT_JSON
        _atomic_write(json_path, json.dumps(report.to_dict(), indent=2))
        log.info("JSON → %s", json_path)

        # HTML
        html_path = run_dir / REPORT_HTML
        _atomic_write(html_path, render_html(report))
        log.info("HTML → %s", html_path)

        # JUnit XML
        junit_path = run_dir / REPORT_JUNIT
        _atomic_write(junit_path, render_junit_xml(report))
        log.info("JUnit XML → %s", junit_path)

        # "latest" convenience symlinks / copies in out_dir root
        for src, name in [
            (json_path,  REPORT_JSON),
            (html_path,  REPORT_HTML),
            (junit_path, REPORT_JUNIT),
        ]:
            dst = self.cfg.out_dir / name
            try:
                if dst.is_symlink() or dst.exists():
                    dst.unlink(missing_ok=True)
                dst.symlink_to(src.resolve())
            except (OSError, NotImplementedError):
                # Windows / some NFS mounts don't support symlinks
                shutil.copy2(str(src), str(dst))


# ─────────────────────────────────────────────────────────────────────────────
# Exit code helpers
# ─────────────────────────────────────────────────────────────────────────────

# Standardised exit codes (shared by manifest mode and CLI)
EXIT_PASS        = 0   # all tests passed
EXIT_FAIL        = 1   # ≥1 signature mismatch
EXIT_CRASH       = 2   # build/sim infrastructure failure
EXIT_TOOL        = 3   # required tool (spike, gcc) not found


def compute_exit_code(report: RunReport) -> int:
    """
    Derive the correct exit code from a completed RunReport.

    Priority: tool errors > crash > mismatch > pass.
    """
    s = report.summary
    if s.get("tool_errors"):
        return EXIT_TOOL
    if s["error"] > 0:
        return EXIT_CRASH
    if s["fail"] > 0:
        return EXIT_FAIL
    if s["total"] == 0:
        return EXIT_CRASH   # nothing ran — infrastructure problem
    return EXIT_PASS


# ─────────────────────────────────────────────────────────────────────────────
# Manifest helpers  (mirrors Agent B's deep-merge + atomic pattern)
# ─────────────────────────────────────────────────────────────────────────────

def _deep_merge(base: Dict, overlay: Dict) -> None:
    """In-place recursive merge of overlay into base (same logic as Agent B)."""
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def patch_manifest(manifest_path: Path, updates: Dict[str, Any]) -> None:
    """
    Read manifest_path, deep-merge updates, write back atomically.
    Uses os.replace (POSIX-atomic) exactly like Agent B's patch_manifest().
    """
    with open(manifest_path) as f:
        data = json.load(f)
    _deep_merge(data, updates)
    tmp = manifest_path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, manifest_path)


# ─────────────────────────────────────────────────────────────────────────────
# Manifest-driven entrypoint  (AVA contract mode)
# ─────────────────────────────────────────────────────────────────────────────

def run_compliance_manifest(manifest_path: Path) -> int:
    """
    AVA contract entrypoint.  Called when `--manifest` is passed.

    Reads from manifest
    -------------------
    rundir                    → base output directory
    binary                    → ELF under test (informational; Agent B runs it)
    isa                       → ISA string, default RV32IM
    spikebin                  → spike binary, default "spike"
    workers                   → parallelism, default 4
    compliance.suitepath      → path to riscv-arch-test repo (optional)
    compliance.dutsim         → path to DUT adapter script (optional;
                                 defaults to run_rtl_adapter.py beside this file)

    Writes back to manifest (atomic deep-merge)
    -------------------------------------------
    phases.compliance.status         "running" → "completed" | "error"
    phases.compliance.elapsed_sec
    phases.compliance.timestamp
    outputs.signaturedir             relative path to signature directory
    outputs.compliance_result        relative path to compliance.result.json
    compliance.result                full result object (schema v2.0.0)
    status                           "passed" | "failed"

    Exit codes
    ----------
    0  all pass
    1  ≥1 mismatch
    2  build/sim crash
    3  tool not found
    """
    if not manifest_path.exists():
        log.error("Manifest not found: %s", manifest_path)
        return EXIT_CRASH

    try:
        manifest = json.loads(manifest_path.read_text())
    except Exception as exc:
        log.error("Cannot parse manifest: %s", exc)
        return EXIT_CRASH

    run_dir = Path(manifest.get("rundir", manifest_path.parent)).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)

    # Resolve compliance-specific config from manifest
    compliance_cfg  = manifest.get("compliance", {})
    suite_path      = compliance_cfg.get("suitepath")
    default_adapter = Path(__file__).parent / "run_rtl_adapter.py"
    dut_sim         = compliance_cfg.get("dutsim") or (
        str(default_adapter) if default_adapter.is_file() else None
    )

    cfg = RunConfig(
        isa            = manifest.get("isa", "RV32IM"),
        spike_bin      = manifest.get("spikebin", "spike"),
        dut_sim        = dut_sim,
        arch_test_repo = Path(suite_path) if suite_path else None,
        out_dir        = run_dir / "compliance",
        workers        = manifest.get("workers", 4),
        verbose        = True,
    )

    now_iso = datetime.now(timezone.utc).isoformat()

    # Mark phase as running
    patch_manifest(manifest_path, {
        "phases": {
            "compliance": {
                "status":    "running",
                "timestamp": now_iso,
            },
        },
    })

    t0 = time.monotonic()
    try:
        runner = ComplianceRunner(cfg)
        report = runner.run()
    except Exception as exc:
        log.error("Compliance runner crashed: %s", exc, exc_info=True)
        patch_manifest(manifest_path, {
            "phases": {
                "compliance": {
                    "status":    "error",
                    "error":     str(exc),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            },
            "status": "error",
        })
        return EXIT_CRASH

    elapsed = round(time.monotonic() - t0, 3)
    s       = report.summary

    # Build compliance.result object (schema v2.0.0)
    failed_list = [
        {
            "test":         t["name"],
            "isa_subset":   t["isa_subset"],
            "mismatch_word": t["mismatch_idx"],
            "error_class":  t["error_class"],
            "message":      t["error_msg"],
        }
        for t in report.tests
        if t["result"] in (TestResult.FAIL.value, TestResult.ERROR.value)
    ]

    compliance_result = {
        "schemaversion": "2.0.0",
        "total":         s["total"],
        "pass":          s["pass"],
        "fail":          s["fail"],
        "error":         s["error"],
        "pass_pct":      s["pass_rate_pct"],
        "failedlist":    failed_list,
    }

    # Write compliance.result.json atomically into rundir
    result_json = run_dir / "compliance.result.json"
    _atomic_write(result_json, json.dumps(compliance_result, indent=2))
    report_json = run_dir / "compliance.report.json"
    _atomic_write(report_json, json.dumps(report.to_dict(), indent=2))

    # Signature directory (relative path for portability)
    sig_dir_rel = "compliance/signatures"

    overall_status = "passed" if (s["pass"] == s["total"] and s["total"] > 0) else "failed"

    patch_manifest(manifest_path, {
        "phases": {
            "compliance": {
                "status":      "completed",
                "elapsed_sec": elapsed,
                "timestamp":   datetime.now(timezone.utc).isoformat(),
                "pass":        s["pass"],
                "fail":        s["fail"],
                "error":       s["error"],
                "total":       s["total"],
            },
        },
        "outputs": {
            "signaturedir":      sig_dir_rel,
            "compliance_result": str(result_json.relative_to(run_dir)),
            "compliance_report": str(report_json.relative_to(run_dir)),
        },
        "compliance": {
            "result": compliance_result,
        },
        "status": overall_status,
    })

    log.info(
        "Manifest updated: status=%s  pass=%d/%d  elapsed=%.1fs",
        overall_status, s["pass"], s["total"], elapsed,
    )
    return compute_exit_code(report)


# ─────────────────────────────────────────────────────────────────────────────
# AVA integration hook
# ─────────────────────────────────────────────────────────────────────────────

def run_compliance_for_ava(
    isa:            str           = "RV32IM",
    spike_bin:      str           = "spike",
    dut_sim:        Optional[str] = None,
    arch_test_repo: Optional[str] = None,
    out_dir:        str           = "compliance_results",
    workers:        int           = 4,
    timeout:        int           = 60,
    verbose:        bool          = False,
) -> Dict[str, Any]:
    """
    RISCOF-compatible entry point for the AVA pipeline.

    Returns a dict that maps into VerificationResult.metadata:
    {
        "compliance_pass":     bool,   all tests passed
        "compliance_pass_pct": float,  0.0 – 100.0
        "compliance_total":    int,
        "compliance_pass_cnt": int,
        "compliance_fail_cnt": int,
        "compliance_error_cnt":int,
        "compliance_json":     str,    path to JSON report
        "compliance_html":     str,    path to HTML report
        "compliance_junit":    str,    path to JUnit XML
    }
    """
    cfg = RunConfig(
        isa            = isa,
        spike_bin      = spike_bin,
        dut_sim        = dut_sim,
        arch_test_repo = Path(arch_test_repo) if arch_test_repo else None,
        out_dir        = Path(out_dir),
        workers        = workers,
        timeout_run_s  = timeout,
        verbose        = verbose,
    )
    try:
        runner = ComplianceRunner(cfg)
        report = runner.run()
    except Exception as exc:
        log.error("run_compliance_for_ava failed: %s", exc, exc_info=True)
        return {
            "compliance_pass":      False,
            "compliance_pass_pct":  0.0,
            "compliance_total":     0,
            "compliance_pass_cnt":  0,
            "compliance_fail_cnt":  0,
            "compliance_error_cnt": 1,
            "compliance_json":      "",
            "compliance_html":      "",
            "compliance_junit":     "",
            "compliance_error":     str(exc),
        }

    s = report.summary
    base = Path(out_dir)
    return {
        "compliance_pass":      s["pass"] == s["total"] and s["total"] > 0,
        "compliance_pass_pct":  s["pass_rate_pct"],
        "compliance_total":     s["total"],
        "compliance_pass_cnt":  s["pass"],
        "compliance_fail_cnt":  s["fail"],
        "compliance_error_cnt": s["error"],
        "compliance_json":      str(base / REPORT_JSON),
        "compliance_html":      str(base / REPORT_HTML),
        "compliance_junit":     str(base / REPORT_JUNIT),
    }


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog        = "run_compliance.py",
        description = "Agent E — RISC-V architectural compliance runner",
        formatter_class = argparse.ArgumentDefaultsHelpFormatter,
    )
    # ── AVA contract mode (single-flag entrypoint for orchestrator) ───────────
    p.add_argument(
        "--manifest", default=None, metavar="PATH", type=Path,
        help="Path to AVA run_manifest.json. Activates contract mode: reads "
             "rundir/isa/spikebin from manifest, writes back phases.compliance, "
             "outputs.signaturedir, compliance.result, and status atomically.",
    )

    # ── Standalone mode flags ─────────────────────────────────────────────────
    p.add_argument("--isa",            default="RV32IM",
                   help="ISA string: RV32I or RV32IM")
    p.add_argument("--spike",          default="spike", dest="spike_bin",
                   metavar="SPIKE_BIN",
                   help="Spike ISS binary name or absolute path")
    p.add_argument("--dut-sim",        default=None, metavar="SCRIPT",
                   help="DUT adapter script (run_rtl_adapter.py for Agent B, "
                        "or any script accepting --elf/--sig/--isa). "
                        "Omit to use Spike-as-DUT (self-test / golden==DUT).")
    p.add_argument("--arch-test-repo", default=None, metavar="DIR",
                   help="Path to riscv-arch-test repository checkout")
    p.add_argument("--out-dir",        default="compliance_results", metavar="DIR",
                   help="Root output directory")

    # ── Worker tuning ─────────────────────────────────────────────────────────
    p.add_argument("-j", "--workers",        type=int, default=4,
                   help="Base worker count (build_workers = j, run_workers = j*2)")
    p.add_argument("--build-workers",        type=int, default=0, metavar="N",
                   help="CPU-bound build workers (0 = use --workers)")
    p.add_argument("--run-workers",          type=int, default=0, metavar="N",
                   help="I/O-bound run workers (0 = use --workers * 2)")
    p.add_argument("--max-mismatches",       type=int, default=3, metavar="N",
                   help="Stop signature comparison after N mismatches (0 = unlimited)")

    # ── Timeouts / retry ─────────────────────────────────────────────────────
    p.add_argument("--timeout-build",  type=int, default=120, dest="timeout_build_s",
                   metavar="SEC", help="Per-test build timeout (seconds)")
    p.add_argument("--timeout-run",    type=int, default=60,  dest="timeout_run_s",
                   metavar="SEC", help="Per-test simulator timeout (seconds)")
    p.add_argument("--retry",          type=int, default=2,   dest="retry_max",
                   metavar="N",   help="Max retries for subprocess calls")
    p.add_argument("--no-cache",       action="store_true",
                   help="Disable the SHA-256 build cache")
    p.add_argument("-v", "--verbose",  action="store_true")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_arg_parser()
    args   = parser.parse_args(argv)

    _configure_logging(args.verbose)

    # ── Manifest (AVA contract) mode ─────────────────────────────────────────
    if args.manifest:
        return run_compliance_manifest(Path(args.manifest))

    # ── Standalone mode ───────────────────────────────────────────────────────
    cfg = RunConfig(
        isa             = args.isa,
        spike_bin       = args.spike_bin,
        dut_sim         = args.dut_sim,
        arch_test_repo  = Path(args.arch_test_repo) if args.arch_test_repo else None,
        out_dir         = Path(args.out_dir),
        workers         = args.workers,
        build_workers   = args.build_workers,
        run_workers     = args.run_workers,
        max_mismatches  = args.max_mismatches,
        timeout_build_s = args.timeout_build_s,
        timeout_run_s   = args.timeout_run_s,
        retry_max       = args.retry_max,
        verbose         = args.verbose,
        use_cache       = not args.no_cache,
    )

    runner = ComplianceRunner(cfg)
    report = runner.run()
    s      = report.summary

    # ── Terminal summary ──────────────────────────────────────────────────────
    print("\n" + "─" * 68)
    print(f"  RISC-V Compliance  ISA={report.isa}  spike={report.spike_version}")
    print("─" * 68)
    for t in report.tests:
        icon = {"PASS": "✓", "FAIL": "✗", "ERROR": "!", "SKIPPED": "·"}.get(t["result"], "?")
        cache_tag = " [cached]" if t.get("cache_hit") else ""
        print(f"  {icon}  {t['name']:<26}  {t['isa_subset']:<7}  {t['result']}{cache_tag}")
        if t.get("error_msg"):
            print(f"       └─ {t['error_msg'][:90]}")
    print("─" * 68)
    print(
        f"  PASS={s['pass']}  FAIL={s['fail']}  ERROR={s['error']}  "
        f"total={s['total']}  ({s['pass_rate_pct']:.1f}%)"
    )
    print(f"  HTML  → {s['run_dir']}/{REPORT_HTML}")
    print(f"  JSON  → {s['run_dir']}/{REPORT_JSON}")
    print(f"  JUnit → {s['run_dir']}/{REPORT_JUNIT}")
    print("─" * 68 + "\n")

    return compute_exit_code(report)


if __name__ == "__main__":
    sys.exit(main())

