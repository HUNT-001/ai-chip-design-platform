// Formal harness for the Phase-3 ALU DUT.
// Inputs op/a/b are left as free top-level inputs, so the SMT engine reasons
// over the ENTIRE input space symbolically. `g` is an independent bug-free
// golden model; the assertion demands the DUT equal it for every input. For
// this combinational equivalence a single BMC step is an exhaustive proof.
module alu_fv #(
    parameter bit INJECT_BUG = 1'b0
) (
    input logic        clk,
    input logic [2:0]  op,
    input logic [31:0] a,
    input logic [31:0] b
);
  logic [31:0] y, g;

  alu #(.INJECT_BUG(INJECT_BUG)) dut (
      .op_i(op), .a_i(a), .b_i(b), .y_o(y)
  );

  // Independent golden reference (never carries the defect).
  always_comb begin
    unique case (op)
      3'd0:    g = a + b;
      3'd1:    g = a - b;
      3'd2:    g = a & b;
      3'd3:    g = a | b;
      3'd4:    g = a ^ b;
      3'd5:    g = {31'b0, ($signed(a) < $signed(b))};
      3'd6:    g = {31'b0, (a < b)};
      default: g = a;
    endcase
  end

  // The property under proof: golden-equivalence of the ALU result.
  always @(posedge clk) assert (y == g);
endmodule
