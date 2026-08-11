// Parameter-fixing wrapper for the real cv32e40p_fifo.
// sv2v converts wrapper+DUT together, so the generated Verilog has concrete
// widths and the plain-Verilog formal harness needs no parameter handling.
// DEPTH=4, DATA_WIDTH=4 keeps the state space small enough to solve quickly
// while remaining genuinely sequential (pointers + counter + memory).
//   ADDR_DEPTH = $clog2(4) = 2  ->  cnt_o is [2:0]
module fifo_wrap (
    input  logic       clk_i,
    input  logic       rst_ni,
    input  logic       flush_i,
    input  logic       flush_but_first_i,
    input  logic       testmode_i,
    output logic       full_o,
    output logic       empty_o,
    output logic [2:0] cnt_o,
    input  logic [3:0] data_i,
    input  logic       push_i,
    output logic [3:0] data_o,
    input  logic       pop_i
);
  cv32e40p_fifo #(
      .FALL_THROUGH(1'b0),
      .DATA_WIDTH  (4),
      .DEPTH       (4)
  ) u_fifo (
      .clk_i             (clk_i),
      .rst_ni            (rst_ni),
      .flush_i           (flush_i),
      .flush_but_first_i (flush_but_first_i),
      .testmode_i        (testmode_i),
      .full_o            (full_o),
      .empty_o           (empty_o),
      .cnt_o             (cnt_o),
      .data_i            (data_i),
      .push_i            (push_i),
      .data_o            (data_o),
      .pop_i             (pop_i)
  );
endmodule
