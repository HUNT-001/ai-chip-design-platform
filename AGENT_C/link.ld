/* tests/asm/link.ld
 * Bare-metal linker script for RV32IM Spike smoke tests.
 * Memory layout matches Spike's default bare-metal load address.
 *
 * Usage:
 *   riscv32-unknown-elf-gcc -march=rv32im -mabi=ilp32 \
 *       -nostdlib -static -T tests/asm/link.ld \
 *       tests/asm/rv32im_smoke.S -o tests/asm/rv32im_smoke.elf
 */

OUTPUT_ARCH(riscv)
ENTRY(_start)

MEMORY {
    /* Spike bare-metal default RAM base */
    RAM (rwx) : ORIGIN = 0x80000000, LENGTH = 64M
}

SECTIONS {
    /* ── HTIF tohost/fromhost must be in a named section ────────────────── */
    .tohost 0x80001000 : {
        KEEP(*(.tohost))
    } > RAM

    /* ── Code starts at 0x80000000 ─────────────────────────────────────── */
    .text 0x80000000 : {
        *(.text.startup)
        *(.text)
        *(.text.*)
    } > RAM

    .rodata : {
        *(.rodata)
        *(.rodata.*)
    } > RAM

    .data : {
        *(.data)
        *(.data.*)
        *(.sdata)
        *(.sdata.*)
    } > RAM

    .bss : {
        __bss_start = .;
        *(.bss)
        *(.bss.*)
        *(.sbss)
        *(.sbss.*)
        *(COMMON)
        __bss_end = .;
    } > RAM

    /* Stack: grow down from end of RAM segment */
    __stack_top = ORIGIN(RAM) + LENGTH(RAM) - 4;
}
