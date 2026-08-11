// FORMAL-HOSTILE DUT. The multiplier core is the `Multiplier` module from
// corpus/rsd/Processor/Src/Primitives/Multiplier.sv, extracted verbatim (the
// original file also declares SignExtender and PipelinedMultiplier, and pulls
// in BasicMacros.sv, neither of which this experiment needs).
//
// Why this design: proving a behavioural `a*b` equivalent to a structurally
// different shift-and-add implementation forces the solver to reason about
// multiplication itself. Bit-blasting a 32x32 multiply is the classic case
// where formal stops being the cheap answer — which is exactly the regime this
// experiment needs, because every earlier board rewarded going straight to
// proof.
module Multiplier #(
    parameter BIT_WIDTH = 32
)(
    input  logic signed [   BIT_WIDTH-1:0 ] srcA,
    input  logic signed [   BIT_WIDTH-1:0 ] srcB,
    output logic signed [ 2*BIT_WIDTH-1: 0] dst
);
    assign dst = srcA * srcB;
endmodule

// Buggy variant: the low half of the product is corrupted whenever the low
// nibble of srcB is 0xF. Roughly one random vector in sixteen hits it, so
// simulation finds it almost immediately — while formal must still grind
// through the multiplier structure to say anything at all.
module Multiplier_mut #(
    parameter BIT_WIDTH = 32
)(
    input  logic signed [   BIT_WIDTH-1:0 ] srcA,
    input  logic signed [   BIT_WIDTH-1:0 ] srcB,
    output logic signed [ 2*BIT_WIDTH-1: 0] dst
);
    logic signed [ 2*BIT_WIDTH-1:0 ] raw;
    assign raw = srcA * srcB;
    assign dst = (srcB[3:0] == 4'hF) ? (raw ^ 64'h1000) : raw;
endmodule

// 32-bit wrappers so sv2v emits concrete widths.
module mul_wrap (
    input  logic [31:0] a,
    input  logic [31:0] b,
    output logic [63:0] y
);
  Multiplier #(.BIT_WIDTH(32)) u_mul (.srcA(a), .srcB(b), .dst(y));
endmodule

module mul_wrap_mut (
    input  logic [31:0] a,
    input  logic [31:0] b,
    output logic [63:0] y
);
  Multiplier_mut #(.BIT_WIDTH(32)) u_mul (.srcA(a), .srcB(b), .dst(y));
endmodule
