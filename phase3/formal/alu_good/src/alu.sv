// Phase-3 evidence DUT: a small, self-contained, synthesizable ALU.
// Real RTL — no package dependencies — so a first evidence slice runs with
// zero external wiring. INJECT_BUG plants a narrow, input-specific defect so
// BOTH outcomes are demonstrable through real tools:
//   * formal (sby) proves the golden-equivalence property or returns a
//     concrete counterexample,
//   * simulation (Verilator) accumulates inductive pass evidence.
// The bug fires only on operand_a_i == 32'hDEAD_BEEF during an ADD, so random
// simulation essentially never hits it but formal finds it immediately — the
// live demonstration of Sem-1 (formal dominance) that the VSA kernel encodes.
//
// op encoding:  0=ADD 1=SUB 2=AND 3=OR 4=XOR 5=SLT(signed) 6=SLTU 7=passthrough
module alu #(
    parameter bit INJECT_BUG = 1'b0
) (
    input  logic [2:0]  op_i,
    input  logic [31:0] a_i,
    input  logic [31:0] b_i,
    output logic [31:0] y_o
);
  logic [31:0] add_r;

  always_comb begin
    // The only line that differs when the defect is present:
    if (INJECT_BUG && a_i == 32'hDEAD_BEEF)
      add_r = a_i + b_i + 32'd1;   // off-by-one on a single input value
    else
      add_r = a_i + b_i;

    unique case (op_i)
      3'd0:    y_o = add_r;
      3'd1:    y_o = a_i - b_i;
      3'd2:    y_o = a_i & b_i;
      3'd3:    y_o = a_i | b_i;
      3'd4:    y_o = a_i ^ b_i;
      3'd5:    y_o = {31'b0, ($signed(a_i) < $signed(b_i))};
      3'd6:    y_o = {31'b0, (a_i < b_i)};
      default: y_o = a_i;
    endcase
  end
endmodule
