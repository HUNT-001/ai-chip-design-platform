"""
rv32im_testgen.asm_builder
===========================
Assembles test cases into standard bare-metal RV32IM ELFs.

Harness overview
----------------
Every test (directed or random) uses the same bare-metal skeleton:

    ┌─────────────────────────────────────────────────────────┐
    │  _trap_vec   ← CSR mtvec points here                    │
    │               writes 0xDEAD exit code to tohost         │
    ├─────────────────────────────────────────────────────────┤
    │  _start      ← ELF entry point at 0x80000000            │
    │               sets mtvec, sp, t6 (safe-region base)     │
    │               zeroes all working registers               │
    ├─────────────────────────────────────────────────────────┤
    │  <test body> ← directed or random instructions          │
    ├─────────────────────────────────────────────────────────┤
    │  self-checks ← directed: bne reg, expected, _fail       │
    ├─────────────────────────────────────────────────────────┤
    │  _exit       ← writes (exit_code<<1)|1 to tohost        │
    │  _halt       ← infinite loop                            │
    └─────────────────────────────────────────────────────────┘

HTIF convention (compatible with Spike, QEMU, Verilator tohost polling):
    tohost = (exit_code << 1) | 1
    exit_code 0 = PASS, non-zero = FAIL / trap

Evolution metadata
------------------
``write_test()`` accepts optional *evolution_meta* (fitness, targets,
generation) which is merged into the per-test ``.meta.json`` file so Agent B
(RTL runner) and Agent F (coverage) can track which evolved tests hit which
cold paths.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .directed_tests import DirectedTest
from .random_gen import GeneratorConfig, InstructionMix, RV32IMRandomGenerator

logger = logging.getLogger(__name__)


# ─── Toolchain detection ──────────────────────────────────────────────────────

def _detect_toolchain() -> Optional[str]:
    """
    Search for a RISC-V GCC cross-compiler.

    Preference: riscv32-unknown-elf → riscv64-unknown-elf →
                riscv32-linux-gnu   → riscv64-linux-gnu
    """
    candidates = (
        "riscv32-unknown-elf-",
        "riscv64-unknown-elf-",
        "riscv32-linux-gnu-",
        "riscv64-linux-gnu-",
    )
    for prefix in candidates:
        if shutil.which(f"{prefix}gcc"):
            logger.info("RISC-V toolchain detected: %sgcc", prefix)
            return prefix
    logger.warning(
        "No RISC-V GCC toolchain found.  ELF files will NOT be produced.\n"
        "  Install one of: riscv32-unknown-elf-gcc, riscv64-unknown-elf-gcc\n"
        "  or set PATH to include the toolchain bin directory."
    )
    return None


TOOLCHAIN_PREFIX: Optional[str] = _detect_toolchain()


# ─── Linker script ────────────────────────────────────────────────────────────

_LINKER_SCRIPT: str = """\
/* rv32im_testgen bare-metal linker script
   Entry at 0x80000000 (Spike/QEMU M-mode default load address)        */
OUTPUT_ARCH(riscv)
ENTRY(_start)

SECTIONS {
    . = 0x80000000;

    .text : {
        KEEP(*(.text.init))
        *(.text*)
    }

    . = ALIGN(8);
    .data : {
        *(.data*)
        *(.rodata*)
    }

    . = ALIGN(64);
    .htif : {
        *(.htif)
    }

    . = ALIGN(8);
    .bss (NOLOAD) : {
        *(.bss*)
        *(COMMON)
    }

    . = ALIGN(16);
    _stack_base = .;
    . += 0x2000;
    _stack_top  = .;

    _end = .;
}
"""

# ─── Assembly harness templates ───────────────────────────────────────────────

_PROLOGUE: str = """\
    .section .text.init, "ax"
    .global  _start
    .align   2

/*─────────────────────────────────────────────────────────────────────────────
 *  Trap vector: any unexpected exception → write 0xDEAD exit code to tohost
 *──────────────────────────────────────────────────────────────────────────── */
.align 2
_trap_vec:
    la      t0, tohost
    li      a0, 0xDEAD
    slli    a0, a0, 1           /* HTIF: (exit_code << 1) | 1              */
    ori     a0, a0, 1
    sw      a0, 0(t0)           /* write lower 32 bits of tohost           */
_halt:
    j       _halt               /* spin; simulator polls tohost             */

/*─────────────────────────────────────────────────────────────────────────────
 *  Entry point
 *──────────────────────────────────────────────────────────────────────────── */
_start:
    la      t0, _trap_vec
    csrw    mtvec, t0

    la      sp, _stack_top

    la      t6, _mem_region

    li      t0,  0
    li      t1,  0
    li      t2,  0
    li      t3,  0
    li      t4,  0
    li      t5,  0
    li      a0,  0
    li      a1,  0
    li      a2,  0
    li      a3,  0
    li      a4,  0
    li      a5,  0
    li      a6,  0
    li      a7,  0

"""

_EPILOGUE: str = """\
/*─────────────────────────────────────────────────────────────────────────────
 *  Exit paths
 *──────────────────────────────────────────────────────────────────────────── */
    li      a0, 0
    j       _exit

_fail:
    li      a0, 1

_exit:
    la      t0, tohost
    slli    a0, a0, 1
    ori     a0, a0, 1
    sw      a0, 0(t0)
    sw      zero, 4(t0)
    j       _halt

/*─────────────────────────────────────────────────────────────────────────────
 *  HTIF tohost / fromhost (64-byte aligned per HTIF spec)
 *──────────────────────────────────────────────────────────────────────────── */
    .section .htif, "aw"
    .global  tohost
    .global  fromhost
    .align   6
tohost:   .dword 0
fromhost: .dword 0

/*─────────────────────────────────────────────────────────────────────────────
 *  Safe load/store scratch region (256 bytes, 8-byte aligned)
 *──────────────────────────────────────────────────────────────────────────── */
    .section .bss
    .global  _mem_region
    .align   3
_mem_region:
    .space   256
"""


# ─── Self-check assertion generator ──────────────────────────────────────────

def _make_self_checks(expected_regs: Dict[str, int]) -> List[str]:
    """
    Generate assembly self-check assertions for the given register→value map.

    For each ``(reg, expected)`` pair emits::

        li      a4, <expected>
        bne     <reg>, a4, _fail

    x0 is silently skipped (always 0; unobservable via register compare).
    """
    if not expected_regs:
        return []

    lines: List[str] = [
        "/*─────────────────────────────────────────────────────────────────",
        " *  Self-check assertions — generated by rv32im_testgen            ",
        " *─────────────────────────────────────────────────────────────────*/",
    ]
    for reg, expected in sorted(expected_regs.items()):
        if reg in ("x0", "zero"):
            lines.append("    /* skip x0 check: hardwired to 0 by ISA */")
            continue
        lines.append(f"    /* check {reg} == 0x{expected:08X} */")
        lines.append(f"    li      a4, {expected}")
        lines.append(f"    bne     {reg}, a4, _fail")

    return lines


# ─── Directed test assembler ──────────────────────────────────────────────────

def build_directed_asm(test: DirectedTest) -> str:
    """Render a :class:`DirectedTest` to a complete ``.S`` source string."""
    sections: List[str] = [_PROLOGUE]
    sections.append(
        f"/*─────────────────────────────────────────────────────────────────\n"
        f" *  DIRECTED TEST: {test.name}\n"
        f" *  {test.description}\n"
        f" *  Category : {test.category}\n"
        f" *  Spec ref : {test.spec_ref or 'N/A'}\n"
        f" *  Fitness  : {test.fitness:.2f}\n"
        f" *  Targets  : {', '.join(test.targets) if test.targets else 'baseline'}\n"
        f" *─────────────────────────────────────────────────────────────────*/\n"
    )

    for raw_line in test.asm_body:
        stripped = raw_line.strip()
        if not stripped:
            sections.append("")
        elif stripped.endswith(":") or stripped.startswith("."):
            sections.append(stripped)
        elif stripped.startswith(("/*", "//", "#")):
            sections.append(stripped)
        else:
            sections.append(f"    {stripped}")

    sections.append("")
    checks = _make_self_checks(test.expected_regs)
    sections.extend(checks)
    if checks:
        sections.append("")
    sections.append(_EPILOGUE)

    return "\n".join(sections)


# ─── Random test assembler ────────────────────────────────────────────────────

def build_random_asm(cfg: GeneratorConfig) -> Tuple[str, InstructionMix]:
    """
    Generate a random instruction stream and render it to a ``.S`` source string.

    Returns
    -------
    asm_src
        Complete ``.S`` source file content.
    mix
        :class:`InstructionMix` with per-group instruction counts.
    """
    gen = RV32IMRandomGenerator(cfg)
    body_lines, mix = gen.generate()

    sections: List[str] = [
        _PROLOGUE,
        (
            f"/*─────────────────────────────────────────────────────────────────\n"
            f" *  RANDOM TEST   seed=0x{cfg.seed:08X}   length={cfg.length}\n"
            f" *  M-ext weight : {cfg.weights.get('alu_m', 0):.0%}\n"
            f" *  Trap rate    : {cfg.trap_injection_rate:.1%}\n"
            f" *─────────────────────────────────────────────────────────────────*/\n"
        ),
    ]
    sections.extend(body_lines)
    sections.append("")
    sections.append(_EPILOGUE)

    return "\n".join(sections), mix


# ─── Evolved test assembler ───────────────────────────────────────────────────

def build_evolved_asm(
    name: str,
    body_lines: List[str],
    generation: int,
    targets: List[str],
    fitness: float,
) -> str:
    """
    Render an evolved instruction sequence produced by :class:`GeneticEngine`
    to a complete ``.S`` source string.

    Parameters
    ----------
    name
        Test identifier (used in comment header).
    body_lines
        Raw assembly lines from the genetic engine (already indented).
    generation
        Evolutionary generation number (0 = seed population).
    targets
        Module/opcode names this test was evolved to hit.
    fitness
        Fitness score assigned by the GeneticEngine.
    """
    sections: List[str] = [
        _PROLOGUE,
        (
            f"/*─────────────────────────────────────────────────────────────────\n"
            f" *  EVOLVED TEST  : {name}\n"
            f" *  Generation    : {generation}\n"
            f" *  Fitness score : {fitness:.3f}\n"
            f" *  Targets       : {', '.join(targets) if targets else 'none'}\n"
            f" *─────────────────────────────────────────────────────────────────*/\n"
        ),
    ]
    sections.extend(body_lines)
    sections.append("")
    sections.append(_EPILOGUE)

    return "\n".join(sections)


# ─── File and ELF writing ─────────────────────────────────────────────────────

def write_test(
    name: str,
    asm_src: str,
    metadata: Dict[str, Any],
    outdir: Path,
    assemble: bool = True,
    evolution_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Optional[str]]:
    """
    Write a test to disk and optionally assemble it into an ELF.

    Files produced (all in *outdir*):
    * ``<name>.S``         — GNU assembler source (always produced)
    * ``<name>.meta.json`` — JSON metadata record (always produced)
    * ``<name>.elf``       — ELF binary (only if toolchain found + assemble=True)

    Parameters
    ----------
    name
        Base filename (no extension).
    asm_src
        Complete ``.S`` source content.
    metadata
        Base metadata dict (JSON-serialisable).
    outdir
        Output directory (created if absent).
    assemble
        If ``False``, skip ELF generation.
    evolution_meta
        Optional dict with genetic engine metadata to merge into the saved
        ``.meta.json``.  Accepted keys:

        * ``"fitness"``           — float fitness score
        * ``"targets"``           — List[str] cold-path targets
        * ``"evolved_generation"`` — int generation number
        * ``"evolved_from"``      — List[str] parent test names
        * ``"crossover_point"``   — int crossover index (if applicable)

    Returns
    -------
    dict with keys ``"S"``, ``"meta"``, ``"elf"`` mapping to absolute path
    strings (or ``None`` for ``"elf"`` if not produced).
    """
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    s_path    = outdir / f"{name}.S"
    meta_path = outdir / f"{name}.meta.json"
    elf_path  = outdir / f"{name}.elf"
    ld_path   = outdir / "link.ld"

    if not ld_path.exists():
        ld_path.write_text(_LINKER_SCRIPT, encoding="utf-8")

    s_path.write_text(asm_src, encoding="utf-8")

    # Merge evolution metadata into the base metadata record
    full_meta: Dict[str, Any] = dict(metadata)
    if evolution_meta:
        full_meta["evolution"] = {
            "fitness":            evolution_meta.get("fitness", 0.0),
            "targets":            evolution_meta.get("targets", []),
            "evolved_generation": evolution_meta.get("evolved_generation", 0),
            "evolved_from":       evolution_meta.get("evolved_from", []),
            "crossover_point":    evolution_meta.get("crossover_point", None),
        }

    meta_path.write_text(
        json.dumps(full_meta, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )

    artifacts: Dict[str, Optional[str]] = {
        "S":    str(s_path),
        "meta": str(meta_path),
        "elf":  None,
    }

    if assemble:
        artifacts["elf"] = _assemble_elf(s_path, elf_path, ld_path, name)

    return artifacts


# ─── ELF assembly internals ───────────────────────────────────────────────────

def _assemble_elf(
    s_path:   Path,
    elf_path: Path,
    ld_path:  Path,
    test_name: str,
) -> Optional[str]:
    """Invoke RISC-V GCC to assemble *s_path* into an ELF at *elf_path*."""
    if TOOLCHAIN_PREFIX is None:
        return None

    cmd = [
        f"{TOOLCHAIN_PREFIX}gcc",
        "-march=rv32im",
        "-mabi=ilp32",
        "-mno-relax",
        "-nostdlib",
        "-static",
        "-Wl,--no-warn-rwx-segments",
        f"-Wl,-T{ld_path}",
        "-o", str(elf_path),
        str(s_path),
    ]

    try:
        result = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            logger.warning(
                "GCC non-zero exit for %s (rc=%d):\n%s",
                test_name, result.returncode, result.stderr.strip(),
            )
            return None
        if not elf_path.exists() or elf_path.stat().st_size < 16:
            logger.warning("ELF for %s missing or suspiciously small", test_name)
            return None
        logger.debug(
            "ELF assembled: %s (%d bytes)", elf_path, elf_path.stat().st_size
        )
        return str(elf_path)

    except subprocess.CalledProcessError as exc:
        logger.warning(
            "Assembly failed for %s:\n  CMD: %s\n  ERR: %s",
            test_name,
            " ".join(cmd),
            exc.stderr.strip() if exc.stderr else "(no stderr)",
        )
    except subprocess.TimeoutExpired:
        logger.warning("Assembly timeout (60 s) for %s", test_name)
        elf_path.unlink(missing_ok=True)
    except FileNotFoundError as exc:
        logger.error(
            "Toolchain binary not found (%s). TOOLCHAIN_PREFIX=%r",
            exc, TOOLCHAIN_PREFIX,
        )

    return None


# ─── Optional Spike runner ────────────────────────────────────────────────────

def run_spike(
    elf_path: Path,
    timeout: float = 10.0,
) -> Optional[int]:
    """
    Run *elf_path* on Spike (if installed) and return the process exit code.

    Returns ``None`` if Spike is not installed or execution times out.
    Exit code 0 = PASS (test wrote tohost=1); non-zero = FAIL.
    """
    if not shutil.which("spike"):
        return None

    cmd = ["spike", "--isa=rv32im", str(elf_path)]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode
    except subprocess.TimeoutExpired:
        logger.warning("Spike timeout for %s", elf_path)
        return None
    except Exception as exc:
        logger.warning("Spike run error for %s: %s", elf_path, exc)
        return None
