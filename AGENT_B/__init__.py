"""AGENT_B — AVA Verification Platform agent package."""

from .testbench_generator import (
    TestbenchGenerator, TBPort, BusInterface, detect_clock_reset, detect_buses,
    generate_for_module, run_from_manifest,
)

__all__ = [
    "TestbenchGenerator", "TBPort", "BusInterface", "detect_clock_reset",
    "detect_buses", "generate_for_module", "run_from_manifest",
]
