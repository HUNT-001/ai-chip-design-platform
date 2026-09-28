// Parameter-fixing wrapper, matching the pattern used by the other boards so
// the harness needs no parameter handling. Identical for both variants except
// for the instantiated module, so the testbench cannot tell them apart by shape.
module dfifo_wrap_mut (
    input  logic        clk_i,
    input  logic        rst_ni,
    input  logic [15:0] data_i,
    input  logic        push_i,
    input  logic        pop_i,
    output logic [15:0] data_o,
    output logic        full_o,
    output logic        empty_o,
    output logic [5:0]  cnt_o,
    output logic [5:0]  dbg_q_o
);
  dfifo_mut #(.DW(16), .DEPTH(24)) u_fifo (
      .clk_i(clk_i), .rst_ni(rst_ni), .data_i(data_i),
      .push_i(push_i), .pop_i(pop_i), .data_o(data_o),
      .full_o(full_o), .empty_o(empty_o), .cnt_o(cnt_o), .dbg_q_o(dbg_q_o));
endmodule
