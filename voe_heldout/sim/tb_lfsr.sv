// Self-checking testbench (for Verilator) for the real pulp lfsr_8bit.
//
// Its purpose in the experiment is to make the held-out benchmark
// DISCRIMINATING. With formal as the only channel, every policy is forced down
// the same path and all strategies score identically — a tie that says nothing
// about the policies. Adding simulation gives a genuine allocation decision:
//
//   simulation  cheap, raises n_eff, NEVER discharges a sequential obligation
//   formal      4x the cost, but closes the obligation for all time
//
// A policy that samples first now pays for it, and one that goes straight to
// proof does not. That difference is what the comparison is supposed to measure.
//
// Checks the same properties the formal harness does, so the two channels are
// talking about the same obligations.
`ifndef DUT
`define DUT lfsr_wrap
`endif
`ifndef NCYC
`define NCYC 20000
`endif

module tb_lfsr;
  logic clk = 1'b0;
  logic rst_n = 1'b0;
  logic en = 1'b1;
  logic [7:0] oh;
  logic [2:0] bin;
  int fails = 0;
  int checked = 0;

  always #5 clk = ~clk;

  `DUT dut (
      .clk_i          (clk),
      .rst_ni         (rst_n),
      .en_i           (en),
      .refill_way_oh  (oh),
      .refill_way_bin (bin)
  );

  // one-hot: exactly one bit set
  function automatic bit is_onehot(input logic [7:0] v);
    is_onehot = (v != 8'b0) && ((v & (v - 8'b1)) == 8'b0);
  endfunction

  initial begin
    repeat (2) @(posedge clk);
    rst_n = 1'b1;                       // release reset
    for (int i = 0; i < `NCYC; i++) begin
      en = ($urandom() % 4) != 0;       // mostly enabled, sometimes idle
      @(posedge clk);
      #1;
      checked++;
      if (!is_onehot(oh)) begin
        fails++;
        if (fails <= 5) $display("MISMATCH not-onehot oh=%08b bin=%0d", oh, bin);
      end else if (oh[bin] !== 1'b1) begin
        fails++;
        if (fails <= 5) $display("MISMATCH oh/bin disagree oh=%08b bin=%0d", oh, bin);
      end
    end
    if (fails == 0) $display("SIM_RESULT PASS n=%0d fails=0", checked);
    else            $display("SIM_RESULT FAIL n=%0d fails=%0d", checked, fails);
    $finish;
  end
endmodule
