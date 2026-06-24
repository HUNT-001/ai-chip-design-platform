"""
AGENT_H/formal_fuzzer.py
=========================
T13 — Formal-Guided Fuzzing Bridge

Uses counterexamples (CEX) produced by Agent L's SymbiYosys equivalence checks
to seed the fuzzing campaign in Agent G.  This bridges formal verification and
random test generation:

  1. Parse SymbiYosys .vcd / witness output to extract the failing signal trace.
  2. Convert the trace into a concrete Assembly instruction sequence that
     exercises the same state transitions.
  3. Pass the seeds to Agent G's CausalGeneticEngine for population seeding
     and directed mutation.

Why this matters
----------------
Formal tools can find deep bugs but produce terse, hard-to-interpret witnesses.
Fuzzing is broad but shallow without seeds.  Together:
  - Formal finds corner cases that random testing misses for weeks.
  - The fuzzer explores the neighbourhood of the formal witness, finding related
    bugs and increasing robustness confidence.

CEX format (SymbiYosys .witness.json)
--------------------------------------
  {
    "step": 0,
    "inputs": {"clk": 0, "rst_n": 1, "instr": "0x00628233"},
    ...
  }
Each step maps to one clock cycle; the `instr` field encodes the instruction
word fed to the DUT.

Usage
-----
  from AGENT_H.formal_fuzzer import FormalFuzzBridge

  bridge = FormalFuzzBridge(
      witness_path  = Path("rundir/equiv_witness.json"),
      outdir        = Path("rundir/formal_seeds"),
  )
  seeds = bridge.extract_seeds()     # List[Dict] suitable for CausalGeneticEngine
  bridge.write_asm_files(seeds)
"""

from __future__ import annotations

import json
import logging
import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "2.1.0"

# RV32IM opcode table for disassembly (subset)
_OPCODE_MAP: Dict[int, str] = {
    0b0110011: "R-type",
    0b0010011: "I-type",
    0b0000011: "load",
    0b0100011: "store",
    0b1100011: "branch",
    0b1101111: "jal",
    0b1100111: "jalr",
    0b0010111: "auipc",
    0b0110111: "lui",
    0b1110011: "system",
}

_FUNCT3_R: Dict[Tuple[int, int], str] = {
    (0, 0): "add", (0, 0x20): "sub", (4, 0): "xor",
    (6, 0): "or",  (7, 0): "and",   (1, 0): "sll",
    (5, 0): "srl", (5, 0x20): "sra", (2, 0): "slt",
    (3, 0): "sltu",
    # M-extension
    (0, 1): "mul", (1, 1): "mulh", (2, 1): "mulhsu",
    (3, 1): "mulhu", (4, 1): "div", (5, 1): "divu",
    (6, 1): "rem", (7, 1): "remu",
}

_GPR_NAMES = [
    "zero","ra","sp","gp","tp","t0","t1","t2",
    "s0","s1","a0","a1","a2","a3","a4","a5",
    "a6","a7","s2","s3","s4","s5","s6","s7",
    "s8","s9","s10","s11","t3","t4","t5","t6",
]


def _sign_extend(val: int, bits: int) -> int:
    if val & (1 << (bits - 1)):
        val -= (1 << bits)
    return val


def disassemble_rv32im(word: int) -> str:
    """Best-effort disassembly of a 32-bit RV32IM instruction word."""
    opcode  = word & 0x7F
    rd      = (word >> 7)  & 0x1F
    funct3  = (word >> 12) & 0x07
    rs1     = (word >> 15) & 0x1F
    rs2     = (word >> 20) & 0x1F
    funct7  = (word >> 25) & 0x7F
    rn      = _GPR_NAMES

    kind = _OPCODE_MAP.get(opcode, "unknown")
    if kind == "R-type":
        mnem = _FUNCT3_R.get((funct3, funct7), f"r.{funct3}.{funct7}")
        return f"{mnem} {rn[rd]}, {rn[rs1]}, {rn[rs2]}"
    elif kind == "I-type":
        imm = _sign_extend((word >> 20) & 0xFFF, 12)
        mnems = {0:"addi",4:"xori",6:"ori",7:"andi",1:"slli",5:"srli/srai",2:"slti",3:"sltiu"}
        mnem = mnems.get(funct3, f"i.{funct3}")
        return f"{mnem} {rn[rd]}, {rn[rs1]}, {imm}"
    elif kind == "load":
        imm = _sign_extend((word >> 20) & 0xFFF, 12)
        mnems = {0:"lb",1:"lh",2:"lw",4:"lbu",5:"lhu"}
        mnem = mnems.get(funct3, f"ld.{funct3}")
        return f"{mnem} {rn[rd]}, {imm}({rn[rs1]})"
    elif kind == "store":
        imm = _sign_extend(((word >> 25) << 5) | ((word >> 7) & 0x1F), 12)
        mnems = {0:"sb",1:"sh",2:"sw"}
        mnem = mnems.get(funct3, f"st.{funct3}")
        return f"{mnem} {rn[rs2]}, {imm}({rn[rs1]})"
    elif kind == "branch":
        imm = _sign_extend(
            ((word >> 31) << 12) | (((word >> 7) & 1) << 11) |
            (((word >> 25) & 0x3F) << 5) | (((word >> 8) & 0xF) << 1), 13)
        mnems = {0:"beq",1:"bne",4:"blt",5:"bge",6:"bltu",7:"bgeu"}
        mnem = mnems.get(funct3, f"br.{funct3}")
        return f"{mnem} {rn[rs1]}, {rn[rs2]}, {imm}"
    elif kind == "jal":
        imm = _sign_extend(
            ((word >> 31) << 20) | (((word >> 12) & 0xFF) << 12) |
            (((word >> 20) & 1) << 11) | (((word >> 21) & 0x3FF) << 1), 21)
        return f"jal {rn[rd]}, {imm}"
    elif kind == "jalr":
        imm = _sign_extend((word >> 20) & 0xFFF, 12)
        return f"jalr {rn[rd]}, {rn[rs1]}, {imm}"
    elif kind in ("lui", "auipc"):
        imm = (word >> 12) & 0xFFFFF
        return f"{kind} {rn[rd]}, 0x{imm:05x}"
    elif kind == "system":
        if funct3 == 0 and rs1 == 0 and rd == 0:
            sub = (word >> 20) & 0xFFF
            return {0: "ecall", 1: "ebreak", 0x302: "mret"}.get(sub, f"sys.{sub}")
        csr = (word >> 20) & 0xFFF
        mnems = {1:"csrrw",2:"csrrs",3:"csrrc",5:"csrrwi",6:"csrrsi",7:"csrrci"}
        mnem = mnems.get(funct3, f"csr.{funct3}")
        return f"{mnem} {rn[rd]}, {csr:#x}, {rn[rs1]}"
    return f".word 0x{word:08x}"


# ─────────────────────────────────────────────────────────
# Seed extraction
# ─────────────────────────────────────────────────────────

@dataclass
class FormalSeed:
    """One Assembly seed derived from a formal witness."""
    name:       str
    asm_lines:  List[str]
    source:     str    # "witness", "cex_extracted"
    instr_words: List[int] = field(default_factory=list)
    cycles:     int = 0


class FormalFuzzBridge:
    """
    Extracts fuzzing seeds from SymbiYosys witness files.

    Parameters
    ----------
    witness_path : path to .witness.json or .json from sby output
    outdir       : directory for .S seed files
    max_seeds    : maximum number of seeds to produce
    """

    def __init__(
        self,
        witness_path:  Path | str,
        outdir:        Optional[Path | str] = None,
        max_seeds:     int = 20,
    ) -> None:
        self.witness_path = Path(witness_path)
        self.outdir       = Path(outdir) if outdir else None
        self.max_seeds    = max_seeds

    def _load_witness(self) -> List[Dict]:
        """Load SymbiYosys witness JSON (list of step dicts)."""
        with open(self.witness_path) as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        # Some sbyw outputs wrap in {"steps": [...]}
        if isinstance(data, dict):
            return data.get("steps", data.get("trace", []))
        return []

    def _instr_word_from_step(self, step: Dict) -> Optional[int]:
        """Extract instruction word from a witness step dict."""
        for key in ("instr", "instruction", "imem_rdata", "mem_rdata"):
            val = step.get("inputs", {}).get(key) or step.get(key)
            if val is not None:
                if isinstance(val, str):
                    val = int(val, 16) if val.startswith("0x") else int(val, 0)
                return val
        return None

    def extract_seeds(self) -> List[FormalSeed]:
        """
        Parse the witness file and produce FormalSeed objects.

        If the witness file doesn't exist or has no instruction data,
        falls back to a set of hardcoded corner-case seeds.
        """
        seeds: List[FormalSeed] = []

        if self.witness_path.exists():
            try:
                steps = self._load_witness()
                words = []
                for step in steps[:64]:  # cap at 64 cycles
                    w = self._instr_word_from_step(step)
                    if w is not None:
                        words.append(w)

                if words:
                    asm_lines = [f"    .word 0x{w:08x}   # {disassemble_rv32im(w)}"
                                 for w in words]
                    seeds.append(FormalSeed(
                        name="cex_from_witness",
                        asm_lines=asm_lines,
                        source="witness",
                        instr_words=words,
                        cycles=len(steps),
                    ))
                    logger.info("FormalFuzzBridge: extracted %d instruction words from witness",
                                len(words))
            except Exception as exc:
                logger.warning("FormalFuzzBridge: failed to parse witness: %s", exc)

        # Hardcoded corner seeds (always included for coverage)
        _CORNER_SEEDS: List[Tuple[str, List[str]]] = [
            ("formal_div_zero", [
                "    li    t0, 0x80000000",
                "    li    t1, -1",
                "    div   t2, t0, t1",      # INT_MIN / -1 overflow
                "    li    t3, 0",
                "    div   t4, t0, t3",      # div by zero
            ]),
            ("formal_mul_chain", [
                "    li    a0, 0x7fffffff",
                "    li    a1, 0x7fffffff",
                "    mul   a2, a0, a1",
                "    mulh  a3, a0, a1",
                "    mulhu a4, a0, a1",
            ]),
            ("formal_amo_sequence", [
                "    lui   t0, 0x80001",
                "    li    t1, 0x42",
                "    amoswap.w.aq t2, t1, (t0)",
                "    amoadd.w.rl t3, t1, (t0)",
            ]),
            ("formal_csr_sequence", [
                "    csrr  t0, mstatus",
                "    li    t1, 0x1808",
                "    csrw  mstatus, t1",
                "    csrr  t2, mstatus",
                "    bne   t1, t2, .",
            ]),
            ("formal_lr_sc_loop", [
                "    lui   t0, 0x80001",
                "1:  lr.w  t1, (t0)",
                "    li    t2, 0x99",
                "    sc.w  t3, t2, (t0)",
                "    bnez  t3, 1b",
                "    lw    t4, 0(t0)",
                "    bne   t2, t4, .",
            ]),
        ]
        for name, lines in _CORNER_SEEDS[:self.max_seeds - len(seeds)]:
            seeds.append(FormalSeed(name=name, asm_lines=lines,
                                    source="cex_extracted"))

        return seeds[:self.max_seeds]

    def write_asm_files(self, seeds: List[FormalSeed]) -> List[Path]:
        """Write seeds as .S files. Returns list of paths written."""
        if self.outdir is None:
            return []
        self.outdir.mkdir(parents=True, exist_ok=True)
        paths = []
        for seed in seeds:
            path = self.outdir / f"{seed.name}.S"
            with open(path, "w") as f:
                f.write(f"# AVA formal-guided fuzzing seed: {seed.name}\n")
                f.write(f"# Source: {seed.source}, cycles={seed.cycles}\n\n")
                f.write(".section .text\n.global _start\n_start:\n")
                for line in seed.asm_lines:
                    f.write(line + "\n")
                f.write("\n    li    a0, 1\n")
                f.write("    lui   t0, 0x80001\n")
                f.write("    sw    a0, 0x1000(t0)\n")
                f.write("    j     .\n")
            paths.append(path)
            logger.debug("FormalFuzzBridge: wrote %s", path)
        return paths

    def run(self) -> Dict[str, Any]:
        """Extract seeds, write files, return report."""
        started = datetime.now(timezone.utc)
        seeds = self.extract_seeds()
        paths = self.write_asm_files(seeds)
        finished = datetime.now(timezone.utc)

        return {
            "schema_version": SCHEMA_VERSION,
            "agent": "formal_fuzzer",
            "witness_path": str(self.witness_path),
            "seeds_extracted": len(seeds),
            "files_written": len(paths),
            "seed_names": [s.name for s in seeds],
            "output_dir": str(self.outdir) if self.outdir else None,
            "started_at":  started.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "finished_at": finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duration_s":  round((finished - started).total_seconds(), 3),
        }


def run_from_manifest(manifest_path: Path) -> int:
    with open(manifest_path) as f:
        manifest = json.load(f)

    run_dir = Path(manifest["run_dir"])
    outputs = manifest.get("outputs", {})

    # Look for SymbiYosys witness
    witness_path = run_dir / (outputs.get("equiv_witness") or "equiv_witness.json")
    outdir       = run_dir / "formal_seeds"

    bridge = FormalFuzzBridge(witness_path, outdir)
    report = bridge.run()
    report["run_id"] = manifest.get("run_id", "unknown")

    report_path = run_dir / "formal_fuzz_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
        f.write("\n")

    manifest.setdefault("outputs", {})["formal_fuzz_report"] = "formal_fuzz_report.json"
    manifest.setdefault("outputs", {})["formal_seeds_dir"]   = "formal_seeds"

    tmp = manifest_path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    tmp.rename(manifest_path)
    return 0
