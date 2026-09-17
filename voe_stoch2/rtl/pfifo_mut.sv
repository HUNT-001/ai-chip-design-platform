// pfifo_mut — pfifo with EXACTLY ONE line changed.
//
// The difference, and nothing else:
//
//     good:  wr_q <= (cnt_q != 0) ? nxt(rd_q) : 3'd0;
//     mut:   wr_q <= (cnt_q != 0) ? (rd_q + 3'd1) : 3'd0;
//
// The wrap is missing on the flush-but-first path only. Everywhere else the
// pointers still wrap correctly, so this is not a broken FIFO — it is a correct
// FIFO with one control corner wrong, which is what makes it a realistic mutant
// rather than a design that any vector would refute.
//
// WHY IT IS RARE, AND WHY THAT RARITY IS A PROPERTY OF THE DESIGN. The bug can
// only manifest when flush_but_first arrives while the FIFO is non-empty AND the
// read pointer happens to sit at DEPTH-1. At any other read pointer, rd_q + 1
// and nxt(rd_q) are the same value and the mutant is bit-identical to the good
// design. Then the corruption still has to become OBSERVABLE: the out-of-range
// write pointer must be used by a later push, and that entry must later be
// popped and compared. None of those probabilities is chosen by the harness.
// They fall out of the occupancy random walk and the stimulus rates, and
// characterise.py MEASURES them rather than assuming them.
//
// Note the mutant remains in-range-looking to a casual reader: rd_q + 1 is a
// perfectly ordinary pointer increment. It is only wrong because DEPTH is 6 and
// the address is 3 bits wide, so values 6 and 7 are addressable but not backed
// by memory. That is the same class of defect as a real non-power-of-two
// prefetch buffer bug, and it is invisible to a depth-8 version of this design.
module pfifo_mut #(
    parameter int unsigned DW    = 16,
    parameter int unsigned DEPTH = 6
) (
    input  logic          clk_i,
    input  logic          rst_ni,
    input  logic          flush_i,
    input  logic          flush_but_first_i,
    input  logic [DW-1:0] data_i,
    input  logic          push_i,
    input  logic          pop_i,
    output logic [DW-1:0] data_o,
    output logic          full_o,
    output logic          empty_o,
    output logic [3:0]    cnt_o,
    output logic [2:0]    dbg_rd_o,
    output logic [2:0]    dbg_wr_o
);
  localparam int unsigned AW = 3;

  logic [2:0] rd_q, wr_q;
  logic [3:0] cnt_q;
  // EIGHT physical slots for a 3-bit pointer, of which only DEPTH are in the
  // logical ring. This is what the hardware actually looks like, and it is why
  // a missing wrap loses data silently rather than trapping: slots 6 and 7 are
  // addressable and writable, but the read pointer -- which always wraps -- can
  // never reach them, so anything written there is simply never read back.
  logic [DW-1:0] mem_q [8];

  assign full_o   = (cnt_q == 4'(DEPTH));
  assign empty_o  = (cnt_q == 4'd0);
  assign cnt_o    = cnt_q;
  assign data_o   = mem_q[rd_q];
  assign dbg_rd_o = rd_q;
  assign dbg_wr_o = wr_q;

  wire do_push = push_i && !full_o  && !flush_i && !flush_but_first_i;
  wire do_pop  = pop_i  && !empty_o && !flush_i && !flush_but_first_i;

  function automatic logic [2:0] nxt(input logic [2:0] p);
    nxt = (p == 3'(DEPTH - 1)) ? 3'd0 : p + 3'd1;
  endfunction

  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      rd_q  <= 3'd0;
      wr_q  <= 3'd0;
      cnt_q <= 4'd0;
    end else if (flush_i) begin
      rd_q  <= 3'd0;
      wr_q  <= 3'd0;
      cnt_q <= 4'd0;
    end else if (flush_but_first_i) begin
      rd_q  <= (cnt_q != 0) ? rd_q          : 3'd0;
      wr_q  <= (cnt_q != 0) ? (rd_q + 3'd1) : 3'd0;   // <-- MISSING WRAP
      cnt_q <= (cnt_q != 0) ? 4'd1          : 4'd0;
    end else begin
      if (do_push) wr_q <= nxt(wr_q);
      if (do_pop)  rd_q <= nxt(rd_q);
      cnt_q <= cnt_q + (do_push ? 4'd1 : 4'd0) - (do_pop ? 4'd1 : 4'd0);
    end
  end

  // Memory is cleared on reset so that reading a slot the design never wrote
  // yields a concrete wrong VALUE rather than X. That matters for the mutant:
  // an X-valued mismatch is detected by !== just as a wrong value is, but it is
  // far harder to read in a counterexample, and X-propagation differences
  // between simulators would make the benchmark's detection rate depend on the
  // tool rather than on the design.
  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      for (int s = 0; s < 8; s++) mem_q[s] <= '0;
    end else if (do_push) begin
      mem_q[wr_q] <= data_i;
    end
  end
endmodule
