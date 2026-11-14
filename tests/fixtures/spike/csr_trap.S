# tests/fixtures/spike/csr_trap.S
# ===============================
# Bare-metal RV32IM program whose only purpose is to make Spike's
# `--log-commits` output show CSR writes, a store, and a trap, so that
# csr_trap.log (recorded from it, see PROVENANCE.md) holds real examples of
# each line shape.
#
#   - CSR writes: mtvec, mscratch, mstatus (set and clear)
#   - a store
#   - ECALL into a direct-mode mtvec handler, which reads mcause / mepc,
#     writes mepc past the ecall and returns with MRET
#   - exit through HTIF tohost (1 = pass, 3 = fail)
#
# Build and record: see PROVENANCE.md in this directory.

    .section .tohost, "aw", @progbits
    .globl tohost
    .globl fromhost
    .align 6
tohost:   .dword 0
fromhost: .dword 0

    .section .text
    .globl _start
    .align 2
_start:
    la      t0, trap_handler
    csrw    mtvec, t0           # CSR write: mtvec = handler (direct mode)
    li      t1, 0x5a5a
    csrw    mscratch, t1        # CSR write: mscratch
    csrr    t2, mscratch
    bne     t2, t1, _fail
    li      t3, 0x8
    csrs    mstatus, t3         # CSR write: set mstatus.MIE
    csrc    mstatus, t3         # CSR write: clear it again
    la      a0, _data_start
    sw      t1, 0(a0)           # store
    li      s1, 0
    ecall                       # trap -> trap_handler
    li      t5, 11              # mcause for ECALL from M-mode
    bne     s1, t5, _fail

_pass:
    li      a0, 1               # tohost = 1 -> Spike exits 0
    la      t0, tohost
    sw      a0, 0(t0)
1:  j       1b

_fail:
    li      a0, 3               # tohost = 3 -> Spike exits non-zero
    la      t0, tohost
    sw      a0, 0(t0)
1:  j       1b

    .align 2
trap_handler:
    csrr    s1, mcause
    csrr    s2, mepc
    addi    s2, s2, 4
    csrw    mepc, s2            # CSR write: resume after the ecall
    mret

    .section .data
    .align 4
_data_start:
    .word 0
