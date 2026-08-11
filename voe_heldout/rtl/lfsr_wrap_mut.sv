// Wrapper bound to the MUTATED LFSR (bit 0 forced set), so the one-hot
// invariant is violated whenever the selected way is not 0. Negative control:
// the one-hot property MUST fail against this, which is how we know the
// property is binding rather than vacuously true.
//
// Named `lfsr_wrap_mut` so the harness selects it with -DDUT=lfsr_wrap_mut.
module lfsr_wrap_mut (
    input  logic       clk_i,
    input  logic       rst_ni,
    input  logic       en_i,
    output logic [7:0] refill_way_oh,
    output logic [2:0] refill_way_bin
);
  lfsr_8bit_mut #(.SEED(8'hA5), .WIDTH(8)) u_lfsr (
      .clk_i          (clk_i),
      .rst_ni         (rst_ni),
      .en_i           (en_i),
      .refill_way_oh  (refill_way_oh),
      .refill_way_bin (refill_way_bin)
  );
endmodule
