// Plain-Verilog formal harness for the real pulp mv_filter.
// Read DIRECTLY by yosys — never through sv2v, which strips assert/assume.
//
// SECOND HELD-OUT DESIGN. No policy was developed against it. Its purpose is
// replication: a policy that beat the incumbent on one unseen design may simply
// have suited that design. Two designs is still not generalisation, but it is
// the difference between one observation and a repeated one.
//
// Both properties are INDUCTIVE and stated over outputs only (no hierarchical
// references into the DUT), so they survive sv2v flattening.
`ifndef DUT
`define DUT mvf_wrap
`endif

module mvf_fv (
    input wire clk,
    input wire sample,
    input wire clear,
    input wire d
);
  // reset sequencing: rst_ni low for cycle 0, released thereafter
  reg [1:0] cyc = 2'd0;
  always @(posedge clk) if (cyc != 2'd3) cyc <= cyc + 2'd1;
  wire rst_n   = (cyc != 2'd0);
  wire settled = (cyc != 2'd0);

  wire q;

  `DUT dut (
      .clk_i    (clk),
      .rst_ni   (rst_n),
      .sample_i (sample),
      .clear_i  (clear),
      .d_i      (d),
      .q_o      (q)
  );

  // one-cycle history, so the properties can talk about transitions
  reg p_q, p_clear, p_settled;
  always @(posedge clk) begin
    p_q <= q; p_clear <= clear; p_settled <= settled;
  end

`ifdef CLASS_STICKY
  // Once the filter has fired, it must STAY fired until explicitly cleared.
  // A filter whose output could drop on its own would report a transient as
  // having ended when it had not.
  always @(posedge clk) begin
    if (settled && p_settled && p_q && !p_clear) assert (q == 1'b1);
  end
`endif

`ifdef CLASS_CLEAR
  // Clear must actually clear: asserting clear_i drives the output low on the
  // next edge, unconditionally.
  always @(posedge clk) begin
    if (settled && p_settled && p_clear) assert (q == 1'b0);
  end
`endif

endmodule
