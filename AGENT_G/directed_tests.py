"""
rv32im_testgen.directed_tests
==============================
RISC-V M-extension directed test definitions — all mathematical edge cases.

Reference: The RISC-V Instruction Set Manual, Volume I: Unprivileged ISA
           Section 7 "M" Standard Extension for Integer Multiplication and Division

Design principles
-----------------
* Every expected register value is computed by the same Python functions that
  implement the spec semantics.  ``verify_all()`` runs at module import time so
  any regression in the math is caught immediately.
* ``DirectedTest.asm_body`` holds bare instruction strings (no leading spaces);
  the assembler layer adds consistent indentation during rendering.
* The 20-test core set is defined by explicit name, not fragile slicing.
* Each test carries optional ``fitness`` and ``targets`` fields so the
  GeneticEngine can score and track which cold-path constraints it satisfies.

Canonical expected-value formulae (Python 3)
--------------------------------------------
  u32(x)       = x & 0xFFFF_FFFF
  s32(x)       = u32(x) - 2**32 if u32(x) >= 2**31 else u32(x)
  mulh (a, b)  = (s32(a) * s32(b)) >> 32  [masked to u32]
  mulhsu(a, b) = (s32(a) * u32(b)) >> 32  [masked to u32]
  mulhu (a, b) = (u32(a) * u32(b)) >> 32  [masked to u32]
  div   (a, b) = INT_MIN                   if a==INT_MIN and b==UINT_MAX
                 UINT_MAX                  if b == 0
                 int(s32(a) / s32(b))      otherwise  [truncate toward 0]
  divu  (a, b) = UINT_MAX                  if b == 0
                 u32(a) // u32(b)          otherwise
  rem   (a, b) = u32(a)                    if b == 0
                 0                         if a==INT_MIN and b==UINT_MAX
                 u32(s32(a) - div(a,b)*s32(b))  otherwise
  remu  (a, b) = u32(a)                    if b == 0
                 u32(a) % u32(b)           otherwise
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Tuple


# ─── Bit-width helpers (verified against spec) ───────────────────────────────

def _u32(x: int) -> int:
    """Return x as an unsigned 32-bit integer (modulo 2^32)."""
    return x & 0xFFFF_FFFF


def _s32(x: int) -> int:
    """Reinterpret the lower 32 bits of x as a signed 32-bit integer."""
    x = _u32(x)
    return x - (1 << 32) if x >= (1 << 31) else x


# ─── RISC-V M-extension semantics (exact per spec, Table 7.1) ────────────────

def _rv_mul(a: int, b: int) -> int:
    """Lower 32 bits of signed × signed product."""
    return _u32(_s32(a) * _s32(b))


def _rv_mulh(a: int, b: int) -> int:
    """Upper 32 bits of signed × signed 64-bit product."""
    return _u32((_s32(a) * _s32(b)) >> 32)


def _rv_mulhsu(a: int, b: int) -> int:
    """Upper 32 bits of signed rs1 × unsigned rs2 product."""
    return _u32((_s32(a) * _u32(b)) >> 32)


def _rv_mulhu(a: int, b: int) -> int:
    """Upper 32 bits of unsigned × unsigned 64-bit product."""
    return _u32((_u32(a) * _u32(b)) >> 32)


def _rv_div(a: int, b: int) -> int:
    """
    Signed division truncated toward zero (RISC-V spec §7.2).

    Corner cases (Table 7.1):
      divisor == 0            → −1  (all-ones, 0xFFFF_FFFF)
      INT_MIN  ÷ (−1)         → INT_MIN (overflow; no trap)
    """
    a, b = _u32(a), _u32(b)
    if b == 0:
        return 0xFFFF_FFFF
    if a == _INT_MIN and b == _UINT_MAX:   # INT_MIN ÷ −1
        return _INT_MIN
    return _u32(int(_s32(a) / _s32(b)))   # int() truncates toward zero


def _rv_divu(a: int, b: int) -> int:
    """
    Unsigned division truncated toward zero (RISC-V spec §7.2).

    Corner cases:
      divisor == 0 → UINT_MAX (0xFFFF_FFFF)
    """
    a, b = _u32(a), _u32(b)
    if b == 0:
        return 0xFFFF_FFFF
    return a // b


def _rv_rem(a: int, b: int) -> int:
    """
    Signed remainder; sign follows dividend (truncate-toward-zero semantics).

    Corner cases (Table 7.1):
      divisor  == 0          → dividend unchanged
      INT_MIN  %  (−1)       → 0  (companion to DIV overflow)
    """
    a, b = _u32(a), _u32(b)
    if b == 0:
        return a
    if a == _INT_MIN and b == _UINT_MAX:
        return 0
    quotient = int(_s32(a) / _s32(b))   # truncate toward zero
    return _u32(_s32(a) - quotient * _s32(b))


def _rv_remu(a: int, b: int) -> int:
    """
    Unsigned remainder.

    Corner cases:
      divisor == 0 → dividend unchanged
    """
    a, b = _u32(a), _u32(b)
    if b == 0:
        return a
    return a % b


# ─── Integer boundary constants ───────────────────────────────────────────────

_INT_MIN  = 0x8000_0000   # −2 147 483 648 as unsigned bit pattern
_INT_MAX  = 0x7FFF_FFFF   #  2 147 483 647
_UINT_MAX = 0xFFFF_FFFF   #  4 294 967 295


# ─── Test dataclass ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DirectedTest:
    """
    A single directed RISC-V M-extension test case.

    Attributes
    ----------
    name
        Unique, filesystem-safe identifier (lowercase, underscores only).
    description
        One-line human-readable explanation of the edge case being tested.
    category
        Dot-separated hierarchy (e.g. ``"div.zero"``, ``"mulhsu.sign"``).
    asm_body
        Tuple of assembly instruction strings — NO leading whitespace.
        Labels (ending with ``:``) are kept at column 0 by the renderer.
    expected_regs
        Mapping of ABI register name → expected unsigned 32-bit value after
        the test body executes.  Omit ``x0`` (always 0 per ISA; unobservable).
    spec_ref
        Optional RISC-V specification section reference (informational).
    fitness
        Genetic fitness score computed against Agent F cold-paths and Agent D
        hypotheses.  0.0 for static baseline tests; updated by GeneticEngine
        via ``with_fitness()``.  Higher = better coverage contribution.
    targets
        Tuple of constraint module/path strings this test is intended to hit
        (populated by GeneticEngine when the test is evolved or scored).
    """

    name: str
    description: str
    category: str
    asm_body: Tuple[str, ...]
    expected_regs: Dict[str, int] = field(default_factory=dict)
    spec_ref: str = ""
    fitness: float = 0.0
    targets: Tuple[str, ...] = field(default_factory=tuple)

    # ── Factory that accepts lists for asm_body and targets ────────────────

    @classmethod
    def make(
        cls,
        name: str,
        description: str,
        category: str,
        asm_body: List[str],
        expected_regs: Optional[Dict[str, int]] = None,
        spec_ref: str = "",
        fitness: float = 0.0,
        targets: Optional[List[str]] = None,
    ) -> "DirectedTest":
        """Convenience constructor accepting lists for *asm_body* and *targets*."""
        return cls(
            name=name,
            description=description,
            category=category,
            asm_body=tuple(asm_body),
            expected_regs=dict(expected_regs) if expected_regs else {},
            spec_ref=spec_ref,
            fitness=fitness,
            targets=tuple(targets) if targets else (),
        )

    def with_fitness(
        self,
        fitness: float,
        targets: Optional[List[str]] = None,
    ) -> "DirectedTest":
        """
        Return a new frozen instance with updated *fitness* and *targets*.

        Because the dataclass is frozen, mutation is not possible in-place;
        this factory pattern is the correct way to update genetic metadata
        without breaking hash-based set membership or dict keying.
        """
        return DirectedTest(
            name=self.name,
            description=self.description,
            category=self.category,
            asm_body=self.asm_body,
            expected_regs=dict(self.expected_regs),
            spec_ref=self.spec_ref,
            fitness=fitness,
            targets=tuple(targets) if targets else self.targets,
        )

    def evaluate_fitness(self, constraints: List[Dict]) -> float:
        """
        Compute a fitness score relative to a list of Agent F/D constraints.

        Scoring rules (additive):
        * +1.0  per constraint whose ``"module"`` keyword appears in the test
                name or category (case-insensitive substring match).
        * +1.5  bonus if the test targets any MUL opcode family (highest
                coverage value in M-extension verification).
        * +1.0  bonus if the test targets any DIV/REM opcode family.
        * +0.5  per constraint whose ``"hypothesis"`` text substring matches
                the test description (Agent D hypotheses).
        * +0.5  bonus if the constraint has ``reachability < 0.2`` (cold path;
                harder to reach → higher reward for hitting it).

        Parameters
        ----------
        constraints
            Unified list of Agent F cold-path dicts and Agent D hypothesis
            dicts, each containing at minimum one of:
            ``{"module": str, "reachability": float}``  (Agent F format)
            ``{"module": str, "hypothesis": str, "confidence": float}``  (Agent D format)

        Returns
        -------
        float
            Non-negative fitness score.  Higher = more valuable for coverage.
        """
        score: float = 0.0
        name_lower = self.name.lower()
        cat_lower  = self.category.lower()
        desc_lower = self.description.lower()

        for c in constraints:
            module = str(c.get("module", "")).lower()
            if module and (module in name_lower or module in cat_lower):
                score += 1.0
                reachability = float(c.get("reachability", 1.0))
                if reachability < 0.2:
                    score += 0.5   # cold-path bonus

            hypothesis = str(c.get("hypothesis", "")).lower()
            if hypothesis and any(
                word in desc_lower for word in hypothesis.split() if len(word) > 3
            ):
                score += 0.5

        # Opcode-family bonuses
        mul_keywords = ("mul",)
        div_keywords = ("div", "divu")
        rem_keywords = ("rem", "remu")
        if any(k in name_lower for k in mul_keywords):
            score += 1.5
        if any(k in name_lower for k in div_keywords + rem_keywords):
            score += 1.0

        return score

    # ── Structural validation ──────────────────────────────────────────────

    def validate(self) -> List[str]:
        """
        Return a list of validation error strings (empty list ⇒ valid).

        Checks:
        * name is non-empty and contains only [a-z0-9_]
        * asm_body is non-empty
        * every expected_regs value is a valid unsigned 32-bit integer
        * fitness is a finite non-negative float
        """
        errors: List[str] = []
        valid_chars = set("abcdefghijklmnopqrstuvwxyz0123456789_")
        if not self.name:
            errors.append("name must not be empty")
        elif not all(c in valid_chars for c in self.name):
            errors.append(
                f"name {self.name!r}: only [a-z0-9_] allowed (got {self.name!r})"
            )
        if not self.asm_body:
            errors.append("asm_body must not be empty")
        for reg, val in self.expected_regs.items():
            if not isinstance(val, int) or not (0 <= val <= 0xFFFF_FFFF):
                errors.append(
                    f"expected_regs[{reg!r}] = {val!r} is not a valid u32"
                )
        import math
        if not isinstance(self.fitness, (int, float)) or math.isnan(self.fitness) or self.fitness < 0.0:
            errors.append(f"fitness={self.fitness!r} must be a non-negative finite float")
        return errors


# ══════════════════════════════════════════════════════════════════════════════
# MUL — lower 32 bits of rs1_signed × rs2_signed
# §7.1: "MUL performs an XLEN-bit×XLEN-bit multiplication … places the lower
#        XLEN bits in the destination register."
# ══════════════════════════════════════════════════════════════════════════════

MUL_TESTS: List[DirectedTest] = [
    DirectedTest.make(
        name="mul_zero_operand",
        description="MUL: any_value × 0 = 0",
        category="mul.special",
        asm_body=[
            "li   t0, 0x12345678",
            "li   t1, 0",
            "mul  t2, t0, t1",
        ],
        expected_regs={"t2": _rv_mul(0x12345678, 0)},
        spec_ref="§7.1",
    ),
    DirectedTest.make(
        name="mul_identity",
        description="MUL: x × 1 = x (multiplicative identity)",
        category="mul.special",
        asm_body=[
            "li   t0, 0x12345678",
            "li   t1, 1",
            "mul  t2, t0, t1",
        ],
        expected_regs={"t2": _rv_mul(0x12345678, 1)},
        spec_ref="§7.1",
    ),
    DirectedTest.make(
        name="mul_neg_one",
        description="MUL: x × (−1) = two's complement negation of x",
        category="mul.sign",
        asm_body=[
            "li   t0, 7",
            "li   t1, -1",
            "mul  t2, t0, t1",
        ],
        expected_regs={"t2": _rv_mul(7, _UINT_MAX)},
        spec_ref="§7.1",
    ),
    DirectedTest.make(
        name="mul_both_negative",
        description="MUL: negative × negative = positive (lower 32 bits)",
        category="mul.sign",
        asm_body=[
            "li   t0, -3",
            "li   t1, -4",
            "mul  t2, t0, t1",
        ],
        expected_regs={"t2": _rv_mul(_u32(-3), _u32(-4))},
        spec_ref="§7.1",
    ),
    DirectedTest.make(
        name="mul_overflow_wrap",
        description="MUL: INT_MAX × 2 overflows; lower 32 bits = 0xFFFFFFFE",
        category="mul.overflow",
        asm_body=[
            "li   t0, 0x7FFFFFFF",
            "li   t1, 2",
            "mul  t2, t0, t1",
        ],
        expected_regs={"t2": _rv_mul(_INT_MAX, 2)},
        spec_ref="§7.1",
    ),
    DirectedTest.make(
        name="mul_int_min_squared",
        description="MUL: INT_MIN × INT_MIN lower 32 = 0 (product = 2^62)",
        category="mul.overflow",
        asm_body=[
            "li   t0, 0x80000000",
            "mv   t1, t0",
            "mul  t2, t0, t1",
        ],
        expected_regs={"t2": _rv_mul(_INT_MIN, _INT_MIN)},
        spec_ref="§7.1",
    ),
    DirectedTest.make(
        name="mul_x0_destination",
        description=(
            "MUL into x0: write must be suppressed; "
            "tests that no illegal-instruction trap is raised"
        ),
        category="mul.special",
        asm_body=[
            "li   t0, 42",
            "li   t1, 99",
            "mul  x0, t0, t1",
        ],
        expected_regs={},
        spec_ref="§2.6",
    ),
]


# ══════════════════════════════════════════════════════════════════════════════
# MULH — upper 32 bits of signed rs1 × signed rs2 (64-bit product)
# ══════════════════════════════════════════════════════════════════════════════

MULH_TESTS: List[DirectedTest] = [
    DirectedTest.make(
        name="mulh_small_positive_zero_upper",
        description="MULH: 100 × 200 = 20 000 fits in 32 bits; upper 32 = 0",
        category="mulh.basic",
        asm_body=[
            "li   t0, 100",
            "li   t1, 200",
            "mulh t2, t0, t1",
        ],
        expected_regs={"t2": _rv_mulh(100, 200)},
        spec_ref="§7.1",
    ),
    DirectedTest.make(
        name="mulh_int_max_squared",
        description="MULH: INT_MAX² upper 32 = 0x3FFFFFFF",
        category="mulh.overflow",
        asm_body=[
            "li   t0, 0x7FFFFFFF",
            "mv   t1, t0",
            "mulh t2, t0, t1",
        ],
        expected_regs={"t2": _rv_mulh(_INT_MAX, _INT_MAX)},
        spec_ref="§7.1",
    ),
    DirectedTest.make(
        name="mulh_int_min_squared",
        description="MULH: INT_MIN² upper 32 = 0x40000000 (product = 2^62)",
        category="mulh.overflow",
        asm_body=[
            "li   t0, 0x80000000",
            "mv   t1, t0",
            "mulh t2, t0, t1",
        ],
        expected_regs={"t2": _rv_mulh(_INT_MIN, _INT_MIN)},
        spec_ref="§7.1",
    ),
    DirectedTest.make(
        name="mulh_negative_times_positive",
        description="MULH: INT_MIN × 2 → product −2^32; upper 32 = −1 (0xFFFFFFFF)",
        category="mulh.sign",
        asm_body=[
            "li   t0, 0x80000000",
            "li   t1, 2",
            "mulh t2, t0, t1",
        ],
        expected_regs={"t2": _rv_mulh(_INT_MIN, 2)},
        spec_ref="§7.1",
    ),
    DirectedTest.make(
        name="mulh_int_min_times_neg_one",
        description="MULH: INT_MIN × (−1) = 2^31; upper 32 = 0 (no carry to upper half)",
        category="mulh.sign",
        asm_body=[
            "li   t0, 0x80000000",
            "li   t1, -1",
            "mulh t2, t0, t1",
        ],
        expected_regs={"t2": _rv_mulh(_INT_MIN, _UINT_MAX)},
        spec_ref="§7.1",
    ),
]


# ══════════════════════════════════════════════════════════════════════════════
# MULHSU — upper 32 bits of signed rs1 × unsigned rs2
# ══════════════════════════════════════════════════════════════════════════════

MULHSU_TESTS: List[DirectedTest] = [
    DirectedTest.make(
        name="mulhsu_positive_times_uint_max",
        description="MULHSU: 1 × UINT_MAX = 2^32−1; upper 32 = 0 (fits in 32 bits)",
        category="mulhsu.basic",
        asm_body=[
            "li   t0, 1",
            "li   t1, -1",
            "mulhsu t2, t0, t1",
        ],
        expected_regs={"t2": _rv_mulhsu(1, _UINT_MAX)},
        spec_ref="§7.1",
    ),
    DirectedTest.make(
        name="mulhsu_int_min_times_uint_max",
        description=(
            "MULHSU: INT_MIN(signed) × UINT_MAX(unsigned); "
            "product = −2^63 + 2^31 = 0x8000000080000000; upper 32 = 0x80000000"
        ),
        category="mulhsu.sign",
        asm_body=[
            "li   t0, 0x80000000",
            "li   t1, -1",
            "mulhsu t2, t0, t1",
        ],
        expected_regs={"t2": _rv_mulhsu(_INT_MIN, _UINT_MAX)},
        spec_ref="§7.1",
    ),
    DirectedTest.make(
        name="mulhsu_zero_rs1",
        description="MULHSU: 0(signed) × UINT_MAX(unsigned) = 0; upper 32 = 0",
        category="mulhsu.basic",
        asm_body=[
            "li   t0, 0",
            "li   t1, -1",
            "mulhsu t2, t0, t1",
        ],
        expected_regs={"t2": _rv_mulhsu(0, _UINT_MAX)},
        spec_ref="§7.1",
    ),
]


# ══════════════════════════════════════════════════════════════════════════════
# MULHU — upper 32 bits of unsigned rs1 × unsigned rs2
# ══════════════════════════════════════════════════════════════════════════════

MULHU_TESTS: List[DirectedTest] = [
    DirectedTest.make(
        name="mulhu_uint_max_squared",
        description="MULHU: UINT_MAX² upper 32 = 0xFFFFFFFE",
        category="mulhu.overflow",
        asm_body=[
            "li   t0, -1",
            "mv   t1, t0",
            "mulhu t2, t0, t1",
        ],
        expected_regs={"t2": _rv_mulhu(_UINT_MAX, _UINT_MAX)},
        spec_ref="§7.1",
    ),
    DirectedTest.make(
        name="mulhu_zero_operand",
        description="MULHU: 0 × UINT_MAX = 0; upper 32 = 0",
        category="mulhu.basic",
        asm_body=[
            "li   t0, 0",
            "li   t1, -1",
            "mulhu t2, t0, t1",
        ],
        expected_regs={"t2": _rv_mulhu(0, _UINT_MAX)},
        spec_ref="§7.1",
    ),
    DirectedTest.make(
        name="mulhu_two_times_int_min_boundary",
        description="MULHU: 2 × 2^31 = 2^32 → upper 32 = 1 (exact carry-out)",
        category="mulhu.boundary",
        asm_body=[
            "li   t0, 2",
            "li   t1, 0x80000000",
            "mulhu t2, t0, t1",
        ],
        expected_regs={"t2": _rv_mulhu(2, _INT_MIN)},
        spec_ref="§7.1",
    ),
    DirectedTest.make(
        name="mulhu_one_times_uint_max",
        description="MULHU: 1 × UINT_MAX = UINT_MAX; upper 32 = 0 (no carry)",
        category="mulhu.basic",
        asm_body=[
            "li   t0, 1",
            "li   t1, -1",
            "mulhu t2, t0, t1",
        ],
        expected_regs={"t2": _rv_mulhu(1, _UINT_MAX)},
        spec_ref="§7.1",
    ),
]


# ══════════════════════════════════════════════════════════════════════════════
# DIV — signed division, truncated toward zero
# ══════════════════════════════════════════════════════════════════════════════

DIV_TESTS: List[DirectedTest] = [
    DirectedTest.make(
        name="div_positive_basic",
        description="DIV: 20 ÷ 4 = 5 (exact, positive operands)",
        category="div.basic",
        asm_body=[
            "li   t0, 20",
            "li   t1, 4",
            "div  t2, t0, t1",
        ],
        expected_regs={"t2": _rv_div(20, 4)},
        spec_ref="§7.2",
    ),
    DirectedTest.make(
        name="div_negative_dividend",
        description="DIV: −20 ÷ 4 = −5 (truncate toward zero)",
        category="div.sign",
        asm_body=[
            "li   t0, -20",
            "li   t1, 4",
            "div  t2, t0, t1",
        ],
        expected_regs={"t2": _rv_div(_u32(-20), 4)},
        spec_ref="§7.2",
    ),
    DirectedTest.make(
        name="div_negative_divisor",
        description="DIV: 20 ÷ (−4) = −5",
        category="div.sign",
        asm_body=[
            "li   t0, 20",
            "li   t1, -4",
            "div  t2, t0, t1",
        ],
        expected_regs={"t2": _rv_div(20, _u32(-4))},
        spec_ref="§7.2",
    ),
    DirectedTest.make(
        name="div_both_negative",
        description="DIV: −20 ÷ (−4) = 5 (neg ÷ neg = positive)",
        category="div.sign",
        asm_body=[
            "li   t0, -20",
            "li   t1, -4",
            "div  t2, t0, t1",
        ],
        expected_regs={"t2": _rv_div(_u32(-20), _u32(-4))},
        spec_ref="§7.2",
    ),
    DirectedTest.make(
        name="div_by_zero_nonzero_dividend",
        description="DIV: 42 ÷ 0 → −1 (0xFFFFFFFF); no exception per spec",
        category="div.zero",
        asm_body=[
            "li   t0, 42",
            "li   t1, 0",
            "div  t2, t0, t1",
        ],
        expected_regs={"t2": _rv_div(42, 0)},
        spec_ref="§7.2 Table 7.1",
    ),
    DirectedTest.make(
        name="div_by_zero_zero_dividend",
        description="DIV: 0 ÷ 0 → −1 (0xFFFFFFFF); both operands zero",
        category="div.zero",
        asm_body=[
            "li   t0, 0",
            "li   t1, 0",
            "div  t2, t0, t1",
        ],
        expected_regs={"t2": _rv_div(0, 0)},
        spec_ref="§7.2 Table 7.1",
    ),
    DirectedTest.make(
        name="div_overflow_int_min_by_neg_one",
        description=(
            "DIV: INT_MIN ÷ (−1) → INT_MIN; "
            "mathematical result 2^31 overflows; spec mandates INT_MIN with no trap"
        ),
        category="div.overflow",
        asm_body=[
            "li   t0, 0x80000000",
            "li   t1, -1",
            "div  t2, t0, t1",
        ],
        expected_regs={"t2": _rv_div(_INT_MIN, _UINT_MAX)},
        spec_ref="§7.2 Table 7.1",
    ),
    DirectedTest.make(
        name="div_truncate_toward_zero_negative",
        description="DIV: −7 ÷ 2 = −3 (truncated toward 0, NOT floor −4)",
        category="div.rounding",
        asm_body=[
            "li   t0, -7",
            "li   t1, 2",
            "div  t2, t0, t1",
        ],
        expected_regs={"t2": _rv_div(_u32(-7), 2)},
        spec_ref="§7.2",
    ),
    DirectedTest.make(
        name="div_int_max_by_one",
        description="DIV: INT_MAX ÷ 1 = INT_MAX (identity)",
        category="div.basic",
        asm_body=[
            "li   t0, 0x7FFFFFFF",
            "li   t1, 1",
            "div  t2, t0, t1",
        ],
        expected_regs={"t2": _rv_div(_INT_MAX, 1)},
        spec_ref="§7.2",
    ),
    DirectedTest.make(
        name="div_int_min_by_one",
        description="DIV: INT_MIN ÷ 1 = INT_MIN (identity, distinct from overflow case)",
        category="div.basic",
        asm_body=[
            "li   t0, 0x80000000",
            "li   t1, 1",
            "div  t2, t0, t1",
        ],
        expected_regs={"t2": _rv_div(_INT_MIN, 1)},
        spec_ref="§7.2",
    ),
]


# ══════════════════════════════════════════════════════════════════════════════
# DIVU — unsigned division, truncated toward zero
# ══════════════════════════════════════════════════════════════════════════════

DIVU_TESTS: List[DirectedTest] = [
    DirectedTest.make(
        name="divu_positive_basic",
        description="DIVU: 100 ÷ 4 = 25 (unsigned, no remainder)",
        category="divu.basic",
        asm_body=[
            "li   t0, 100",
            "li   t1, 4",
            "divu t2, t0, t1",
        ],
        expected_regs={"t2": _rv_divu(100, 4)},
        spec_ref="§7.2",
    ),
    DirectedTest.make(
        name="divu_by_zero",
        description="DIVU: 42 ÷ 0 → UINT_MAX (0xFFFFFFFF); no exception",
        category="divu.zero",
        asm_body=[
            "li   t0, 42",
            "li   t1, 0",
            "divu t2, t0, t1",
        ],
        expected_regs={"t2": _rv_divu(42, 0)},
        spec_ref="§7.2 Table 7.1",
    ),
    DirectedTest.make(
        name="divu_uint_max_by_one",
        description="DIVU: UINT_MAX ÷ 1 = UINT_MAX (identity for unsigned max)",
        category="divu.basic",
        asm_body=[
            "li   t0, -1",
            "li   t1, 1",
            "divu t2, t0, t1",
        ],
        expected_regs={"t2": _rv_divu(_UINT_MAX, 1)},
        spec_ref="§7.2",
    ),
    DirectedTest.make(
        name="divu_int_min_bit_pattern",
        description=(
            "DIVU: 0x80000000 (2^31 unsigned) ÷ 2 = 0x40000000; "
            "differs from DIV because sign bit is NOT extended"
        ),
        category="divu.sign_bits",
        asm_body=[
            "li   t0, 0x80000000",
            "li   t1, 2",
            "divu t2, t0, t1",
        ],
        expected_regs={"t2": _rv_divu(_INT_MIN, 2)},
        spec_ref="§7.2",
    ),
]


# ══════════════════════════════════════════════════════════════════════════════
# REM — signed remainder; sign of result matches dividend (truncate semantics)
# ══════════════════════════════════════════════════════════════════════════════

REM_TESTS: List[DirectedTest] = [
    DirectedTest.make(
        name="rem_positive_basic",
        description="REM: 20 % 6 = 2 (positive operands)",
        category="rem.basic",
        asm_body=[
            "li   t0, 20",
            "li   t1, 6",
            "rem  t2, t0, t1",
        ],
        expected_regs={"t2": _rv_rem(20, 6)},
        spec_ref="§7.2",
    ),
    DirectedTest.make(
        name="rem_negative_dividend_sign_follows_dividend",
        description=(
            "REM: −7 % 3 = −1; sign follows dividend. "
            "Truncation: −7 = 3×(−2) + (−1)"
        ),
        category="rem.sign",
        asm_body=[
            "li   t0, -7",
            "li   t1, 3",
            "rem  t2, t0, t1",
        ],
        expected_regs={"t2": _rv_rem(_u32(-7), 3)},
        spec_ref="§7.2",
    ),
    DirectedTest.make(
        name="rem_negative_divisor_sign_follows_dividend",
        description=(
            "REM: 7 % (−3) = 1; sign follows positive dividend. "
            "Truncation: 7 = (−3)×(−2) + 1"
        ),
        category="rem.sign",
        asm_body=[
            "li   t0, 7",
            "li   t1, -3",
            "rem  t2, t0, t1",
        ],
        expected_regs={"t2": _rv_rem(7, _u32(-3))},
        spec_ref="§7.2",
    ),
    DirectedTest.make(
        name="rem_by_zero_returns_dividend",
        description="REM: 42 % 0 → 42 (dividend returned unchanged per spec)",
        category="rem.zero",
        asm_body=[
            "li   t0, 42",
            "li   t1, 0",
            "rem  t2, t0, t1",
        ],
        expected_regs={"t2": _rv_rem(42, 0)},
        spec_ref="§7.2 Table 7.1",
    ),
    DirectedTest.make(
        name="rem_negative_by_zero_returns_dividend",
        description="REM: −42 % 0 → −42 (negative dividend preserved)",
        category="rem.zero",
        asm_body=[
            "li   t0, -42",
            "li   t1, 0",
            "rem  t2, t0, t1",
        ],
        expected_regs={"t2": _rv_rem(_u32(-42), 0)},
        spec_ref="§7.2 Table 7.1",
    ),
    DirectedTest.make(
        name="rem_overflow_int_min_neg_one_is_zero",
        description=(
            "REM: INT_MIN % (−1) → 0; companion to DIV overflow corner case"
        ),
        category="rem.overflow",
        asm_body=[
            "li   t0, 0x80000000",
            "li   t1, -1",
            "rem  t2, t0, t1",
        ],
        expected_regs={"t2": _rv_rem(_INT_MIN, _UINT_MAX)},
        spec_ref="§7.2 Table 7.1",
    ),
    DirectedTest.make(
        name="rem_exact_divisible",
        description="REM: 12 % 4 = 0 (exact division; zero remainder)",
        category="rem.basic",
        asm_body=[
            "li   t0, 12",
            "li   t1, 4",
            "rem  t2, t0, t1",
        ],
        expected_regs={"t2": _rv_rem(12, 4)},
        spec_ref="§7.2",
    ),
]


# ══════════════════════════════════════════════════════════════════════════════
# REMU — unsigned remainder
# ══════════════════════════════════════════════════════════════════════════════

REMU_TESTS: List[DirectedTest] = [
    DirectedTest.make(
        name="remu_positive_basic",
        description="REMU: 20 % 6 = 2 (unsigned)",
        category="remu.basic",
        asm_body=[
            "li   t0, 20",
            "li   t1, 6",
            "remu t2, t0, t1",
        ],
        expected_regs={"t2": _rv_remu(20, 6)},
        spec_ref="§7.2",
    ),
    DirectedTest.make(
        name="remu_by_zero_returns_dividend",
        description="REMU: 42 % 0 → 42 (dividend returned unchanged per spec)",
        category="remu.zero",
        asm_body=[
            "li   t0, 42",
            "li   t1, 0",
            "remu t2, t0, t1",
        ],
        expected_regs={"t2": _rv_remu(42, 0)},
        spec_ref="§7.2 Table 7.1",
    ),
    DirectedTest.make(
        name="remu_uint_max_mod_two",
        description="REMU: UINT_MAX % 2 = 1 (UINT_MAX is odd)",
        category="remu.basic",
        asm_body=[
            "li   t0, -1",
            "li   t1, 2",
            "remu t2, t0, t1",
        ],
        expected_regs={"t2": _rv_remu(_UINT_MAX, 2)},
        spec_ref="§7.2",
    ),
    DirectedTest.make(
        name="remu_int_min_bit_pattern_mod_three",
        description="REMU: 0x80000000 (2^31 unsigned) % 3 = 2",
        category="remu.basic",
        asm_body=[
            "li   t0, 0x80000000",
            "li   t1, 3",
            "remu t2, t0, t1",
        ],
        expected_regs={"t2": _rv_remu(_INT_MIN, 3)},
        spec_ref="§7.2",
    ),
]


# ══════════════════════════════════════════════════════════════════════════════
# COMBINED — multi-instruction identity/consistency tests
# ══════════════════════════════════════════════════════════════════════════════

COMBINED_TESTS: List[DirectedTest] = [
    DirectedTest.make(
        name="combined_mul_div_roundtrip",
        description="(a × b) ÷ b = a for values that fit without overflow",
        category="combined",
        asm_body=[
            "li   t0, 17",
            "li   t1, 5",
            "mul  t2, t0, t1",
            "div  t3, t2, t1",
        ],
        expected_regs={"t3": _rv_div(_rv_mul(17, 5), 5)},
        spec_ref="§7.1–7.2",
    ),
    DirectedTest.make(
        name="combined_rem_div_euclidean_identity",
        description="a = (a÷b)×b + (a%b) for any non-zero divisor",
        category="combined",
        asm_body=[
            "li   t0, 37",
            "li   t1, 7",
            "div  t2, t0, t1",
            "rem  t3, t0, t1",
            "mul  t4, t2, t1",
            "add  t5, t4, t3",
        ],
        expected_regs={"t5": 37},
        spec_ref="§7.2",
    ),
    DirectedTest.make(
        name="combined_divu_zero_chain",
        description=(
            "8 % 8 = 0; then DIVU(99, 0) = UINT_MAX. "
            "Verifies no trap on div-by-zero when divisor comes from prior REM."
        ),
        category="combined",
        asm_body=[
            "li   t0, 8",
            "li   t1, 8",
            "remu t2, t0, t1",
            "li   t3, 99",
            "divu t4, t3, t2",
        ],
        expected_regs={"t4": _rv_divu(99, 0)},
        spec_ref="§7.2 Table 7.1",
    ),
    DirectedTest.make(
        name="combined_mulh_mul_full_64bit_reconstruction",
        description=(
            "MULH + MUL together reconstruct the full 64-bit product. "
            "Upper half (t3) and lower half (t2) verified by ISS tandem comparison."
        ),
        category="combined",
        asm_body=[
            "li   t0, 0x12345678",
            "li   t1, 0x9ABCDEF0",
            "mul  t2, t0, t1",
            "mulh t3, t0, t1",
        ],
        expected_regs={
            "t2": _rv_mul(0x12345678, 0x9ABCDEF0),
            "t3": _rv_mulh(0x12345678, 0x9ABCDEF0),
        },
        spec_ref="§7.1",
    ),
]


# ══════════════════════════════════════════════════════════════════════════════
# Test pool assembly
# ══════════════════════════════════════════════════════════════════════════════

ALL_DIRECTED_TESTS_EXTENDED: List[DirectedTest] = (
    MUL_TESTS
    + MULH_TESTS
    + MULHSU_TESTS
    + MULHU_TESTS
    + DIV_TESTS
    + DIVU_TESTS
    + REM_TESTS
    + REMU_TESTS
    + COMBINED_TESTS
)

# Curated 20-test core set — explicit names guarantee deterministic selection
_CORE_20_NAMES: Tuple[str, ...] = (
    # MUL (3): sign, overflow ×2
    "mul_neg_one",
    "mul_overflow_wrap",
    "mul_int_min_squared",
    # MULH (3): overflow ×2, sign
    "mulh_int_max_squared",
    "mulh_int_min_squared",
    "mulh_negative_times_positive",
    # MULHSU (2): basic, sign
    "mulhsu_int_min_times_uint_max",
    "mulhsu_zero_rs1",
    # MULHU (2): overflow, boundary
    "mulhu_uint_max_squared",
    "mulhu_two_times_int_min_boundary",
    # DIV (4): zero ×2, overflow, rounding
    "div_by_zero_nonzero_dividend",
    "div_by_zero_zero_dividend",
    "div_overflow_int_min_by_neg_one",
    "div_truncate_toward_zero_negative",
    # DIVU (2): zero, sign-bit interpretation
    "divu_by_zero",
    "divu_int_min_bit_pattern",
    # REM (2): zero, overflow
    "rem_by_zero_returns_dividend",
    "rem_overflow_int_min_neg_one_is_zero",
    # REMU (1) + combined (1)
    "remu_by_zero_returns_dividend",
    "combined_rem_div_euclidean_identity",
)

assert len(_CORE_20_NAMES) == 20, (
    f"Core set must contain exactly 20 names, got {len(_CORE_20_NAMES)}"
)

_ALL_BY_NAME: Dict[str, DirectedTest] = {
    t.name: t for t in ALL_DIRECTED_TESTS_EXTENDED
}

_missing_from_pool = [n for n in _CORE_20_NAMES if n not in _ALL_BY_NAME]
if _missing_from_pool:
    raise RuntimeError(
        f"Core-20 references names not found in extended pool: {_missing_from_pool}"
    )

ALL_DIRECTED_TESTS: List[DirectedTest] = [
    _ALL_BY_NAME[n] for n in _CORE_20_NAMES
]


# ─── Public lookup helper ─────────────────────────────────────────────────────

def get_test_by_name(name: str) -> Optional[DirectedTest]:
    """Return the test with *name* from the extended pool, or None."""
    return _ALL_BY_NAME.get(name)


# ─── Module-load self-verification ───────────────────────────────────────────

def verify_all(pool: Optional[List[DirectedTest]] = None) -> bool:
    """
    Validate every test in *pool* (defaults to the full extended set).

    Checks:
    * No duplicate names.
    * Each test passes ``DirectedTest.validate()``.
    * All expected_regs values are reachable unsigned 32-bit integers.

    Returns ``True`` on success; raises ``AssertionError`` listing all failures.
    """
    if pool is None:
        pool = ALL_DIRECTED_TESTS_EXTENDED

    seen_names: FrozenSet[str] = frozenset()
    errors: List[str] = []

    for test in pool:
        if test.name in seen_names:
            errors.append(f"Duplicate test name: {test.name!r}")
        seen_names = seen_names | {test.name}
        for err in test.validate():
            errors.append(f"[{test.name}] {err}")

    if errors:
        raise AssertionError(
            f"Directed test validation found {len(errors)} error(s):\n"
            + "\n".join(f"  • {e}" for e in errors)
        )
    return True


# Eager validation — any mathematical or structural bug surfaces at import time
verify_all()
