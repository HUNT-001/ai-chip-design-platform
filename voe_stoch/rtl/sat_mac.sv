// Saturating signed multiply-accumulate — the arithmetic core of almost every
// DSP and AI datapath, and the home of one of hardware's most famous corner
// cases.
//
// WHY THIS DUT. The corpus needed a STOCHASTIC benchmark: a design whose bug
// random stimulus sometimes finds and sometimes misses. Experiment 15 showed
// the existing benches are all effectively deterministic classifiers — true
// properties pass on every seed, mutants are caught on every seed — so no
// experiment about seed sensitivity, adaptive sampling or posterior learning
// could be evaluated on them at all.
//
// The rarity here is NOT dialled in to produce convenient statistics. It comes
// from the design: in two's complement, (-128) * (-128) = +16384 is the single
// asymmetric corner of an 8x8 signed multiply, because -128 has no positive
// counterpart. That is one operand pair out of 65536 — genuinely rare under
// uniform random stimulus, and a real bug class in fixed-point hardware.
//
// The stimulus count stays at the project's existing default (20000 vectors).
// Tuning THAT to manufacture variance was considered and refused; the rarity
// belongs to the DUT, not to the measuring apparatus.
module sat_mac #(
    parameter int unsigned W   = 8,    // operand width
    parameter int unsigned ACC = 16    // accumulator width
)(
    input  logic                 clk_i,
    input  logic                 rst_ni,
    input  logic                 en_i,
    input  logic                 clr_i,
    input  logic signed [W-1:0]  a_i,
    input  logic signed [W-1:0]  b_i,
    output logic signed [ACC-1:0] acc_o
);
  localparam signed [ACC:0] ACC_MAX = (1 <<< (ACC - 1)) - 1;   //  32767
  localparam signed [ACC:0] ACC_MIN = -(1 <<< (ACC - 1));      // -32768

  logic signed [ACC-1:0] acc_q;
  logic signed [2*W-1:0] prod;
  logic signed [ACC:0]   sum;      // one extra bit, so overflow is visible

  assign acc_o = acc_q;
  assign prod  = a_i * b_i;
  assign sum   = $signed({acc_q[ACC-1], acc_q}) + $signed(prod);

  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (~rst_ni) begin
      acc_q <= '0;
    end else if (clr_i) begin
      acc_q <= '0;
    end else if (en_i) begin
      if (sum > ACC_MAX)      acc_q <= ACC_MAX[ACC-1:0];
      else if (sum < ACC_MIN) acc_q <= ACC_MIN[ACC-1:0];
      else                    acc_q <= sum[ACC-1:0];
    end
  end
endmodule
