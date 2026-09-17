// Parameter-fixing wrapper, matching the pattern used by the other DUTs in this
// corpus so the harness needs no parameter handling.
module pfifo_wrap (
    input  logic          clk_i,
    input  logic          rst_ni,
    input  logic          flush_i,
    input  logic          flush_but_first_i,
    input  logic [15:0]   data_i,
    input  logic          push_i,
    input  logic          pop_i,
    output logic [15:0]   data_o,
    output logic          full_o,
    output logic          empty_o,
    output logic [3:0]    cnt_o,
    output logic [2:0]    dbg_rd_o,
    output logic [2:0]    dbg_wr_o
);
  pfifo #(.DW(16), .DEPTH(6)) u_fifo (
      .clk_i(clk_i), .rst_ni(rst_ni), .flush_i(flush_i),
      .flush_but_first_i(flush_but_first_i), .data_i(data_i),
      .push_i(push_i), .pop_i(pop_i), .data_o(data_o),
      .full_o(full_o), .empty_o(empty_o), .cnt_o(cnt_o),
      .dbg_rd_o(dbg_rd_o), .dbg_wr_o(dbg_wr_o));
endmodule
