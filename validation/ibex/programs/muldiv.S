# RV32M: every multiply and divide, including divide by zero and INT_MIN / -1.
#include "common.inc"
  VECTORS

  .section .text.start, "ax"
  .option norvc
  .globl _start
_start:
  li   a0, 0x7fffffff
  li   a1, -3
  li   a2, 0x80000000
  li   a3, -1
  mul    s0, a0, a1
  mulh   s1, a0, a1
  mulhsu s2, a1, a0
  mulhu  s3, a0, a1
  div    s4, a0, a1
  divu   s5, a0, a1
  rem    s6, a0, a1
  remu   s7, a0, a1
  div    s8, a0, zero
  divu   s9, a0, zero
  rem    s10, a0, zero
  remu   s11, a0, zero
  div    t3, a2, a3
  rem    t4, a2, a3
  mul    t5, a2, a3
  mulh   t6, a2, a2
  HALT
