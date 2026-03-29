from riscvcocotb.cores.picorv32 import PicoRV32Testbench
from riscvcocotb.basic_tests import BasicTestFactory

tf = BasicTestFactory(PicoRV32Testbench)
tf.generate_tests()
