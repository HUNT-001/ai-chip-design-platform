import cocotb
from cocotb.triggers import Timer, RisingEdge, FallingEdge
from riscvcocotb.testbench import Testbench

from riscvmodel.variant import RV32I
from riscvmodel.golden import GoldenProgramEndException
from riscvmodel.isa import InstructionNOP, InstructionAND

class PicoRV32Testbench(Testbench):
    _clk = "clk"
    _reset = "resetn"
    _reset_polarity = False

    def __init__(self, dut):
        super().__init__(RV32I, dut, self.rvfi_lookup(dut))
        dut.mem_ready <= 0
        dut.irq <= 0
        self.end_of_program = False

    @cocotb.coroutine
    def busif(self):
        while True:
            if int(self.dut.mem_valid) == 1:
                if int(self.dut.mem_instr) == 1:
                    if self.end_of_program:
                        insn = InstructionNOP().encode()
                    else:
                        try:
                            insn = self.fetch(int(self.dut.mem_addr))
                        except GoldenProgramEndException:
                            insn = InstructionNOP().encode()
                            self.end_of_program = True
                    self.dut.mem_rdata <= insn
                    self.dut.mem_ready <= 1
                    yield FallingEdge(self.sig_clk)
                    self.dut.mem_ready <= 0
            else:
                yield FallingEdge(self.sig_clk)
