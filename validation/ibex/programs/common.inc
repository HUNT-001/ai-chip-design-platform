# Shared prologue/epilogue for the Ibex validation programs.
#
# VECTORS fills the 32 exception/interrupt slots at boot_addr with a spin, so
# an unexpected trap stalls both simulators at a recognisable PC instead of
# running off into data.  None of these programs is meant to trap.
#
# HALT writes 1 to the SimCtrl/tohost word and then spins on `j .`.  Ibex's
# SimCtrl stops the simulation on that store; Spike sees tohost = 1 and exits 0
# at its next HTIF poll, retiring the `j .` until then.

.macro VECTORS
  .section .vectors, "ax"
  .option norvc
  .rept 32
  j .
  .endr
.endm

.macro HALT
  li   t0, 0x00020008
  li   t1, 1
  sw   t1, 0(t0)
1:
  j    1b
.endm
