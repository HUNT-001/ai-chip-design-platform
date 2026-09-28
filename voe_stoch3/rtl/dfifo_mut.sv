// dfifo_mut — dfifo with EXACTLY ONE CHARACTER changed.
//
//     good:  assign full_o = (cnt_q >= 6'(DEPTH));
//     mut:   assign full_o = (cnt_q >  6'(DEPTH));
//
// A >= written as > in a full comparison. This is among the most ordinary FIFO
// defects there is, and it has the property this board needs:
//
//     THE TWO DESIGNS ARE BIT-IDENTICAL UNTIL THE OCCUPANCY REACHES DEPTH.
//
// For cnt < 24 both expressions are false and every signal agrees. At cnt = 24
// the correct design refuses a push; the mutant accepts it, the counter goes to
// 25, and a live entry is overwritten. So activation happens if and only if the
// occupancy walk reaches the top — detection is a first-passage event, and the
// corner count the testbench records is exactly the quantity the theory
// predicts.
//
// The divergence is also observable almost immediately: the testbench compares
// cnt every cycle against an independent queue model, so the mismatch is caught
// on the first check after the over-push rather than waiting for the corrupted
// data to drain to the head. The lag is therefore the wait for one push draw,
// geometric with mean ~2.2 cycles against a first-passage scale of ~668 — but
// that lag is MEASURED and reported, not assumed.
//
// (Original header follows.)
//
// dfifo — a depth-24 synchronous FIFO, built as the THIRD stochastic benchmark.
//
// WHAT THIS BOARD IS FOR. sat_mac and pfifo are both admitted stochastic
// benchmarks, and the Stochastic Generalization Gate still failed on them: one
// memoryless law fits both detection curves, so the corpus contained one
// phenomenon sampled twice. The diagnosis was quantitative rather than vague.
// pfifo's occupancy chain mixes in ~12 cycles while its rare corner arrives
// once in ~1556, a ratio of ~130, so the state is forgotten many times over
// between opportunities and successive trials are effectively independent.
//
// Statefulness was not the variable. The ratio of mixing time to rare-event
// interval was. So this board is built to put that ratio at O(1):
//
//     depth D = 24, push and pop equally likely
//     E[T_hit | q0 = 0] = 667.8 cycles      (theory.py, exact)
//     t_mix             = 141 cycles
//     ratio             = 4.73              (pfifo: ~130)
//
// Both quantities scale as O(D^2) for a balanced one-dimensional walk, so the
// O(1) ratio is a structural consequence of the design rather than a tuned
// coincidence. See theory.py, which was written and solved BEFORE this file
// existed and which fixes the campaign budget.
//
// RARITY COMES FROM REACHABILITY, NOT FROM COINCIDENCE. On the earlier boards a
// rare input pattern could arrive at any moment, independent of history. Here
// the bug lives at the top of the occupancy walk, and from a cold start the
// design CANNOT reach it before enough net upward movement has accumulated.
// That produces a genuine dead zone, which an exponential hazard cannot
// represent: two campaigns of equal elapsed time have materially different
// remaining detection probability depending on where the occupancy currently
// sits. Detection is a first-passage event, not a Bernoulli trial.
//
// DEPTH 24 IS NOT A POWER OF TWO, deliberately, so the pointers must wrap
// explicitly and the memory has 32 physical slots behind a 5-bit pointer of
// which only 24 are in the logical ring.
//
// The dbg_q_o tap is OBSERVATION ONLY. The checker never reads it; it exists so
// the testbench can record the occupancy trajectory. Discovering later that the
// stochastic mechanism matters, having thrown away the state variable needed to
// explain why, would waste the whole board.
module dfifo_mut #(
    parameter int unsigned DW    = 16,
    parameter int unsigned DEPTH = 24
) (
    input  logic          clk_i,
    input  logic          rst_ni,
    input  logic [DW-1:0] data_i,
    input  logic          push_i,
    input  logic          pop_i,
    output logic [DW-1:0] data_o,
    output logic          full_o,
    output logic          empty_o,
    output logic [5:0]    cnt_o,
    // observation only — never consulted by the checker
    output logic [5:0]    dbg_q_o
);
  logic [4:0]    rd_q, wr_q;
  logic [5:0]    cnt_q;          // 6 bits: must be able to REPRESENT an
                                 // overflow past DEPTH, or the mutant below
                                 // would be masked by the counter width rather
                                 // than caught by the checker.
  logic [DW-1:0] mem_q [32];     // 32 physical slots, 24 in the logical ring

  // THE MUTATED LINE.
  assign full_o  = (cnt_q >  6'(DEPTH));   // <-- THE MUTATION: >= became >
  assign empty_o = (cnt_q == 6'd0);
  assign cnt_o   = cnt_q;
  assign data_o  = mem_q[rd_q];
  assign dbg_q_o = cnt_q;

  wire do_push = push_i && !full_o;
  wire do_pop  = pop_i  && !empty_o;

  // Explicit wrap at DEPTH-1: with DEPTH = 24 the 5-bit pointer does not wrap
  // on its own, so this function is the only thing keeping pointers in range.
  function automatic logic [4:0] nxt(input logic [4:0] p);
    nxt = (p == 5'(DEPTH - 1)) ? 5'd0 : p + 5'd1;
  endfunction

  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      rd_q  <= 5'd0;
      wr_q  <= 5'd0;
      cnt_q <= 6'd0;
    end else begin
      if (do_push) wr_q <= nxt(wr_q);
      if (do_pop)  rd_q <= nxt(rd_q);
      cnt_q <= cnt_q + (do_push ? 6'd1 : 6'd0) - (do_pop ? 6'd1 : 6'd0);
    end
  end

  // Memory cleared on reset so an unwritten slot reads as a concrete wrong
  // value rather than X: X-propagation differences between simulators would
  // make the detection rate a property of the tool instead of the design.
  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      for (int s = 0; s < 32; s++) mem_q[s] <= '0;
    end else if (do_push) begin
      mem_q[wr_q] <= data_i;
    end
  end
endmodule
