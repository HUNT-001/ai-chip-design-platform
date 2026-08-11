// Parameter-fixing wrapper for the real pulp lfsr_8bit (cache-replacement LFSR).
// SEED must be non-zero for a meaningful sequence; WIDTH=8 gives an 8-way
// one-hot output and a 3-bit binary index.
module lfsr_wrap (
    input  logic       clk_i,
    input  logic       rst_ni,
    input  logic       en_i,
    output logic [7:0] refill_way_oh,
    output logic [2:0] refill_way_bin
);
  lfsr_8bit #(.SEED(8'hA5), .WIDTH(8)) u_lfsr (
      .clk_i          (clk_i),
      .rst_ni         (rst_ni),
      .en_i           (en_i),
      .refill_way_oh  (refill_way_oh),
      .refill_way_bin (refill_way_bin)
  );
endmodule
