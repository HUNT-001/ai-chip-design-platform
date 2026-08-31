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
  wire [3:0] dbg_cnt;          // formal observation tap (see mv_filter.sv)

  `DUT dut (
      .clk_i    (clk),
      .rst_ni   (rst_n),
      .sample_i (sample),
      .clear_i  (clear),
      .d_i      (d),
      .q_o      (q),
      .dbg_cnt_o(dbg_cnt)
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

// --------------------------------------------------------------------------
// COUPLING PROBE — a SECOND design with a lemma dependency, if the solver says
// there is one. The FIFO's lemma related pointers and a memory; this one is a
// single scalar (the sample counter), so it tests whether the DEPENDENCY
// PHENOMENON recurs, not whether one lemma shape does.
//
// Every lesson from the FIFO probe is applied here from the start:
//   * the tap is a PORT, never a hierarchical reference;
//   * a connectivity control must pass before anything else is read;
//   * the reference model decides for itself and never reads q_o, so it cannot
//     inherit the bug it is supposed to detect;
//   * the lemma is asserted ALONE, never alongside the property it supports.
// --------------------------------------------------------------------------
`define MVF_TH 4'd10

`ifdef MVF_SHADOW
  // Reference model: counts qualifying samples and latches its own flag.
  // It reads sample/clear/d — the DUT's INPUTS — and never q_o.
  reg [3:0] s_cnt;
  reg       s_q;
  always @(posedge clk) begin
    if (!rst_n) begin
      s_cnt <= 4'd0; s_q <= 1'b0;
    end else if (clear) begin
      s_cnt <= 4'd0; s_q <= 1'b0;
    end else if (s_cnt >= `MVF_TH) begin
      s_q <= 1'b1;                       // latched: counter stops, flag sticks
    end else if (sample && d) begin
      s_cnt <= s_cnt + 4'd1;
    end
  end
`endif

`ifdef CLASS_MVF_EQUIV
  // THE DEPENDENT PROPERTY: the DUT's output agrees with the reference model.
  // Expected NOT to be inductive: from an arbitrary state the two counters may
  // differ, so one crosses THRESHOLD while the other does not.
  always @(posedge clk) if (settled) assert (q == s_q);
`endif

`ifdef CLASS_MVF_STATE
  // THE LEMMA, asserted alone: the two counters agree. A scalar relation, in
  // contrast to the FIFO's pointer+memory invariant.
  always @(posedge clk) if (settled) assert (s_cnt == dbg_cnt);
`endif

`ifdef USE_LEMMA_MVF
  // ASSUMED. Sound only once CLASS_MVF_STATE is proved on its own.
  always @(posedge clk) if (settled) assume (s_cnt == dbg_cnt);
`endif

`ifdef CLASS_MVF_TAP
  // CONNECTIVITY CONTROL, and it must PASS. counter_q only ever increments
  // while below THRESHOLD, so it cannot exceed it. An UNDRIVEN 4-bit wire can
  // take 11..15 and this fails — which is the point: it detects a tap that was
  // never wired, the defect that silently invalidated the FIFO probe twice.
  always @(posedge clk) if (settled) assert (dbg_cnt <= `MVF_TH);
`endif

endmodule
