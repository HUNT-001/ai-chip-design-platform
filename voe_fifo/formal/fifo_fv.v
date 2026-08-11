// Plain-Verilog formal harness for the real cv32e40p_fifo (DEPTH=4, WIDTH=4).
// Read DIRECTLY by yosys — never through sv2v, which strips assert/assume.
//
// This is the first STATEFUL DUT on the platform, and it exists to exercise the
// distinction the kernel encodes but combinational logic could never show:
//
//   CLASS_CNT_BOUND (cnt_o <= DEPTH) is an INDUCTIVE invariant. A bounded model
//   check can only confirm it for the cycles it unrolls — the counter might
//   still overflow at cycle N+1 — so a bmc pass is recorded as `bounded_pass`
//   and does NOT discharge risk. k-induction proves it for all time, and only
//   that is recorded as a deductive proof.
//
// Reset: the DUT has an async active-low reset, so the harness holds rst_ni low
// for the first cycle and releases it. Without this the initial state is
// unconstrained and bmc fails immediately for the wrong reason.
`ifndef DUT
`define DUT fifo_wrap
`endif
`define DEPTH 3'd4

module fifo_fv (
    input wire       clk,
    input wire       flush,
    input wire       flush_but_first,
    input wire       testmode,
    input wire [3:0] din,
    input wire       push,
    input wire       pop
);
  // reset sequencing: rst_ni low in cycle 0, high thereafter
  reg [1:0] cyc = 2'd0;
  always @(posedge clk) if (cyc != 2'd3) cyc <= cyc + 2'd1;
  wire rst_n = (cyc != 2'd0);
  wire settled = (cyc != 2'd0);      // check properties only out of reset

  wire       full, empty;
  wire [2:0] cnt;
  wire [3:0] dout;

  `DUT dut (
      .clk_i             (clk),
      .rst_ni            (rst_n),
      .flush_i           (flush),
      .flush_but_first_i (flush_but_first),
      .testmode_i        (testmode),
      .full_o            (full),
      .empty_o           (empty),
      .cnt_o             (cnt),
      .data_i            (din),
      .push_i            (push),
      .data_o            (dout),
      .pop_i             (pop)
  );

`ifdef CLASS_CNT_BOUND
  // The occupancy counter may never exceed the FIFO depth. Overflowing it would
  // corrupt the full/empty flags and silently drop or duplicate entries.
  // Inductive: true in reset, and preserved by every transition because a push
  // is blocked while full_o holds.
  always @(posedge clk) if (settled) assert (cnt <= `DEPTH);
`endif

`ifdef CLASS_FLAGS
  // Status flags must agree with the counter (FALL_THROUGH=0).
  always @(posedge clk) if (settled) begin
    assert (full  == (cnt == `DEPTH));
    assert (empty == (cnt == 3'd0));
  end
`endif

`ifdef CLASS_NO_OVERFLOW
  // A push against a full FIFO must not change occupancy: no silent overwrite.
  reg       p_full, p_push, p_pop, p_settled, p_flush, p_fbf;
  reg [2:0] p_cnt;
  always @(posedge clk) begin
    p_full <= full; p_push <= push; p_pop <= pop; p_cnt <= cnt;
    p_settled <= settled; p_flush <= flush; p_fbf <= flush_but_first;
  end
  always @(posedge clk) begin
    if (settled && p_settled && !p_flush && !p_fbf &&
        p_full && p_push && !p_pop)
      assert (cnt == p_cnt);
  end
`endif

endmodule
