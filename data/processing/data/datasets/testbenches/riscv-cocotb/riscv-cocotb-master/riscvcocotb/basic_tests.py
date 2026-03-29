import cocotb
from cocotb.regression import TestFactory

from riscvmodel.program.tests import RV32ITests


@cocotb.coroutine
def run_test(dut, testbench, program):
    tb = testbench(dut)
    yield tb.basic(program)


class BasicTestFactory(TestFactory):
    def __init__(self, testbench):
        super().__init__(run_test, testbench)
        self.add_option("program", RV32ITests)