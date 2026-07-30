// Formal harness for the REAL ibex_alu (RV32BNone). Free inputs op/a/b, an
// independent RV32I golden, and per-op-class assertions selected by a define
// (CLASS_ADD | CLASS_LOGIC | CLASS_SHIFT | CLASS_CMP). The DUT module is chosen
// by the DUT macro so the same harness proves the clean core or catches the
// mutant. Golden matches Ibex's result_o mux and comparator exactly:
//   result_o(ADD)=a+b, (SUB)=a-b, (XOR/OR/AND)=bitwise,
//   (SLL/SRL/SRA)=shift by b[4:0], (compare ops)={31'0, cmp_result};
//   comparison_result_o per ALU_EQ/NE/LT/LTU/GE/GEU(/SLT/SLTU);
//   is_equal_result_o = (a==b).
`ifndef DUT
`define DUT ibex_alu
`endif

module ibex_alu_fv (
    input logic        clk,
    input ibex_pkg::alu_op_e op,
    input logic [31:0] a,
    input logic [31:0] b
);
  import ibex_pkg::*;

  logic [31:0] imd_q [2];
  logic [31:0] imd_d [2];
  logic [1:0]  imd_we;
  logic [31:0] result;
  logic        cmp, is_eq;
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

  // -------- golden references (RV32I) --------
  logic [31:0] g_res;
  logic        g_cmp;

`ifdef CLASS_ADD
  always_comb g_res = (op == ALU_ADD) ? (a + b) : (a - b);
  always @(posedge clk) begin
    assume (op == ALU_ADD || op == ALU_SUB);
    assert (result == g_res);
  end
`endif

`ifdef CLASS_LOGIC
  always_comb g_res = (op == ALU_XOR) ? (a ^ b) :
                      (op == ALU_OR)  ? (a | b) : (a & b);
  always @(posedge clk) begin
    assume (op == ALU_XOR || op == ALU_OR || op == ALU_AND);
    assert (result == g_res);
  end
`endif

`ifdef CLASS_SHIFT
  always_comb g_res = (op == ALU_SLL) ? (a << b[4:0]) :
                      (op == ALU_SRL) ? (a >> b[4:0]) :
                                        ($signed(a) >>> b[4:0]);
  always @(posedge clk) begin
    assume (op == ALU_SLL || op == ALU_SRL || op == ALU_SRA);
    assert (result == g_res);
  end
`endif

`ifdef CLASS_CMP
  always_comb begin
    unique case (op)
      ALU_EQ:            g_cmp = (a == b);
      ALU_NE:            g_cmp = (a != b);
      ALU_LT,  ALU_SLT:  g_cmp = ($signed(a) <  $signed(b));
      ALU_LTU, ALU_SLTU: g_cmp = (a <  b);
      ALU_GE:            g_cmp = ($signed(a) >= $signed(b));
      ALU_GEU:           g_cmp = (a >= b);
      default:           g_cmp = (a == b);
    endcase
  end
  always @(posedge clk) begin
    assume (op == ALU_EQ  || op == ALU_NE  || op == ALU_LT  || op == ALU_LTU ||
            op == ALU_GE  || op == ALU_GEU || op == ALU_SLT || op == ALU_SLTU);
    assert (cmp   == g_cmp);
    assert (is_eq == (a == b));
    assert (result == {31'h0, g_cmp});
  end
`endif

endmodule
