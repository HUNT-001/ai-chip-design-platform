// Plain-Verilog formal harness for the sv2v-converted ibex_alu.
// Read DIRECTLY by yosys (NOT through sv2v) so the assert/assume survive — the
// earlier all-sv2v flow silently stripped them, which the bug_logic self-check
// caught. No package/enum here: opcodes are numeric localparams whose values
// match sv2v's enum encoding (declaration order, verified against the DUT).
// `DUT selects the clean core or the mutant.
`ifndef DUT
`define DUT ibex_alu
`endif

module ibex_alu_fv (
    input wire        clk,
    input wire [6:0]  op,
    input wire [31:0] a,
    input wire [31:0] b
);
  localparam [6:0] ALU_ADD = 7'd0,  ALU_SUB = 7'd1,  ALU_XOR = 7'd2,
                   ALU_OR  = 7'd3,  ALU_AND = 7'd4,
                   ALU_SRA = 7'd8,  ALU_SRL = 7'd9,  ALU_SLL = 7'd10,
                   ALU_LT  = 7'd25, ALU_LTU = 7'd26, ALU_GE  = 7'd27,
                   ALU_GEU = 7'd28, ALU_EQ  = 7'd29, ALU_NE  = 7'd30,
                   ALU_SLT = 7'd43, ALU_SLTU = 7'd44;

  wire [31:0] result;
  wire        cmp, is_eq;

  `DUT dut (
      .operator_i          (op),
      .operand_a_i         (a),
      .operand_b_i         (b),
      .instr_first_cycle_i (1'b1),
      .multdiv_operand_a_i (33'b0),
      .multdiv_operand_b_i (33'b0),
      .multdiv_sel_i       (1'b0),
      .imd_val_q_i         (64'b0),
      .imd_val_d_o         (),
      .imd_val_we_o        (),
      .adder_result_o      (),
      .adder_result_ext_o  (),
      .result_o            (result),
      .comparison_result_o (cmp),
      .is_equal_result_o   (is_eq)
  );

  reg [31:0] g_res;
  reg        g_cmp;

`ifdef CLASS_ADD
  always @(*) g_res = (op == ALU_ADD) ? (a + b) : (a - b);
  always @(posedge clk) begin
    assume (op == ALU_ADD || op == ALU_SUB);
    assert (result == g_res);
  end
`endif

`ifdef CLASS_LOGIC
  always @(*) g_res = (op == ALU_XOR) ? (a ^ b) :
                      (op == ALU_OR)  ? (a | b) : (a & b);
  always @(posedge clk) begin
    assume (op == ALU_XOR || op == ALU_OR || op == ALU_AND);
    assert (result == g_res);
  end
`endif

`ifdef CLASS_SHIFT
  // NOTE (real bug found by formal): this MUST be if/else, not a ternary chain.
  // In a Verilog conditional expression, if any operand is unsigned the whole
  // expression is unsigned and that propagates into the branches — which
  // silently demoted `$signed(a) >>> amt` to a LOGICAL shift. Formal caught it
  // with SRA, a=0xFFFFFFFF, amt=16: Ibex 0xFFFFFFFF vs golden 0x0000FFFF.
  // Separate assignments keep each RHS's own signedness.
  always @(*) begin
    if      (op == ALU_SLL) g_res = a << b[4:0];
    else if (op == ALU_SRL) g_res = a >> b[4:0];
    else                    g_res = $signed(a) >>> b[4:0];
  end
  always @(posedge clk) begin
    assume (op == ALU_SLL || op == ALU_SRL || op == ALU_SRA);
    assert (result == g_res);
  end
`endif

`ifdef CLASS_CMP
  always @(*) begin
    case (op)
      ALU_EQ:   g_cmp = (a == b);
      ALU_NE:   g_cmp = (a != b);
      ALU_LT:   g_cmp = ($signed(a) <  $signed(b));
      ALU_SLT:  g_cmp = ($signed(a) <  $signed(b));
      ALU_LTU:  g_cmp = (a <  b);
      ALU_SLTU: g_cmp = (a <  b);
      ALU_GE:   g_cmp = ($signed(a) >= $signed(b));
      ALU_GEU:  g_cmp = (a >= b);
      default:  g_cmp = (a == b);
    endcase
  end
  always @(posedge clk) begin
    assume (op == ALU_EQ  || op == ALU_NE  || op == ALU_LT  || op == ALU_LTU ||
            op == ALU_GE  || op == ALU_GEU || op == ALU_SLT || op == ALU_SLTU);
    assert (cmp    == g_cmp);
    assert (is_eq  == (a == b));
    assert (result == {31'h0, g_cmp});
  end
`endif

endmodule
