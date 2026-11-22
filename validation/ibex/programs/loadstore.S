# Stores of every width at every legal offset, from registers whose upper bytes
# are non-zero, so a store that reports the whole register instead of the
# bytes it wrote is visible; then every load width reads them back.
#include "common.inc"
  VECTORS

  .section .text.start, "ax"
  .option norvc
  .globl _start
_start:
  la   s0, buf
  li   a0, 0x8899aabb
  li   a1, 0x11223344
  li   a2, 0xcafef00d

  sw   a0, 0(s0)
  sw   a0, 4(s0)
  sb   a1, 0(s0)
  sb   a1, 1(s0)
  sb   a1, 2(s0)
  sb   a1, 3(s0)
  sh   a2, 4(s0)
  sh   a2, 6(s0)
  sw   a2, 8(s0)

  lw   t0, 0(s0)
  lw   t1, 4(s0)
  lb   t2, 4(s0)
  lbu  t3, 5(s0)
  lh   t4, 6(s0)
  lhu  t5, 4(s0)
  lw   t6, 8(s0)
  lb   s1, 11(s0)
  HALT

  .section .data
  .balign 16
buf:
  .word 0, 0, 0, 0
