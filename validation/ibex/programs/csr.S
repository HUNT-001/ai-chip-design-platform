# One CSR, read and written: mscratch, through csrrw/csrrs/csrrc and their
# immediate forms.  mscratch has no WARL fields and no side effects, so both
# simulators must agree on every value.
#include "common.inc"
  VECTORS

  .section .text.start, "ax"
  .option norvc
  .globl _start
_start:
  li     a0, 0x5a5a1234
  li     a1, 0x0000ff00
  csrw   mscratch, a0
  csrr   s0, mscratch
  csrrs  s1, mscratch, a1
  csrrc  s2, mscratch, a0
  csrrwi s3, mscratch, 0x15
  csrrsi s4, mscratch, 0x0a
  csrrci s5, mscratch, 0x01
  csrr   s6, mscratch
  HALT
