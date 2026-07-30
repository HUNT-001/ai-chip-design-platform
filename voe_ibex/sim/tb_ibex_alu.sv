// Self-checking testbench (for Verilator) for the REAL ibex_alu (RV32BNone).
// Random RV32I base-op vectors vs an independent golden; prints one SIM_RESULT
// line. `DUT selects the clean core or the mutant.
`ifndef DUT
`define DUT ibex_alu
`endif
`ifndef NVEC
`define NVEC 20000
`endif

module tb_ibex_alu;
  import ibex_pkg::*;

  alu_op_e     op;
  logic [31:0] a, b, result, g_res;
  logic        cmp, is_eq;
  int          fails = 0;

  logic [31:0] imd_q [2];
  logic [31:0] imd_d [2];
  logic [1:0]  imd_we;
  assign imd_q[0] = '0;
  assign imd_q[1] = '0;

  `DUT #(.RV32B(RV32BNone)) dut (
      .operator_i          (op),
      .operand_a_i         (a),
      .operand_b_i         (b),
      .instr_first_cycle_i (1'b1),
      .multdiv_operand_a_i (33'b0),
      .multdiv_operand_b_i (33'b0),
      .multdiv_sel_i       (1'b0),
      .imd_val_q_i         (imd_q),
      .imd_val_d_o         (imd_d),
      .imd_val_we_o        (imd_we),
      .adder_result_o      (),
      .adder_result_ext_o  (),
      .result_o            (result),
      .comparison_result_o (cmp),
      .is_equal_result_o   (is_eq)
  );

  function automatic logic [31:0] golden_res(input alu_op_e o,
                                             input logic [31:0] x,
                                             input logic [31:0] z);
    unique case (o)
      ALU_ADD:  golden_res = x + z;
      ALU_SUB:  golden_res = x - z;
      ALU_XOR:  golden_res = x ^ z;
      ALU_OR:   golden_res = x | z;
      ALU_AND:  golden_res = x & z;
      ALU_SLL:  golden_res = x << z[4:0];
      ALU_SRL:  golden_res = x >> z[4:0];
      ALU_SRA:  golden_res = $signed(x) >>> z[4:0];
      ALU_SLT:  golden_res = {31'h0, ($signed(x) <  $signed(z))};
      ALU_SLTU: golden_res = {31'h0, (x <  z)};
      ALU_EQ:   golden_res = {31'h0, (x == z)};
      ALU_NE:   golden_res = {31'h0, (x != z)};
      ALU_LT:   golden_res = {31'h0, ($signed(x) <  $signed(z))};
      ALU_LTU:  golden_res = {31'h0, (x <  z)};
      ALU_GE:   golden_res = {31'h0, ($signed(x) >= $signed(z))};
      ALU_GEU:  golden_res = {31'h0, (x >= z)};
      default:  golden_res = x;
    endcase
  endfunction

  initial begin
    alu_op_e base_ops [16];
    base_ops = '{ALU_ADD, ALU_SUB, ALU_XOR, ALU_OR, ALU_AND, ALU_SLL, ALU_SRL,
                 ALU_SRA, ALU_SLT, ALU_SLTU, ALU_EQ, ALU_NE, ALU_LT, ALU_LTU,
                 ALU_GE, ALU_GEU};
    for (int i = 0; i < `NVEC; i++) begin
      op = base_ops[$urandom() % 16];
      a  = $urandom();
      b  = $urandom();
      #1;
      g_res = golden_res(op, a, b);
      if (result !== g_res) begin
        fails++;
        if (fails <= 5)
          $display("MISMATCH op=%0d a=%08x b=%08x r=%08x g=%08x", op, a, b, result, g_res);
      end
    end
    if (fails == 0) $display("SIM_RESULT PASS n=%0d fails=0", `NVEC);
    else            $display("SIM_RESULT FAIL n=%0d fails=%0d", `NVEC, fails);
    $finish;
  end
endmodule
