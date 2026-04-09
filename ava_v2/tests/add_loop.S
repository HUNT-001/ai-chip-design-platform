# tests/add_loop.S — RV32IM end-to-end test (add loop + M-ext corner cases).
# Assemble:
#   riscv32-unknown-elf-gcc -march=rv32im -mabi=ilp32 -nostdlib \
#       -T tests/link.ld tests/add_loop.S -o tests/add_loop.elf
.section .text.startup
.globl _start
_start:
    lui   sp, %hi(0x80400000)
    addi  sp, sp, %lo(0x80400000)

    # 1. ADD loop: sum 1..10 = 55
    li    t0, 0
    li    t1, 1
    li    t2, 10
loop:
    add   t0, t0, t1
    addi  t1, t1, 1
    ble   t1, t2, loop
    mv    a0, t0           # a0 = 55

    # 2. Store + load
    lui   t3, %hi(0x80001010)
    addi  t3, t3, %lo(0x80001010)
    sw    a0, 0(t3)
    lw    a1, 0(t3)

    # 3. M-extension
    li    t0, 10
    li    t1, 11
    mul   a2, t0, t1       # 110
    li    t1, 2
    div   a3, a2, t1       # 55
    li    t1, 3
    rem   a4, a2, t1       # 2
    li    t0, -1; li t1, -1
    mulh  a5, t0, t1       # 0

    # divide-by-zero
    li    t0, 5; li t1, 0
    div   a6, t0, t1       # 0xFFFFFFFF
    divu  t4, t0, t1       # 0xFFFFFFFF
    rem   t5, t0, t1       # 5
    remu  s0, t0, t1       # 5

    # signed overflow
    li    t0, 0x80000000; li t1, -1
    div   a7, t0, t1       # 0x80000000
    rem   s1, t0, t1       # 0

    # 4. Byte / halfword
    li    t0, 0xABCD1234
    sw    t0, 4(t3)
    lb    t1, 4(t3)        # sign-ext 0x34
    lbu   t2, 4(t3)        # zero-ext 0x34
    lh    s2, 4(t3)        # sign-ext 0x1234
    lhu   s3, 4(t3)        # zero-ext 0x1234

    # 5. Signal pass via TOHOST
    li    a0, 1
    lui   t3, %hi(0x80001000)
    addi  t3, t3, %lo(0x80001000)
    sw    a0, 0(t3)
spin: j spin

.section .data
.align 4
scratch: .word 0
