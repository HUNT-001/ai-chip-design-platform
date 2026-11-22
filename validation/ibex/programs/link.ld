/* Memory map of Ibex simple_system: 1 MB RAM at 0x00100000, boot_addr
 * 0x00100000 (exception vectors), first fetch at boot_addr + 0x80.
 * SimCtrl sits at 0x00020000; a write of 1 to 0x00020008 halts the sim.
 * Spike uses the same word as `tohost`, so one ELF stops both. */
OUTPUT_ARCH(riscv)
ENTRY(_start)

MEMORY
{
  ram (rwx) : ORIGIN = 0x00100000, LENGTH = 0x30000
}

SECTIONS
{
  .vectors 0x00100000 : { KEEP(*(.vectors)) } > ram
  .text    0x00100080 : { *(.text.start) *(.text*) } > ram
  .data    0x00110000 : { *(.data*) *(.rodata*) *(.bss*) } > ram
}

tohost   = 0x00020008;
fromhost = 0x00020010;
