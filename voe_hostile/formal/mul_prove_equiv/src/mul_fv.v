// Plain-Verilog formal harness for the FORMAL-HOSTILE multiplier board.
// Read DIRECTLY by yosys — never through sv2v, which strips assert/assume.
//
// The point of this board is to invert the economics of every previous one.
// Until now formal was the cheap decisive answer and simulation was the
// expensive way to learn nothing. Here:
//
//   CLASS_EQUIV   behavioural a*b vs a 32-step SHIFT-AND-ADD implementation.
//                 The property is TRUE, and proving it forces the solver to
//                 reason about multiplication structurally. This is expected to
//                 exhaust the time budget: no verdict, budget spent.
//
//   CLASS_BUG     the mutated multiplier corrupts the product whenever
//                 srcB[3:0]==0xF. Random simulation hits that in ~16 vectors.
//
// So a policy that always reaches for proof should burn its budget here, and a
// policy that samples should close the bug obligation cheaply. If the adaptive
// policy does NOT notice and re-allocate, that is the failure this experiment
// exists to find.
`ifndef DUT
`define DUT mul_wrap
`endif

module mul_fv (
    input wire [31:0] a,
    input wire [31:0] b
);
  wire [63:0] y;

  `DUT dut (.a(a), .b(b), .y(y));

// Structurally independent reference: 32 shift-and-add partial products.
// Deliberately NOT written as `a*b` — an identical formulation would let the
// solver discharge it by syntactic identity and the board would not be hostile.
//
// SIGNED. The RSD Multiplier declares srcA/srcB as `logic signed`, so it does a
// two's-complement multiply. An unsigned accumulation disagrees with it the
// moment a high bit is set — which is exactly what the first version of this
// harness did, and the solver refuted it in seconds instead of timing out. The
// board measured nothing until this was fixed. Two's complement shift-add adds
// the first 31 partial products and SUBTRACTS the one weighted by b[31].
`define SEXT_A {{32{a[31]}}, a}

`ifdef CLASS_EQUIV
  reg [63:0] acc;
  integer i;
  always @(*) begin
    acc = 64'd0;
    for (i = 0; i < 31; i = i + 1) begin
      if (b[i]) acc = acc + (`SEXT_A << i);
    end
    if (b[31]) acc = acc - (`SEXT_A << 31);
  end
  always @(*) assert (y == acc);
`endif

`ifdef CLASS_BUG
  // Same reference, applied to the mutated DUT. A counterexample exists and is
  // dense in the input space (1 in 16), so simulation finds it fast.
  reg [63:0] acc2;
  integer j;
  always @(*) begin
    acc2 = 64'd0;
    for (j = 0; j < 31; j = j + 1) begin
      if (b[j]) acc2 = acc2 + (`SEXT_A << j);
    end
    if (b[31]) acc2 = acc2 - (`SEXT_A << 31);
  end
  always @(*) assert (y == acc2);
`endif

endmodule
