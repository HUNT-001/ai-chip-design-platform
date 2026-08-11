// Plain-Verilog formal harness for the real pulp lfsr_8bit.
// Read DIRECTLY by yosys — never through sv2v, which strips assert/assume.
//
// HELD-OUT DESIGN. No policy was developed against this block. It exists so the
// evaluation engine can decide whether a candidate strategy generalises, rather
// than merely fitting the design it was tuned on.
`ifndef DUT
`define DUT lfsr_wrap
`endif

module lfsr_fv (
    input wire clk,
    input wire en
);
  // reset sequencing: rst_ni low for cycle 0, released thereafter
  reg [1:0] cyc = 2'd0;
  always @(posedge clk) if (cyc != 2'd3) cyc <= cyc + 2'd1;
  wire rst_n   = (cyc != 2'd0);
  wire settled = (cyc != 2'd0);

  wire [7:0] oh;
  wire [2:0] bin;

  `DUT dut (
      .clk_i          (clk),
      .rst_ni         (rst_n),
      .en_i           (en),
      .refill_way_oh  (oh),
      .refill_way_bin (bin)
  );

`ifdef CLASS_ONEHOT
  // The way-select must always be ONE-HOT. If two bits were ever set, a cache
  // refill would target two ways at once; if none, no way at all. This is the
  // invariant that matters for this block.
  always @(posedge clk) if (settled) begin
    assert (oh != 8'b0);              // at least one way selected
    assert ((oh & (oh - 8'b1)) == 8'b0);   // at most one bit set
  end
`endif

`ifdef CLASS_CONSISTENT
  // The one-hot and binary encodings of the same selection must agree.
  always @(posedge clk) if (settled) assert (oh[bin] == 1'b1);
`endif

`ifdef CLASS_STABLE
  // With the enable low the LFSR must not advance: a disabled replacement
  // policy that keeps moving would silently change victim selection.
  reg [7:0] p_oh;
  reg [2:0] p_bin;
  reg       p_en, p_settled;
  always @(posedge clk) begin
    p_oh <= oh; p_bin <= bin; p_en <= en; p_settled <= settled;
  end
  always @(posedge clk) begin
    if (settled && p_settled && !p_en) begin
      assert (oh  == p_oh);
      assert (bin == p_bin);
    end
  end
`endif

endmodule
