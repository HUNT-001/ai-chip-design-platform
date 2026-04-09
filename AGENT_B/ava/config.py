"""
ava/config.py — Configuration knobs for the AVA platform.
"""
from __future__ import annotations
import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class AVAConfig:
    # ISA
    xlen:        int            = 32
    extensions:  List[str]      = field(default_factory=lambda: ["I", "M"])
    priv_modes:  List[str]      = field(default_factory=lambda: ["M"])

    # Run control
    seed:        int            = 42
    max_insns:   int            = 10_000
    timeout_sec: int            = 300

    # Coverage goals (0.0–1.0)
    coverage_goals: Dict[str, float] = field(default_factory=lambda: {
        "line": 0.90, "branch": 0.80, "toggle": 0.70
    })

    # RTL backend
    rtl_files:       List[str]  = field(default_factory=list)
    rtl_top:         str        = "top"
    enable_waveform: bool       = False
    verilator_jobs:  int        = 4

    # ISS backend
    spike_path:  str = "spike"
    isa_string:  str = ""       # auto-derived from xlen+extensions if empty

    # Generator
    gen_num_insns: int  = 500
    gen_mem_range: tuple = field(default_factory=lambda: (0x1000, 0x4000))

    # Compliance
    compliance_suite_root: str = ""   # path to riscv-arch-test

    # Output
    run_base_dir:  str = "runs"
    keep_waveform: bool = False

    # -----------------------------------------------------------------------

    def derived_isa(self) -> str:
        if self.isa_string:
            return self.isa_string
        base = f"rv{self.xlen}i"
        extras = [e.lower() for e in self.extensions if e.upper() != "I"]
        return base + "".join(extras)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "AVAConfig":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    @classmethod
    def from_file(cls, path: Path) -> "AVAConfig":
        with open(path) as f:
            return cls.from_dict(json.load(f))

    def to_file(self, path: Path) -> None:
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)
