// Wrapper bound to the MUTATED mv_filter, whose default next-state drops the
// flag instead of holding it. The sticky property must FAIL against this —
// that is how we know the property is binding rather than vacuously true.
module mvf_wrap_mut (
    input  logic clk_i,
    input  logic rst_ni,
    input  logic sample_i,
    input  logic clear_i,
    input  logic d_i,
    output logic q_o
);
  mv_filter_mut #(.WIDTH(4), .THRESHOLD(10)) u_filter (
      .clk_i    (clk_i),
      .rst_ni   (rst_ni),
      .sample_i (sample_i),
      .clear_i  (clear_i),
      .d_i      (d_i),
      .q_o      (q_o)
  );
endmodule
