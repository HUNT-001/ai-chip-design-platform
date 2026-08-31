// Parameter-fixing wrapper for the real pulp mv_filter (majority-vote filter).
// WIDTH=4, THRESHOLD=10 keeps the state space small while remaining genuinely
// sequential (a saturating sample counter plus a sticky output flag).
module mvf_wrap (
    input  logic clk_i,
    input  logic rst_ni,
    input  logic sample_i,
    input  logic clear_i,
    input  logic d_i,
    output logic q_o,
    output logic [3:0] dbg_cnt_o
);
  mv_filter #(.WIDTH(4), .THRESHOLD(10)) u_filter (
      .clk_i    (clk_i),
      .rst_ni   (rst_ni),
      .sample_i (sample_i),
      .clear_i  (clear_i),
      .d_i      (d_i),
      .q_o      (q_o),
      .dbg_cnt_o(dbg_cnt_o)
  );
endmodule
