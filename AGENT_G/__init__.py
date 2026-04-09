"""
rv32im_testgen
===============
Agent G — Adaptive RV32IM test generation package for the AVA verification pipeline.

Public API
----------
Directed tests::

    from rv32im_testgen import (
        ALL_DIRECTED_TESTS,             # curated 20-test core set
        ALL_DIRECTED_TESTS_EXTENDED,    # full ~48-test extended set
        DirectedTest, verify_all,
        MUL_TESTS, MULH_TESTS, MULHSU_TESTS, MULHU_TESTS,
        DIV_TESTS, DIVU_TESTS, REM_TESTS, REMU_TESTS, COMBINED_TESTS,
    )

Random generation::

    from rv32im_testgen import RV32IMRandomGenerator, GeneratorConfig, InstructionMix

Assembly / ELF output::

    from rv32im_testgen import build_directed_asm, build_random_asm, write_test

Genetic evolution (AVA feedback loop)::

    from rv32im_testgen import GeneticEngine, Individual
"""

from .directed_tests import (
    ALL_DIRECTED_TESTS,
    ALL_DIRECTED_TESTS_EXTENDED,
    DirectedTest,
    MUL_TESTS,
    MULH_TESTS,
    MULHSU_TESTS,
    MULHU_TESTS,
    DIV_TESTS,
    DIVU_TESTS,
    REM_TESTS,
    REMU_TESTS,
    COMBINED_TESTS,
    verify_all,
    get_test_by_name,
)
from .random_gen import (
    RV32IMRandomGenerator,
    GeneratorConfig,
    InstructionMix,
    DEFAULT_WEIGHTS,
)
from .asm_builder import (
    build_directed_asm,
    build_random_asm,
    build_evolved_asm,
    write_test,
    run_spike,
    TOOLCHAIN_PREFIX,
)
from .genetic_engine import GeneticEngine, Individual

__version__ = "3.0.0"
__author__  = "Agent G — AVA Verification Pipeline"

__all__ = [
    # Directed tests
    "ALL_DIRECTED_TESTS",
    "ALL_DIRECTED_TESTS_EXTENDED",
    "DirectedTest",
    "MUL_TESTS", "MULH_TESTS", "MULHSU_TESTS", "MULHU_TESTS",
    "DIV_TESTS", "DIVU_TESTS", "REM_TESTS", "REMU_TESTS", "COMBINED_TESTS",
    "verify_all",
    "get_test_by_name",
    # Random generation
    "RV32IMRandomGenerator",
    "GeneratorConfig",
    "InstructionMix",
    "DEFAULT_WEIGHTS",
    # Assembly
    "build_directed_asm",
    "build_random_asm",
    "build_evolved_asm",
    "write_test",
    "run_spike",
    "TOOLCHAIN_PREFIX",
    # Genetic engine
    "GeneticEngine",
    "Individual",
    # Package metadata
    "__version__",
]
