"""
RISC-V golden reference dataset (high quality)
"""
from dataclasses import dataclass
from typing import Dict, List

@dataclass
class RISCVGoldenExample:
    name: str
    spec: str
    reference_rtl: str
    testbench: Dict
    riscv_subset: str  # "RV32I", "RV32IM", "RV32IMC"
    complexity: str    # "simple", "medium", "complex"

GOLDEN_RISCV = [
    RISCVGoldenExample(
        name="ibex_core",
        spec="RV32IMC core with debug, multipliers, bit manipulation",
        riscv_subset="RV32IMC",
        complexity="complex",
        reference_rtl="""// Extracted from ibex core""",
        testbench={
            "type": "cocotb",
            "test_cases": 50,
            "coverage": {"line": 92.3, "branch": 89.1}
        }
    ),
    RISCVGoldenExample(
        name="picorv32",
        spec="Minimal RV32I/M implementation",
        riscv_subset="RV32IM",
        complexity="simple",
        reference_rtl="""// From picorv32""",
        testbench={}
    ),
    # Add more from your knowledge
]
