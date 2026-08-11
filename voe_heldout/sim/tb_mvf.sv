// Self-checking testbench (for Verilator) for the real pulp mv_filter.
// Gives the second held-out design a genuine simulation option, so policies
// face a real allocation choice here too rather than being forced to formal.
// Checks the same two properties the formal harness states.
`ifndef DUT
`define DUT mvf_wrap
`endif
`ifndef NCYC
`define NCYC 20000
`endif

module tb_mvf;
  logic clk = 1'b0;
  logic rst_n = 1'b0;
  logic sample, clear, d, q;
  logic p_q, p_clear;
  int fails = 0, checked = 0;

  always #5 clk = ~clk;

  `DUT dut (
      .clk_i    (clk),
      .rst_ni   (rst_n),
      .sample_i (sample),
      .clear_i  (clear),
      .d_i      (d),
      .q_o      (q)
  );

  initial begin
    sample = 0; clear = 0; d = 0;
    repeat (2) @(posedge clk);
    rst_n = 1'b1;
    p_q = 0; p_clear = 0;
    for (int i = 0; i < `NCYC; i++) begin
      sample = $urandom() % 2;
      d      = $urandom() % 2;
      clear  = ($urandom() % 16) == 0;      // clear occasionally
      // Capture the state and inputs THIS edge will consume. An earlier version
      // compared against the PREVIOUS iteration's clear, which is off by one:
      // the DUT responds to the clear presented at this edge, not the last one.
      // That made the testbench report failures on correct RTL.
      p_q     = q;
      p_clear = clear;
      @(posedge clk);
      #1;
      checked++;
      // sticky: if it was high and this edge did not clear it, it stays high
      if (p_q && !p_clear && q !== 1'b1) begin
        fails++;
        if (fails <= 5) $display("MISMATCH sticky violated at cycle %0d", i);
      end
      // clear: a clear presented at this edge drives the output low
      if (p_clear && q !== 1'b0) begin
        fails++;
        if (fails <= 5) $display("MISMATCH clear ignored at cycle %0d", i);
      end
    end
    if (fails == 0) $display("SIM_RESULT PASS n=%0d fails=0", checked);
    else            $display("SIM_RESULT FAIL n=%0d fails=%0d", checked, fails);
    $finish;
  end
endmodule
