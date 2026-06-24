"""
AGENT_H/digital_twin.py
========================
T14 — Verification Digital Twin

A lightweight behavioural model of the RISC-V pipeline that can quickly
pre-screen test programs before expensive RTL simulation.  The twin:

  1. Simulates test programs at the instruction level using a Python ISS
     (Instruction Set Simulator) — orders of magnitude faster than Verilator.
  2. Predicts whether a test is likely to trigger a known mismatch class
     based on a learned heuristic profile (from historical bug data).
  3. Assigns each test a "likely_trigger" score [0, 1] so Agent G can
     prioritise high-value tests.
  4. Filters out redundant tests (predicted to be equivalent to already-run
     programs) using a lightweight instruction-histogram fingerprint.

Digital twin vs full RTL
------------------------
The twin is NOT a replacement for RTL simulation.  It is a fast pre-screen:
  - Twin pass + RTL pass → likely correct (confidence increases)
  - Twin pass + RTL fail → novel bug found (most valuable outcome)
  - Twin fail + RTL pass → model inaccuracy (expected sometimes)
  - Twin fail + RTL fail → known class (feeds Agent D comparator)

Usage
-----
  from AGENT_H.digital_twin import DigitalTwin

  twin = DigitalTwin()
  result = twin.simulate(asm_source="path/to/test.S")
  print(result["likely_trigger"], result["fingerprint"])
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "2.1.0"


# ─────────────────────────────────────────────────────────
# Instruction histogram fingerprint
# ─────────────────────────────────────────────────────────

# Mnemonic categories for fingerprinting
_CATEGORIES = {
    "mul_div":   re.compile(r"^(mul|mulh|mulhu|mulhsu|div|divu|rem|remu)\b"),
    "load":      re.compile(r"^(lw|lh|lb|lhu|lbu)\b"),
    "store":     re.compile(r"^(sw|sh|sb)\b"),
    "branch":    re.compile(r"^(beq|bne|blt|bge|bltu|bgeu)\b"),
    "jump":      re.compile(r"^(jal|jalr)\b"),
    "csr":       re.compile(r"^(csrrw|csrrs|csrrc|csrrwi|csrrsi|csrrci)\b"),
    "trap":      re.compile(r"^(ecall|ebreak|mret)\b"),
    "amo":       re.compile(r"^(lr\.w|sc\.w|amo)\b"),
    "fence":     re.compile(r"^(fence)\b"),
    "alu":       re.compile(r"^(add|sub|and|or|xor|sll|srl|sra|slt|sltu|lui|auipc|li|mv|addi|andi|ori|xori|slli|srli|srai)\b"),
}

# Bug-class trigger profiles: {category: expected_fraction_of_instructions}
_TRIGGER_PROFILES: Dict[str, Dict[str, float]] = {
    "REG_MISMATCH": {"mul_div": 0.30, "alu": 0.50},
    "MEM_MISMATCH": {"load": 0.25, "store": 0.25},
    "CSR_MISMATCH": {"csr": 0.30, "trap": 0.10},
    "TRAP_MISMATCH": {"trap": 0.20, "branch": 0.20},
    "PC_MISMATCH":  {"branch": 0.35, "jump": 0.15},
    "ORDERMISMATCH": {"load": 0.20, "store": 0.20, "fence": 0.10, "amo": 0.10},
}


@dataclass
class TwinResult:
    """Result of one digital twin simulation."""
    asm_path:       Optional[str]
    fingerprint:    str            # sha256 prefix of instruction histogram
    histogram:      Dict[str, int] # category → count
    total_instrs:   int
    fractions:      Dict[str, float]
    likely_trigger: Dict[str, float]  # mismatch_class → score
    top_class:      str
    top_score:      float
    is_redundant:   bool
    simulated_regs: Dict[str, int]   # lightweight register file after simulation
    simulated_pc:   int


# ─────────────────────────────────────────────────────────
# Micro ISS (integer-only, no pipeline, no memory model)
# ─────────────────────────────────────────────────────────

class _MicroISS:
    """
    Minimal in-process RISC-V RV32IM integer ISS for fast pre-screening.
    Does NOT model: exceptions, CSRs, memory ordering, caches.
    """

    def __init__(self, max_steps: int = 1000) -> None:
        self.max_steps = max_steps
        self.regs = [0] * 32
        self.pc   = 0x80000000
        self._mem: Dict[int, int] = {}

    def _read_reg(self, r: int) -> int:
        return 0 if r == 0 else self.regs[r]

    def _write_reg(self, r: int, v: int) -> None:
        if r != 0:
            self.regs[r] = v & 0xFFFFFFFF

    def _sign_ext(self, v: int, bits: int) -> int:
        mask = 1 << (bits - 1)
        return (v ^ mask) - mask if v & mask else v

    def _mem_load(self, addr: int, size: int) -> int:
        val = 0
        for i in range(size):
            val |= (self._mem.get(addr + i, 0) & 0xFF) << (i * 8)
        if size < 4:
            val = self._sign_ext(val, size * 8)
        return val & 0xFFFFFFFF

    def _mem_store(self, addr: int, val: int, size: int) -> None:
        for i in range(size):
            self._mem[addr + i] = (val >> (i * 8)) & 0xFF

    def step(self, word: int) -> bool:
        """Execute one instruction word. Returns False if terminal (ecall/ebreak/j .)."""
        op     = word & 0x7F
        rd     = (word >> 7)  & 0x1F
        f3     = (word >> 12) & 0x07
        rs1    = (word >> 15) & 0x1F
        rs2    = (word >> 20) & 0x1F
        f7     = (word >> 25) & 0x7F

        v1 = self._read_reg(rs1)
        v2 = self._read_reg(rs2)
        pc = self.pc
        npc = (pc + 4) & 0xFFFFFFFF

        if op == 0b0110011:   # R-type
            is_m = (f7 == 1)
            if is_m:
                sv1 = self._sign_ext(v1, 32)
                sv2 = self._sign_ext(v2, 32)
                if f3 == 0: res = (sv1 * sv2) & 0xFFFFFFFF
                elif f3 == 1: res = ((sv1 * sv2) >> 32) & 0xFFFFFFFF
                elif f3 == 2: res = ((sv1 * v2) >> 32) & 0xFFFFFFFF
                elif f3 == 3: res = ((v1 * v2) >> 32) & 0xFFFFFFFF
                elif f3 == 4: res = (0xFFFFFFFF if v2 == 0 else (sv1 // sv2) & 0xFFFFFFFF) if sv2 != 0 else 0xFFFFFFFF
                elif f3 == 5: res = (0xFFFFFFFF if v2 == 0 else v1 // v2) & 0xFFFFFFFF
                elif f3 == 6: res = (v1 if v2 == 0 else sv1 % sv2) & 0xFFFFFFFF
                elif f3 == 7: res = (v1 if v2 == 0 else v1 % v2) & 0xFFFFFFFF
                else: res = 0
            else:
                sv1 = self._sign_ext(v1, 32)
                sv2 = self._sign_ext(v2, 32)
                if   f3 == 0 and f7 == 0:    res = (v1 + v2) & 0xFFFFFFFF
                elif f3 == 0 and f7 == 0x20: res = (v1 - v2) & 0xFFFFFFFF
                elif f3 == 4:                res = v1 ^ v2
                elif f3 == 6:                res = v1 | v2
                elif f3 == 7:                res = v1 & v2
                elif f3 == 1:                res = (v1 << (v2 & 31)) & 0xFFFFFFFF
                elif f3 == 5 and f7 == 0:    res = v1 >> (v2 & 31)
                elif f3 == 5 and f7 == 0x20: res = (sv1 >> (v2 & 31)) & 0xFFFFFFFF
                elif f3 == 2:                res = 1 if sv1 < sv2 else 0
                elif f3 == 3:                res = 1 if v1 < v2 else 0
                else: res = 0
            self._write_reg(rd, res)

        elif op == 0b0010011:  # I-type ALU
            imm = self._sign_ext((word >> 20) & 0xFFF, 12)
            sv1 = self._sign_ext(v1, 32)
            if   f3 == 0: res = (v1 + imm) & 0xFFFFFFFF
            elif f3 == 4: res = (v1 ^ imm) & 0xFFFFFFFF
            elif f3 == 6: res = (v1 | imm) & 0xFFFFFFFF
            elif f3 == 7: res = (v1 & imm) & 0xFFFFFFFF
            elif f3 == 1: res = (v1 << (imm & 31)) & 0xFFFFFFFF
            elif f3 == 5 and f7 == 0:    res = v1 >> (imm & 31)
            elif f3 == 5 and f7 == 0x20: res = (sv1 >> (imm & 31)) & 0xFFFFFFFF
            elif f3 == 2: res = 1 if sv1 < imm else 0
            elif f3 == 3: res = 1 if v1 < (imm & 0xFFFFFFFF) else 0
            else: res = 0
            self._write_reg(rd, res)

        elif op == 0b0000011:  # loads
            imm  = self._sign_ext((word >> 20) & 0xFFF, 12)
            addr = (v1 + imm) & 0xFFFFFFFF
            sizes = {0:1, 1:2, 2:4, 4:1, 5:2}
            sz = sizes.get(f3, 4)
            res = self._mem_load(addr, sz)
            if f3 in (4, 5):  res = res & (0xFF if f3 == 4 else 0xFFFF)
            self._write_reg(rd, res)

        elif op == 0b0100011:  # stores
            imm  = self._sign_ext(((word >> 25) << 5) | ((word >> 7) & 0x1F), 12)
            addr = (v1 + imm) & 0xFFFFFFFF
            sizes = {0:1, 1:2, 2:4}
            self._mem_store(addr, v2, sizes.get(f3, 4))

        elif op == 0b1100011:  # branches
            imm = self._sign_ext(
                ((word >> 31) << 12) | (((word >> 7) & 1) << 11) |
                (((word >> 25) & 0x3F) << 5) | (((word >> 8) & 0xF) << 1), 13)
            sv1 = self._sign_ext(v1, 32); sv2 = self._sign_ext(v2, 32)
            taken = {0: v1 == v2, 1: v1 != v2, 4: sv1 < sv2,
                     5: sv1 >= sv2, 6: v1 < v2, 7: v1 >= v2}.get(f3, False)
            if taken:
                npc = (pc + imm) & 0xFFFFFFFF
                if npc == pc:     # j . → terminate
                    self.pc = npc
                    return False

        elif op == 0b1101111:  # jal
            imm = self._sign_ext(
                ((word >> 31) << 20) | (((word >> 12) & 0xFF) << 12) |
                (((word >> 20) & 1) << 11) | (((word >> 21) & 0x3FF) << 1), 21)
            self._write_reg(rd, npc)
            npc = (pc + imm) & 0xFFFFFFFF

        elif op == 0b1100111:  # jalr
            imm = self._sign_ext((word >> 20) & 0xFFF, 12)
            self._write_reg(rd, npc)
            npc = (v1 + imm) & 0xFFFFFFFE

        elif op in (0b0110111, 0b0010111):  # lui / auipc
            imm = (word >> 12) & 0xFFFFF
            self._write_reg(rd, ((imm << 12) + (pc if op == 0b0010111 else 0)) & 0xFFFFFFFF)

        elif op == 0b1110011:  # system
            if f3 == 0:
                return False   # ecall / ebreak / mret → stop

        self.pc = npc
        return True


# ─────────────────────────────────────────────────────────
# Digital twin
# ─────────────────────────────────────────────────────────

class DigitalTwin:
    """
    Pre-screen test programs to predict their likely impact.

    Parameters
    ----------
    seen_fingerprints : set of fingerprints already simulated (for dedup)
    max_steps         : max instructions per simulation
    """

    def __init__(
        self,
        seen_fingerprints: Optional[set] = None,
        max_steps: int = 500,
    ) -> None:
        self._seen  = seen_fingerprints or set()
        self._steps = max_steps

    def _parse_asm(self, source: str) -> List[str]:
        """Extract assembly mnemonic lines (skip directives / labels / comments)."""
        lines = []
        for line in source.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith(".") or line.endswith(":"):
                continue
            # Strip inline comments
            if "#" in line:
                line = line[:line.index("#")].strip()
            if line:
                lines.append(line.lower())
        return lines

    def _histogram(self, mnemonics: List[str]) -> Dict[str, int]:
        hist = {cat: 0 for cat in _CATEGORIES}
        for m in mnemonics:
            for cat, pat in _CATEGORIES.items():
                if pat.match(m):
                    hist[cat] += 1
                    break
        return hist

    def _fingerprint(self, hist: Dict[str, int], total: int) -> str:
        if total == 0:
            return "empty"
        fracs = {k: round(v / total, 2) for k, v in hist.items()}
        blob  = json_dumps_sorted(fracs)
        return hashlib.sha256(blob.encode()).hexdigest()[:12]

    def _likely_trigger_scores(
        self, fracs: Dict[str, float]
    ) -> Dict[str, float]:
        scores = {}
        for bug_class, profile in _TRIGGER_PROFILES.items():
            # Cosine-like similarity between fraction vector and profile
            num = sum(fracs.get(cat, 0) * val for cat, val in profile.items())
            den = (sum(v**2 for v in profile.values()) ** 0.5) * \
                  (sum(v**2 for v in fracs.values()) ** 0.5 or 1)
            scores[bug_class] = round(num / den if den > 0 else 0.0, 4)
        return scores

    def simulate(
        self,
        asm_source: Optional[str | Path] = None,
        asm_text:   Optional[str] = None,
        instr_words: Optional[List[int]] = None,
    ) -> TwinResult:
        """
        Simulate one test program.

        Accepts either:
          - asm_source (path to .S file)
          - asm_text   (raw Assembly string)
          - instr_words (list of 32-bit instruction words)

        Returns TwinResult.
        """
        mnemonics: List[str] = []
        words_used: List[int] = []

        if instr_words:
            words_used = instr_words[:self._steps]
            mnemonics  = [disassemble_rv32im(w).split()[0] for w in words_used]
        else:
            text = asm_text
            if asm_source and text is None:
                text = Path(asm_source).read_text(errors="replace")
            mnemonics = self._parse_asm(text or "")

        hist  = self._histogram(mnemonics)
        total = sum(hist.values())
        fracs = {k: round(v / total, 4) if total > 0 else 0.0 for k, v in hist.items()}
        fp    = self._fingerprint(hist, total)

        is_redundant = fp in self._seen
        self._seen.add(fp)

        trigger_scores = self._likely_trigger_scores(fracs)
        top_class = max(trigger_scores, key=trigger_scores.get) if trigger_scores else "UNKNOWN"
        top_score = trigger_scores.get(top_class, 0.0)

        # Micro-ISS simulation (only if instruction words available)
        sim_regs: Dict[str, int] = {}
        sim_pc = 0
        if words_used:
            iss = _MicroISS(max_steps=self._steps)
            for w in words_used:
                if not iss.step(w):
                    break
            sim_regs = {f"x{i}": iss.regs[i] for i in range(32) if iss.regs[i] != 0}
            sim_pc = iss.pc

        return TwinResult(
            asm_path=str(asm_source) if asm_source else None,
            fingerprint=fp,
            histogram=hist,
            total_instrs=total,
            fractions=fracs,
            likely_trigger=trigger_scores,
            top_class=top_class,
            top_score=top_score,
            is_redundant=is_redundant,
            simulated_regs=sim_regs,
            simulated_pc=sim_pc,
        )

    def batch_screen(
        self,
        test_paths: List[Path],
        min_score:  float = 0.1,
    ) -> List[Dict[str, Any]]:
        """
        Screen a batch of test programs, returning only non-redundant ones
        with trigger score above min_score, sorted by top_score descending.
        """
        results = []
        for p in test_paths:
            try:
                r = self.simulate(asm_source=p)
                if not r.is_redundant and r.top_score >= min_score:
                    results.append({
                        "path":         str(p),
                        "fingerprint":  r.fingerprint,
                        "top_class":    r.top_class,
                        "top_score":    r.top_score,
                        "total_instrs": r.total_instrs,
                        "fractions":    r.fractions,
                    })
            except Exception as exc:
                logger.warning("DigitalTwin: error screening %s: %s", p, exc)
        return sorted(results, key=lambda x: x["top_score"], reverse=True)


def json_dumps_sorted(obj: Any) -> str:
    import json as _json
    return _json.dumps(obj, sort_keys=True)
