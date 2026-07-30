// Minimal ibex_pkg for the ibex_alu verification slice.
// Contains exactly the two enums ibex_alu references (alu_op_e, rv32b_e), with
// the real lowRISC member order/encoding. ibex_alu compares operator_i by NAME
// only, so this is behaviourally identical to the full package for the ALU.
// (Vendored subset — the corpus RTL is never modified.)
package ibex_pkg;

  typedef enum integer {
    RV32BNone       = 0,
    RV32BBalanced   = 1,
    RV32BOTEarlGrey = 2,
    RV32BFull       = 3
  } rv32b_e;

  typedef enum logic [6:0] {
    ALU_ADD,  ALU_SUB,
    ALU_XOR,  ALU_OR,   ALU_AND,
    ALU_XNOR, ALU_ORN,  ALU_ANDN,
    ALU_SRA,  ALU_SRL,  ALU_SLL,
    ALU_SRO,  ALU_SLO,  ALU_ROR,  ALU_ROL,
    ALU_GREV, ALU_GORC, ALU_SHFL, ALU_UNSHFL,
    ALU_XPERM_N, ALU_XPERM_B, ALU_XPERM_H,
    ALU_SH1ADD, ALU_SH2ADD, ALU_SH3ADD,
    ALU_LT,   ALU_LTU,  ALU_GE,   ALU_GEU,  ALU_EQ,  ALU_NE,
    ALU_MIN,  ALU_MINU, ALU_MAX,  ALU_MAXU,
    ALU_PACK, ALU_PACKU, ALU_PACKH,
    ALU_SEXTB, ALU_SEXTH,
    ALU_CLZ,  ALU_CTZ,  ALU_CPOP,
    ALU_SLT,  ALU_SLTU,
    ALU_CMOV, ALU_CMIX, ALU_FSL,  ALU_FSR,
    ALU_BSET, ALU_BCLR, ALU_BINV, ALU_BEXT,
    ALU_BCOMPRESS, ALU_BDECOMPRESS,
    ALU_BFP,
    ALU_CLMUL, ALU_CLMULR, ALU_CLMULH,
    ALU_CRC32_B, ALU_CRC32C_B,
    ALU_CRC32_H, ALU_CRC32C_H,
    ALU_CRC32_W, ALU_CRC32C_W
  } alu_op_e;

endpackage
