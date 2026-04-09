/* tests/link.ld  — minimal linker script, loads at 0x80000000 */
OUTPUT_ARCH(riscv)
ENTRY(_start)

MEMORY {
    RAM (rwx) : ORIGIN = 0x80000000, LENGTH = 4M
}

SECTIONS {
    . = 0x80000000;

    .text : {
        *(.text.startup)
        *(.text .text.*)
    } > RAM

    .rodata : { *(.rodata .rodata.*) } > RAM
    .data   : { *(.data   .data.*)   } > RAM
    .bss    : {
        __bss_start = .;
        *(.bss .bss.*)
        *(COMMON)
        __bss_end = .;
    } > RAM

    . = ALIGN(4);
    __stack_top = ORIGIN(RAM) + LENGTH(RAM) - 64;
}
