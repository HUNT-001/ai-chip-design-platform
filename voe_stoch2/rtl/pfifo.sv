// pfifo — a small synchronous prefetch-style FIFO with flush and
// flush-but-first, written to be the SECOND stochastic benchmark.
//
// WHY A SECOND BOARD AT ALL. voe_stoch/sat_mac established that this project
// finally has a non-degenerate stochastic regime: 0 < P(detect) < 1 under real
// Verilator execution with no probability injected by the harness. It did NOT
// establish that stochastic verification behaviour GENERALISES. One board
// cannot separate "this phenomenon is real" from "this phenomenon is a property
// of that particular design" — which is exactly the trap Experiment 10 caught
// when policy L looked good on the full board and lost on the held-out one.
//
// WHY THIS DESIGN, AND NOT A SECOND ARITHMETIC ONE. sat_mac's rarity is
// MEMORYLESS: each vector independently hits the (-128) x (-128) corner with
// probability 1/65536, so P(detect | N vectors) = 1 - (1 - p)^N exactly. That is
// a Bernoulli process, and a benchmark family built only out of such processes
// would make "stochastic" mean one single shape.
//
// A FIFO's rare corner is structurally different in a way that matters. Reaching
// the violating state requires the occupancy random walk to be in a particular
// place AND a rare control event to arrive AND the corruption to survive long
// enough to reach an output. Those are CORRELATED across cycles, not
// independent, and there is a warm-up period during which the corner cannot be
// reached at all. If the two boards are genuinely distinct stochastic regimes,
// their detection-vs-campaign-length curves should not have the same shape. That
// is a sharper generalisation test than comparing two point estimates of
// P(detect), and it is what this board exists to make measurable.
//
// DEPTH = 6 IS DELIBERATE AND LOAD-BEARING. It is not a power of two, so the
// read/write pointers must wrap EXPLICITLY at DEPTH-1 rather than relying on the
// 3-bit address rolling over. That is what makes a missing wrap a real defect
// instead of a no-op: on a depth-8 FIFO the mutant below would be bit-identical
// to the correct design and the whole benchmark would silently measure nothing.
// Non-power-of-two prefetch buffers are also what the real cv32e40p uses.
//
// The dbg_* ports are OBSERVATION TAPS. They drive nothing inside the design.
// They exist because the testbench's instrumentation needs to count how often
// the stimulus reaches the corner, and inferring that from the detection rate
// would assume the very thing the instrumentation is there to check. The
// CHECKER never reads them — only the counters do. That separation is the point:
// an earlier reference model in this project read a DUT output, inherited the
// mutant's bug, and made a negative control silently vacuous.
module pfifo #(
    parameter int unsigned DW    = 16,
    parameter int unsigned DEPTH = 6
) (
    input  logic          clk_i,
    input  logic          rst_ni,
    input  logic          flush_i,            // drop everything
    input  logic          flush_but_first_i,  // keep the head entry only
    input  logic [DW-1:0] data_i,
    input  logic          push_i,
    input  logic          pop_i,
    output logic [DW-1:0] data_o,
    output logic          full_o,
    output logic          empty_o,
    output logic [3:0]    cnt_o,
    // observation only — instrumentation, never the checker
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

  // Explicit wrap at DEPTH-1. With DEPTH = 6 the 3-bit pointer does NOT wrap on
  // its own, so this function is the only thing keeping the pointers in range.
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
      // Keep the head entry in place and re-point the tail immediately after
      // it. The head STAYS where it is, so the write pointer must be the head's
      // successor -- and computing that successor is where the wrap lives.
      rd_q  <= (cnt_q != 0) ? rd_q      : 3'd0;
      wr_q  <= (cnt_q != 0) ? nxt(rd_q) : 3'd0;   // <-- THE MUTATED LINE
      cnt_q <= (cnt_q != 0) ? 4'd1      : 4'd0;
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
