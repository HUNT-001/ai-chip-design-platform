"""Phase-2 demo: two heterogeneous engineers share one Verification Operating
Environment, working a common task board through the frozen kernel.

    python run_voe.py --mock     # no toolchain (pipeline + laws + reputation)
    python run_voe.py --real     # real verilator + sby evidence

Watch for: the explorer sampling with simulation (including a 'pass' on the
buggy DUT that random vectors never break), the skeptic then proving the clean
property and finding the bug with formal — which retroactively flags the
explorer's earlier pass as miscalibrated — and reputation, computed purely from
that evidence, ranking the two.
"""
import sys
from board import Task
from voe import VOE

TASKS = [
    Task("alu_equiv[clean]", 5.0, formal_task="good",  inject_bug=False),
    Task("alu_equiv[dut2]",  5.0, formal_task="buggy", inject_bug=True),
]


def main():
    mock = "--mock" in sys.argv or "--real" not in sys.argv
    print("=== Phase-2 Verification Operating Environment (2 engineers) ===")
    print(f"    mode = {'MOCK (no toolchain)' if mock else 'REAL (verilator + sby)'}\n")
    VOE(TASKS, budget=40.0, mock=mock).run()


if __name__ == "__main__":
    main()
