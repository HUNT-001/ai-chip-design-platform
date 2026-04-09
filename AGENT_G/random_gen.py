"""
rv32im_testgen.random_gen
==========================
Seedable, constrained, coverage-aware RV32IM random instruction stream generator.

Design guarantees
-----------------
* **Full reproducibility** — identical ``GeneratorConfig`` always yields
  byte-identical output.
* **Memory safety** — all loads/stores confined to ``_mem_region`` via t6.
* **Legal encodings only** — shamt ∈ [0,31], imm12 ∈ [-2048,2047],
  offsets alignment-correct for chosen width.
* **x0 protection** — x0 is never a destination register.
* **Label uniqueness** — branch labels use a per-instance monotonic counter.
* **Constraint-biased generation** — ``generate_biased()`` steers instruction
  selection toward Agent F cold-path modules and Agent D hypotheses.
* **Python 3.8+ compatible**.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ─── ISA register pools ───────────────────────────────────────────────────────

_DEST_REGS: Tuple[str, ...] = (
    "t0", "t1", "t2", "t3", "t4", "t5",
    "a0", "a1", "a2", "a3",
)
_SRC_REGS: Tuple[str, ...] = tuple(f"x{i}" for i in range(1, 32))

# ─── Opcode tables ────────────────────────────────────────────────────────────

_R_TYPE_INT: Tuple[str, ...] = (
    "add", "sub", "sll", "slt", "sltu", "xor", "srl", "sra", "or", "and",
)
_R_TYPE_M: Tuple[str, ...] = (
    "mul", "mulh", "mulhsu", "mulhu",
    "div", "divu", "rem", "remu",
)
_I_TYPE_ALU: Tuple[str, ...] = (
    "addi", "slti", "sltiu", "xori", "ori", "andi",
)
_I_TYPE_SHIFT: Tuple[str, ...] = ("slli", "srli", "srai")
_LOAD_OPS:   Tuple[str, ...] = ("lw", "lh", "lhu", "lb", "lbu")
_STORE_OPS:  Tuple[str, ...] = ("sw", "sh", "sb")
_BRANCH_OPS: Tuple[str, ...] = ("beq", "bne", "blt", "bge", "bltu", "bgeu")

_OP_ALIGN: Dict[str, int] = {
    "lw": 4, "sw": 4,
    "lh": 2, "lhu": 2, "sh": 2,
    "lb": 1, "lbu": 1, "sb": 1,
}

# ─── Module → opcode mapping (used by constraint-biased generation) ───────────
# Keys are lowercase module/keyword tokens from Agent F cold-path reports and
# Agent D hypothesis strings.  Values are the opcodes to stress when that
# module is flagged as cold.

_MODULE_OPS: Dict[str, List[str]] = {
    "mul":    ["mul", "mulh", "mulhsu", "mulhu"],
    "mulh":   ["mulh", "mulhsu", "mulhu"],
    "mulhsu": ["mulhsu"],
    "mulhu":  ["mulhu"],
    "div":    ["div", "divu"],
    "divu":   ["divu"],
    "rem":    ["rem", "remu"],
    "remu":   ["remu"],
    "load":   list(_LOAD_OPS),
    "store":  list(_STORE_OPS),
    "branch": list(_BRANCH_OPS),
    "alu":    list(_R_TYPE_INT),
    "shift":  list(_I_TYPE_SHIFT),
    "overflow":   ["mul", "mulh", "div", "rem"],
    "zero":       ["div", "divu", "rem", "remu"],
    "sign":       ["mul", "mulh", "mulhsu", "div", "rem"],
    "boundary":   ["mulhu", "divu"],
}

# ─── Default weight distributions ─────────────────────────────────────────────

DEFAULT_WEIGHTS: Dict[str, float] = {
    "alu_r":     0.20,
    "alu_m":     0.30,
    "alu_i":     0.12,
    "shift_i":   0.05,
    "load":      0.08,
    "store":     0.08,
    "branch":    0.10,
    "lui_auipc": 0.04,
    "nop":       0.03,
}

_M_SUB_WEIGHTS: Dict[str, float] = {
    "mul":    0.16,
    "mulh":   0.11,
    "mulhsu": 0.09,
    "mulhu":  0.09,
    "div":    0.20,
    "divu":   0.14,
    "rem":    0.12,
    "remu":   0.09,
}


# ─── Dataclasses ─────────────────────────────────────────────────────────────

@dataclass
class GeneratorConfig:
    """
    Parameters for :class:`RV32IMRandomGenerator`.

    Attributes
    ----------
    seed
        Integer seed for :class:`random.Random`.  Same seed → same output.
    length
        Number of instruction *groups* to emit.
    mem_size
        Size in bytes of the safe load/store scratch region (≥ 8).
    trap_injection_rate
        Fraction of instruction slots replaced by trap-inducing instructions.
        0.0 = no traps.
    weights
        Per-group instruction mix weights (normalised internally).
    reserved_dest_regs
        Register names that must not appear as instruction destinations.
    """

    seed: int = 0
    length: int = 200
    mem_size: int = 256
    trap_injection_rate: float = 0.0
    weights: Dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_WEIGHTS)
    )
    reserved_dest_regs: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.mem_size < 8:
            raise ValueError(f"mem_size must be ≥ 8, got {self.mem_size}")
        if not (0.0 <= self.trap_injection_rate <= 1.0):
            raise ValueError(
                f"trap_injection_rate must be in [0, 1], got {self.trap_injection_rate}"
            )
        if self.length < 1:
            raise ValueError(f"length must be ≥ 1, got {self.length}")
        merged = dict(DEFAULT_WEIGHTS)
        merged.update(self.weights)
        self.weights = merged


@dataclass
class InstructionMix:
    """Per-group instruction counts produced by one generate() call."""

    alu_r:     int = 0
    alu_m:     int = 0
    alu_i:     int = 0
    shift_i:   int = 0
    load:      int = 0
    store:     int = 0
    branch:    int = 0
    lui_auipc: int = 0
    nop:       int = 0
    trap:      int = 0
    total:     int = 0

    def to_dict(self) -> Dict[str, object]:
        """Return counts and percentages as a plain dict (JSON-serialisable)."""
        denom = max(self.total, 1)
        return {
            "alu_r":     self.alu_r,
            "alu_m":     self.alu_m,
            "alu_i":     self.alu_i,
            "shift_i":   self.shift_i,
            "load":      self.load,
            "store":     self.store,
            "branch":    self.branch,
            "lui_auipc": self.lui_auipc,
            "nop":       self.nop,
            "trap":      self.trap,
            "total":     self.total,
            "alu_m_pct": round(100.0 * self.alu_m / denom, 1),
        }

    def _increment(self, group: str) -> None:
        current = getattr(self, group, None)
        if current is None:
            raise ValueError(f"Unknown instruction group: {group!r}")
        setattr(self, group, current + 1)
        self.total += 1


# ─── Generator ───────────────────────────────────────────────────────────────

class RV32IMRandomGenerator:
    """
    Seedable random RV32IM instruction stream generator.

    Thread safety: each instance holds its own :class:`random.Random` and
    label counter; do **not** share across threads.

    Standard usage::

        cfg = GeneratorConfig(seed=42, length=200)
        gen = RV32IMRandomGenerator(cfg)
        lines, mix = gen.generate()

    Constraint-biased usage (AVA feedback loop)::

        constraints = [
            {"module": "div", "reachability": 0.05},   # Agent F
            {"module": "mul", "hypothesis": "overflow corner case"},  # Agent D
        ]
        results = gen.generate_biased(population=80, constraints=constraints)
        # results: List[Dict] each with keys "sequence", "targets", "bias_ratio"
    """

    def __init__(self, config: GeneratorConfig) -> None:
        import random as _random_module
        self.config = config
        self._rng = _random_module.Random(config.seed)
        self._label_counter: int = 0
        self._dest_pool: List[str] = [
            r for r in _DEST_REGS
            if r not in config.reserved_dest_regs
        ]
        if not self._dest_pool:
            raise ValueError(
                "All destination registers are reserved; cannot generate instructions"
            )
        self._normalised_weights, self._weight_keys = (
            self._normalise_weights(config.weights)
        )
        self._m_ops: List[str] = list(_M_SUB_WEIGHTS.keys())
        self._m_wts: List[float] = self._normalise_list(
            list(_M_SUB_WEIGHTS.values())
        )
        logger.debug(
            "RV32IMRandomGenerator: seed=0x%08X length=%d",
            config.seed, config.length,
        )

    # ── Standard generation ───────────────────────────────────────────────────

    def generate(self) -> Tuple[List[str], InstructionMix]:
        """
        Generate a complete instruction stream.

        Returns
        -------
        lines
            Flat list of assembly source lines with consistent 4-space
            indentation.  Branch scaffolding may add label lines (col 0).
        mix
            :class:`InstructionMix` with per-group instruction counts.
        """
        cfg = self.config
        mix = InstructionMix()
        out_lines: List[str] = []

        for _ in range(cfg.length):
            if (
                cfg.trap_injection_rate > 0.0
                and self._rng.random() < cfg.trap_injection_rate
            ):
                out_lines.extend(self._emit_trap())
                mix._increment("trap")
                continue

            group = self._pick_group()
            out_lines.extend(self._emit_group(group))
            mix._increment(group)

        return out_lines, mix

    # ── Constraint-biased generation (AVA feedback loop) ─────────────────────

    def generate_biased(
        self,
        population: int,
        constraints: List[Dict],
        bias_ratio: float = 0.70,
    ) -> List[Dict]:
        """
        Generate *population* instruction sequences biased toward *constraints*.

        This is the core AVA feedback integration point.  Each sequence is a
        dict suitable for GeneticEngine consumption.

        Parameters
        ----------
        population
            Number of instruction sequences to produce.
        constraints
            Unified list of Agent F cold-path dicts and Agent D hypothesis
            dicts.  Each must have at minimum a ``"module"`` key.
        bias_ratio
            Fraction of sequences in which constraint-targeted opcodes are
            stressed.  Default 0.70 (70 % biased, 30 % fully random).

        Returns
        -------
        List[Dict]
            Each dict contains:
            * ``"sequence"`` — List[str] of assembly lines
            * ``"targets"``  — List[str] of targeted module names
            * ``"bias_ratio"`` — float (records actual bias ratio for metadata)
            * ``"seed"``     — int (per-sequence deterministic seed)
        """
        if population < 1:
            raise ValueError(f"population must be ≥ 1, got {population}")
        if not (0.0 <= bias_ratio <= 1.0):
            raise ValueError(f"bias_ratio must be in [0.0, 1.0], got {bias_ratio}")

        # Extract module names from constraints; skip empty/None
        modules: List[str] = [
            str(c.get("module", "")).lower()
            for c in constraints
            if c.get("module")
        ]
        # Deduplicate while preserving order (cold paths may repeat)
        seen: Dict[str, None] = {}
        unique_modules: List[str] = []
        for m in modules:
            if m not in seen:
                seen[m] = None
                unique_modules.append(m)

        results: List[Dict] = []
        for i in range(population):
            seq_seed = (self.config.seed ^ (0x9E3779B9 * (i + 1))) & 0xFFFF_FFFF
            is_biased = (self._rng.random() < bias_ratio) and bool(unique_modules)

            if is_biased:
                bias_module = self._rng.choice(unique_modules)
                seq, targets = self._gen_stress_sequence(bias_module)
            else:
                seq, targets = self._gen_random_sequence(), []

            results.append({
                "sequence":   seq,
                "targets":    targets[:3],    # cap to 3 for metadata brevity
                "bias_ratio": bias_ratio,
                "seed":       seq_seed,
            })

        return results

    # ── Stress sequence generator (biased path) ───────────────────────────────

    def _gen_stress_sequence(
        self,
        module: str,
    ) -> Tuple[List[str], List[str]]:
        """
        Build a short instruction sequence that stresses *module*.

        Returns ``(lines, targets)`` where *targets* is the list of opcode
        strings actually emitted.
        """
        # Resolve module keyword to a list of opcodes
        ops = _MODULE_OPS.get(module, [])
        if not ops:
            # Fall back: try prefix matching (e.g. "mul_cold" → "mul")
            for key in sorted(_MODULE_OPS.keys(), key=len, reverse=True):
                if key in module:
                    ops = _MODULE_OPS[key]
                    break
        if not ops:
            # No match — emit a random M-ext instruction
            ops = list(_R_TYPE_M)

        targets: List[str] = []
        lines: List[str] = []

        # Emit 3–6 instructions centred on the target opcode, bookended by
        # register setup (li) so the DUT sees interesting operand values.
        op = self._rng.choice(ops)
        targets.append(op)

        rd  = self._pick_dest()
        rs1 = self._pick_dest()   # use dest pool so values are observable
        rs2 = self._pick_dest()

        # Load boundary values to maximise edge-case coverage
        boundary_val = self._rng.choice([
            "0", "-1", "0x7FFFFFFF", "0x80000000", "1", "2",
        ])
        lines.append(f"    li      {rs1}, {boundary_val}")
        lines.append(f"    li      {rs2}, {self._rng.choice(['0', '1', '-1', '2', '3'])}")
        lines.append(f"    {op:<8} {rd}, {rs1}, {rs2}")

        # Optionally chain with a second stress instruction
        if self._rng.random() < 0.5 and len(ops) > 1:
            op2 = self._rng.choice([o for o in ops if o != op] or ops)
            rd2 = self._pick_dest()
            lines.append(f"    {op2:<8} {rd2}, {rd}, {rs1}")
            targets.append(op2)

        return lines, targets

    def _gen_random_sequence(self) -> List[str]:
        """Return a short random instruction sequence (unbiased path)."""
        lines: List[str] = []
        op = self._rng.choice(list(_R_TYPE_M) + list(_R_TYPE_INT))
        rd  = self._pick_dest()
        rs1 = self._pick_src()
        rs2 = self._pick_src()
        lines.append(f"    {op:<8} {rd}, {rs1}, {rs2}")
        return lines

    # ── Full test emission (for standard generate()) ──────────────────────────

    def _emit_group(self, group: str) -> List[str]:
        if group == "alu_r":     return [self._emit_r_type()]
        if group == "alu_m":     return [self._emit_m_type()]
        if group == "alu_i":     return [self._emit_i_alu()]
        if group == "shift_i":   return [self._emit_shift_i()]
        if group == "load":      return [self._emit_load()]
        if group == "store":     return [self._emit_store()]
        if group == "branch":    return self._emit_branch()
        if group == "lui_auipc": return [self._emit_lui_auipc()]
        return ["    nop"]

    def _emit_r_type(self) -> str:
        op  = self._rng.choice(_R_TYPE_INT)
        rd  = self._pick_dest()
        rs1 = self._pick_src()
        rs2 = self._pick_src()
        return f"    {op:<8} {rd}, {rs1}, {rs2}"

    def _emit_m_type(self) -> str:
        op  = self._rng.choices(self._m_ops, weights=self._m_wts, k=1)[0]
        rd  = self._pick_dest()
        rs1 = self._pick_src()
        rs2 = self._pick_src()
        return f"    {op:<8} {rd}, {rs1}, {rs2}"

    def _emit_i_alu(self) -> str:
        op  = self._rng.choice(_I_TYPE_ALU)
        rd  = self._pick_dest()
        rs1 = self._pick_src()
        imm = self._pick_imm12()
        return f"    {op:<8} {rd}, {rs1}, {imm}"

    def _emit_shift_i(self) -> str:
        op    = self._rng.choice(_I_TYPE_SHIFT)
        rd    = self._pick_dest()
        rs1   = self._pick_src()
        shamt = self._rng.randint(0, 31)
        return f"    {op:<8} {rd}, {rs1}, {shamt}"

    def _emit_load(self) -> str:
        op     = self._rng.choice(_LOAD_OPS)
        rd     = self._pick_dest()
        align  = _OP_ALIGN[op]
        offset = self._pick_mem_offset(align)
        return f"    {op:<8} {rd}, {offset}(t6)"

    def _emit_store(self) -> str:
        op     = self._rng.choice(_STORE_OPS)
        rs2    = self._pick_src()
        align  = _OP_ALIGN[op]
        offset = self._pick_mem_offset(align)
        return f"    {op:<8} {rs2}, {offset}(t6)"

    def _emit_branch(self) -> List[str]:
        """Emit a conditional branch that skips exactly one NOP forward."""
        op  = self._rng.choice(_BRANCH_OPS)
        rs1 = self._pick_src()
        rs2 = self._pick_src()
        lbl = self._next_label()
        return [
            f"    {op:<8} {rs1}, {rs2}, {lbl}",
            f"    nop",
            f"{lbl}:",
        ]

    def _emit_lui_auipc(self) -> str:
        op  = self._rng.choice(("lui", "auipc"))
        rd  = self._pick_dest()
        imm = self._rng.randint(0, 0xF_FFFF)
        return f"    {op:<8} {rd}, {imm}"

    def _emit_trap(self) -> List[str]:
        kind = self._rng.choice(("ecall", "illegal"))
        if kind == "ecall":
            return ["    ecall                    /* trap injection: ecall */"]
        return ["    .word 0x00000000          /* trap injection: illegal instr */"]

    # ── Primitive helpers ─────────────────────────────────────────────────────

    def _next_label(self) -> str:
        self._label_counter += 1
        return f".Lrv_{self._label_counter:07d}"

    def _pick_dest(self) -> str:
        return self._rng.choice(self._dest_pool)

    def _pick_src(self) -> str:
        return self._rng.choice(_SRC_REGS)

    def _pick_imm12(self) -> int:
        return self._rng.randint(-2048, 2047)

    def _pick_mem_offset(self, align: int) -> int:
        max_offset = self.config.mem_size - align
        if max_offset < 0:
            return 0
        max_slot = max_offset // align
        slot = self._rng.randint(0, max_slot)
        return slot * align

    # ── Weight helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _normalise_weights(
        weights: Dict[str, float],
    ) -> Tuple[List[float], List[str]]:
        total = sum(weights.values())
        if total <= 0.0:
            raise ValueError("Sum of instruction-group weights must be positive")
        keys = list(weights.keys())
        vals = [weights[k] / total for k in keys]
        return vals, keys

    @staticmethod
    def _normalise_list(values: List[float]) -> List[float]:
        total = sum(values)
        if total <= 0.0:
            raise ValueError("Sum of weights must be positive")
        return [v / total for v in values]

    def _pick_group(self) -> str:
        return self._rng.choices(
            self._weight_keys,
            weights=self._normalised_weights,
            k=1,
        )[0]
