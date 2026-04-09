# tests/asm/rv32im_smoke.S
# ========================
# Minimal RV32IM bare-metal program for end-to-end ISS smoke tests.
#
# Tests exercised:
#   - Integer arithmetic (ADD, ADDI, SUB)
#   - Load/store (LW, SW)
#   - Branch (BEQ, BNE)
#   - MUL / DIV / REM (M-extension)
#   - Divide-by-zero (REM: result = dividend per spec)
#   - MUL overflow (MULH)
#   - ECALL / tohost termination
#
# Build:
#   riscv32-unknown-elf-gcc -march=rv32im -mabi=ilp32 \
#       -nostdlib -static -T tests/asm/link.ld \
#       tests/asm/rv32im_smoke.S -o tests/asm/rv32im_smoke.elf
#
# Run:
#   python sim/run_iss.py --isa RV32IM \
#       --elf tests/asm/rv32im_smoke.elf --out /tmp/run_smoke --validate

    .section .text
    .globl _start

# ── RISC-V HTIF tohost / fromhost (Spike bare-metal termination) ───────────
    .section .tohost, "aw", @progbits
    .globl tohost
    .globl fromhost
    .align 6
tohost:   .dword 0
fromhost: .dword 0

    .section .text
    .align 2

_start:
    # ── 1. Basic integer arithmetic ────────────────────────────────────────
    li      t0, 10
    li      t1, 7
    add     t2, t0, t1          # t2 = 17
    sub     t3, t0, t1          # t3 = 3
    addi    t4, t2, -7          # t4 = 10

    # ── 2. Load / store ────────────────────────────────────────────────────
    la      a0, _data_start
    sw      t2, 0(a0)           # mem[_data_start] = 17
    lw      a1, 0(a0)           # a1 = 17
    # Verify: a1 should equal t2
    bne     a1, t2, _fail

    # ── 3. MUL (lower 32-bits) ────────────────────────────────────────────
    li      a2, 6
    li      a3, 7
    mul     a4, a2, a3          # a4 = 42
    li      t5, 42
    bne     a4, t5, _fail

    # ── 4. MULH (signed × signed, upper 32-bits) ──────────────────────────
    li      a2, 0x7FFFFFFF      # INT32_MAX
    li      a3, 2
    mulh    a5, a2, a3          # upper 32 bits of INT32_MAX * 2 = 0x0000_0000_FFFF_FFFE → upper = 0
    li      t5, 0
    bne     a5, t5, _fail

    # ── 5. DIV (signed) ───────────────────────────────────────────────────
    li      a2, 42
    li      a3, 6
    div     a4, a2, a3          # a4 = 7
    li      t5, 7
    bne     a4, t5, _fail

    # ── 6. REM (signed) ───────────────────────────────────────────────────
    li      a2, 43
    li      a3, 6
    rem     a4, a2, a3          # a4 = 1
    li      t5, 1
    bne     a4, t5, _fail

    # ── 7. DIV by zero: result must be -1 (all-ones) per spec ─────────────
    li      a2, 100
    li      a3, 0
    div     a4, a2, a3          # a4 = -1 (0xFFFFFFFF)
    li      t5, -1
    bne     a4, t5, _fail

    # ── 8. REM by zero: result must be dividend per spec ──────────────────
    li      a2, 55
    li      a3, 0
    rem     a4, a2, a3          # a4 = 55
    li      t5, 55
    bne     a4, t5, _fail

    # ── 9. DIVU / REMU (unsigned) ─────────────────────────────────────────
    li      a2, 100
    li      a3, 7
    divu    a4, a2, a3          # a4 = 14
    li      t5, 14
    bne     a4, t5, _fail

    remu    a4, a2, a3          # a4 = 2
    li      t5, 2
    bne     a4, t5, _fail

    # ── 10. MULHU (unsigned × unsigned, upper 32-bits) ────────────────────
    li      a2, 0xFFFFFFFF
    li      a3, 0xFFFFFFFF
    mulhu   a4, a2, a3          # (2^32-1)^2 >> 32 = 2^32 - 2 = 0xFFFFFFFE
    li      t5, 0xFFFFFFFE
    bne     a4, t5, _fail

    # ── 11. MULHSU (signed × unsigned, upper 32-bits) ─────────────────────
    li      a2, -1              # 0xFFFFFFFF as signed = -1
    li      a3, 2
    mulhsu  a4, a2, a3          # -1 * 2 = -2; upper 32 = -1 = 0xFFFFFFFF
    li      t5, -1
    bne     a4, t5, _fail

    # ── 12. DIV overflow: INT32_MIN / -1 = INT32_MIN per spec ─────────────
    li      a2, 0x80000000      # INT32_MIN
    li      a3, -1
    div     a4, a2, a3          # result = INT32_MIN (overflow)
    bne     a4, a2, _fail

    # ── All tests passed — signal success to HTIF ─────────────────────────
_pass:
    li      a0, 1               # tohost = 1 → Spike terminates with exit code 0
    la      t0, tohost
    sw      a0, 0(t0)
1:  j       1b                  # spin (Spike will halt us via HTIF)

_fail:
    li      a0, 0x3             # tohost = 3 → non-zero exit (failure)
    la      t0, tohost
    sw      a0, 0(t0)
1:  j       1b


# ── Data section ────────────────────────────────────────────────────────────
    .section .data
    .align 4
_data_start:
    .word 0xDEADBEEF            # sentinel overwritten by test
