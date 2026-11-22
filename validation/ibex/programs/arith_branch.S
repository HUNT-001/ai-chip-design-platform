# RV32I arithmetic, logic, shifts, compares, branches taken and not taken, jumps.
#include "common.inc"
  VECTORS

  .section .text.start, "ax"
  .option norvc
  .globl _start
_start:
  li   a0, 0x12345678
  li   a1, -7
  add  a2, a0, a1
  sub  a3, a0, a1
  xor  a4, a0, a1
  or   a5, a0, a1
  and  a6, a0, a1
  slt  a7, a1, a0
  sltu s2, a1, a0
  slli s3, a0, 7
  srli s4, a1, 3
  srai s5, a1, 3
  sll  s6, a0, a1
  srl  s7, a0, a1
  sra  s8, a1, a1
  lui  s9, 0xfedcb
  auipc s10, 0x1
  xori s11, a0, -1
  sltiu t3, a1, 5

  # countdown loop: bne taken four times, not taken once
  li   t4, 5
  li   t5, 0
loop:
  add  t5, t5, t4
  addi t4, t4, -1
  bne  t4, zero, loop

  # each branch below is taken once and skips an addi that would mark it wrong
  li   t6, 0
  beq  t5, t5, 1f
  addi t6, t6, 1
1:
  blt  a1, a0, 2f
  addi t6, t6, 2
2:
  bge  a0, a1, 3f
  addi t6, t6, 4
3:
  bltu a0, a1, 4f
  addi t6, t6, 8
4:
  bgeu a0, a1, 5f
  addi t6, t6, 16
5:
  # not taken
  beq  a0, a1, 6f
  addi t6, t6, 32
6:
  jal  ra, sub1
  la   t0, sub2
  jalr ra, 0(t0)
  HALT

sub1:
  addi s0, a0, 1
  ret
sub2:
  addi s1, a1, 1
  ret
