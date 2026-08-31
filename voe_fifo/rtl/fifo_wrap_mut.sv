// Same wrapper, bound to the MUTATED fifo (full_o asserts one slot too late, so
// a push can drive the counter past DEPTH). This is the negative control: the
// counter-bound property must FAIL against it, which is how we know the
// property is actually binding rather than vacuously true.
module fifo_wrap_mut (
    input  logic       clk_i,
    input  logic       rst_ni,
    input  logic       flush_i,
    input  logic       flush_but_first_i,
    input  logic       testmode_i,
    output logic       full_o,
    output logic       empty_o,
    output logic [2:0] cnt_o,
    output logic [2:0] dbg_cnt_o,
    output logic [1:0] dbg_rd_o,
    output logic [1:0] dbg_wr_o,
    output logic [15:0] dbg_mem_o,
    input  logic [3:0] data_i,
    input  logic       push_i,
    output logic [3:0] data_o,
    input  logic       pop_i
);
  cv32e40p_fifo_mut #(
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
      .pop_i             (pop_i),
      .dbg_cnt_o         (dbg_cnt_o),
      .dbg_rd_o          (dbg_rd_o),
      .dbg_wr_o          (dbg_wr_o),
      .dbg_mem_o         (dbg_mem_o)
  );
  // The observation taps are ports of the FIFO itself (see cv32e40p_fifo.sv):
  // neither sv2v nor yosys resolves hierarchical references, so tapping from
  // out here produced floating wires and a vacuously-satisfied assumption.
endmodule
