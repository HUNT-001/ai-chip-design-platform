// Constrained-random self-checking testbench for sat_mac.
//
// SEEDED, and the seed genuinely selects the stimulus — that is the whole point
// of this benchmark. Verilator does not support $urandom(seed), so the seed
// arrives via the +verilator+seed+ plusarg exactly as the other benches here do.
//
// The reference model is computed in FULL PRECISION in the testbench and then
// saturated, independently of how the DUT does it. It never reads DUT internals,
// so it cannot inherit the DUT's bug — the defect that made an earlier negative
// control silently vacuous.
`ifndef DUT
`define DUT satmac_wrap
`endif
`ifndef NCYC
`define NCYC 20000
`endif

module tb_satmac;
  logic clk = 1'b0, rst_n = 1'b0, en, clr;
  logic signed [7:0]  a, b;
  logic [31:0] r;
  logic signed [15:0] acc;
  longint ref_acc;
  int fails = 0, checked = 0;
  // STIMULUS-DISTRIBUTION CONTROL. P(detect) was measured at 0.450 while the
  // arithmetic predicts 1-(1-1/65536)^20000 = 0.263, and the 95% interval
  // excluded the prediction. Inferring the corner rate from the detection rate
  // assumes the stimulus is uniform; this COUNTS the corner directly, so the
  // assumption is checked rather than trusted.
  int corner_hits = 0;
  // MARGINALS, so a joint-rate anomaly can be attributed. The paired
  // measurement put the corner at 1 in 32000 where uniform independent draws
  // predict 1 in 65536 (25 events vs 12.2 expected, ~2.6 sigma). Either a
  // marginal is not 1/256, or the two draws are correlated. Counting all three
  // separates those; inferring which from the joint rate alone cannot.
  int a_hits = 0, b_hits = 0;

  always #5 clk = ~clk;

  `DUT dut (.clk_i(clk), .rst_ni(rst_n), .en_i(en), .clr_i(clr),
            .a_i(a), .b_i(b), .acc_o(acc));

  function automatic longint sat16(input longint v);
    if (v > 32767)  return 32767;
    if (v < -32768) return -32768;
    return v;
  endfunction

  initial begin
    en = 0; clr = 0; a = 0; b = 0;
    repeat (2) @(posedge clk);
    rst_n = 1'b1;
    ref_acc = 0;
    @(posedge clk);

    for (int i = 0; i < `NCYC; i++) begin
      // Two CONSECUTIVE $urandom() calls are not independent in Verilator.
      // Measured: marginals exactly uniform (P(a==-128) = P(b==-128) = 1/256),
      // but the joint corner appeared 25 times where independence predicts
      // 12.2 — a 2x excess at ~2.6 sigma. The marginals alone could never have
      // revealed that; only counting the joint did.
      //
      // Drawing both operands from widely separated bit lanes of ONE draw.
      // Whether THIS is independent is not assumed either — characterise.py
      // re-measures the joint rate, and the benchmark documents whatever is
      // measured. The goal is a stimulus whose distribution is known, not a
      // particular detection probability.
      r   = $urandom();
      a   = r[7:0];
      b   = r[23:16];
      en  = 1'b1;
      clr = ($urandom() % 512) == 0;      // occasional clear, keeps acc moving

      if (a == -128) a_hits++;
      if (b == -128) b_hits++;
      if (a == -128 && b == -128) corner_hits++;

      if (clr)      ref_acc = 0;
      else if (en)  ref_acc = sat16(ref_acc + (longint'(a) * longint'(b)));

      @(posedge clk);
      checked++;
      if (acc !== ref_acc[15:0]) begin
        fails++;
        if (fails <= 3)
          $display("MISMATCH i=%0d a=%0d b=%0d dut=%0d ref=%0d",
                   i, a, b, acc, ref_acc);
      end
    end

    // EXACT format the SimChannel parses: /SIM_RESULT (PASS|FAIL) n=\d+ fails=\d+/.
    // The first draft printed "SIM PASS checked=..." and the channel reported
    // status 'error' — "no SIM_RESULT line" — NOT 'fail'. The positive control
    // correctly refused to characterise anything, which is the gate working:
    // a harness that cannot be parsed is an instrument defect, not a result.
    $display("STIM corner_hits=%0d of %0d vectors", corner_hits, checked);
    $display("STIM a_hits=%0d b_hits=%0d", a_hits, b_hits);
    if (fails == 0) $display("SIM_RESULT PASS n=%0d fails=0", checked);
    else            $display("SIM_RESULT FAIL n=%0d fails=%0d", checked, fails);
    $finish;
  end
endmodule
