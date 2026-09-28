// Self-checking testbench for dfifo, the reachability-limited stochastic board.
//
// WHAT IT MEASURES, AND WHY EACH QUANTITY IS SEPARATE
//
//   T_hit     first cycle at which occupancy reaches DEPTH. This is the
//             first-passage time the theory predicts exactly, and it is a
//             property of the STIMULUS, not of the bug — so it is recorded on
//             the good variant and the mutant alike, on the same seed, and the
//             two must agree.
//   T_detect  first cycle at which the checker sees a mismatch. Detection lags
//             activation by the wait for one push draw at full. That lag is
//             expected to be small (geometric, mean ~2.2 cycles) against a
//             first-passage scale of ~668, but "expected to be small" is not a
//             measurement, so it is reported per campaign.
//   levels    first cycle at which occupancy reaches 6, 12, 18, 24. A balanced
//             walk should reach level k in time ~k^2. Checking four points on
//             the way up is far stronger than checking the top alone: a board
//             that arrived at the top on schedule by accident would still have
//             to get the interior milestones right.
//
// THE INITIAL STATE IS PART OF THE EXPERIMENT, NOT AN IMPLEMENTATION DETAIL.
// Q0 selects between two genuinely different questions:
//
//   Q0 = 0     COLD START (primary). Measures the time required to physically
//              reach the rare region. The dead zone is real: the design cannot
//              reach q = 24 before enough net upward movement has accumulated.
//   Q0 > 0     NEAR-STATIONARY (control). Measures recurrence of a rare state
//              in an already-mixed chain. The stationary law of this walk is
//              exactly uniform on {0..24} by detailed balance, so the control
//              draws q0 from the true stationary distribution rather than from
//              a warm-up of guessed length.
//
// Running only the second and calling it "the stochastic regime" would answer a
// different question than the one asked. They are separate conditions, both
// committed in advance.
//
// THE REFERENCE MODEL IS A QUEUE with no pointer and no count of its own, so it
// cannot share the mutant's boundary defect even in principle. The dbg_q_o tap
// is read ONLY by the instrumentation counters, never by the checker.
`ifndef DUT
`define DUT dfifo_wrap
`endif
`ifndef NVEC
`define NVEC 668
`endif
// START MODE, not a start VALUE. The near-stationary control needs a different
// q0 on every seed; a compile-time constant would mean one Verilator build per
// occupancy. Instead the mode is compiled in and the value is drawn from the
// seeded stream, so two builds cover both conditions and the draw is
// reproducible from the seed like everything else.
//   STATIONARY undefined -> cold start, q0 = 0
//   STATIONARY defined   -> q0 ~ Uniform{0..DEPTH}, the EXACT stationary law of
//                           this walk (uniform by detailed balance, since
//                           p_push == p_pop). No warm-up of guessed length.
`ifdef STATIONARY
  `define Q0_MODE 1
`else
  `define Q0_MODE 0
`endif

module tb_dfifo;
  localparam int DEPTH = 24;
  // Thresholds on one byte, so the achievable probabilities are multiples of
  // 1/256 and theory.py can describe EXACTLY this chain: 115/256 = 0.44921875
  // push, the same pop, 26/256 idle.
  localparam int PUSH_T = 115;
  localparam int POP_T  = 230;

  logic clk = 1'b0, rst_n = 1'b0;
  logic push, pop;
  logic [15:0] data_i, data_o;
  logic full, empty;
  logic [5:0] cnt, dbg_q;

  logic [15:0] model[$];

  int fails = 0, checked = 0;
  int t_hit = -1, t_detect = -1, max_q = 0;
  int lvl6 = -1, lvl12 = -1, lvl18 = -1;
  int sz;
  logic [31:0] r;
  int b;
  int q0 = 0;
  // STIMULUS CHARACTERISATION. The chain the theory solves is defined by
  // p_push, p_pop, p_idle and by the steps being INDEPENDENT. The first three
  // are easy to assume and easy to get wrong; the fourth is the one that bit
  // sat_mac, where two consecutive $urandom() calls turned out to be
  // correlated while both marginals were exactly uniform. So all four are
  // measured, on every campaign, and reported whether or not anything looks
  // wrong -- a check that runs only on suspicion never confirms the healthy
  // case, and this project has now hit that pattern five times.
  int n_push = 0, n_pop = 0, n_idle = 0;
  // `cross` is a SystemVerilog KEYWORD (covergroup cross coverage), so the
  // accumulator cannot be called that. The printed field keeps the name.
  int cross_sum = 0;      // sum of s_t * s_{t-1}, for the lag-1 correlation
  int step = 0, prev_step = 0;
  int have_prev = 0;

  always #5 clk = ~clk;

  `DUT dut (.clk_i(clk), .rst_ni(rst_n), .data_i(data_i), .push_i(push),
            .pop_i(pop), .data_o(data_o), .full_o(full), .empty_o(empty),
            .cnt_o(cnt), .dbg_q_o(dbg_q));

  task automatic check(input int i);
    checked++;
    if (cnt !== 6'(model.size())) begin
      fails++;
      if (t_detect < 0) t_detect = i;
      if (fails <= 3)
        $display("MISMATCH i=%0d cnt dut=%0d ref=%0d", i, cnt, model.size());
    end else if (empty !== (model.size() == 0)) begin
      fails++;
      if (t_detect < 0) t_detect = i;
      if (fails <= 3) $display("MISMATCH i=%0d empty", i);
    end else if (model.size() > 0 && data_o !== model[0]) begin
      fails++;
      if (t_detect < 0) t_detect = i;
      if (fails <= 3)
        $display("MISMATCH i=%0d head dut=%04h ref=%04h", i, data_o, model[0]);
    end
  endtask

  // Drive one push, model included. Used only to establish a non-zero initial
  // occupancy for the near-stationary condition, BEFORE measurement starts.
  task automatic preload_one(input logic [15:0] v);
    push   = 1'b1;
    pop    = 1'b0;
    data_i = v;
    @(posedge clk);
    model.push_back(v);
    push = 1'b0;
  endtask

  initial begin
    push = 0; pop = 0; data_i = '0;
    repeat (2) @(posedge clk);
    rst_n = 1'b1;
    @(posedge clk);

    // ---- initial state -----------------------------------------------------
    // Preloading walks the occupancy up from 0, so for any Q0 < DEPTH it cannot
    // itself reach the rare state and cannot activate the bug. Q0 = DEPTH would
    // start the campaign already at the boundary; that is legitimate for the
    // stationary condition and is simply reported as T_hit = 0.
    if (`Q0_MODE == 1) begin
      r  = $urandom();
      q0 = int'(r % 32'd25);          // uniform on {0..24}
    end
    for (int k = 0; k < q0; k++) preload_one(16'hE000 + 16'(k));
    @(negedge clk);

    for (int i = 0; i < `NVEC; i++) begin
      // ONE draw, separated bit lanes -- and now the code actually does that.
      //
      // It previously called $urandom() TWICE per iteration, once for the
      // decision and once for data, so consecutive decisions came from
      // every-other call while this comment claimed they came from one. On
      // sat_mac two CONSECUTIVE calls were measurably correlated (joint corner
      // rate 2x its independent prediction, ~3.6 sigma, with both marginals
      // exactly uniform); whether every-other calls are correlated was never
      // measured. Correlation in the step sequence changes first-passage time
      // directly, so this was a live candidate for the ~8% slow walk observed
      // against theory -- and the comment asserting a property nobody checked
      // is the same defect class as a docstring describing behaviour the
      // function does not have.
      //
      // Decision and data now come from widely separated lanes of a single
      // draw. Whether THAT is independent is not assumed either: the counters
      // below measure it.
      r      = $urandom();
      b      = int'(r[7:0]);
      push   = (b < PUSH_T);
      pop    = (!push) && (b < POP_T);
      data_i = r[31:16];

      // the INTENDED step, before the design clips it at a boundary. This is a
      // property of the stimulus alone, which is what the kernel describes.
      step = push ? 1 : (pop ? -1 : 0);
      if (push)      n_push++;
      else if (pop)  n_pop++;
      else           n_idle++;
      if (have_prev) cross_sum += step * prev_step;
      prev_step = step;
      have_prev = 1;

      // instrumentation, read from the tap BEFORE the edge, so it describes the
      // state the design is about to act on. Never read by the checker.
      if (int'(dbg_q) > max_q) max_q = int'(dbg_q);
      if (lvl6  < 0 && dbg_q >= 6'd6)  lvl6  = i;
      if (lvl12 < 0 && dbg_q >= 6'd12) lvl12 = i;
      if (lvl18 < 0 && dbg_q >= 6'd18) lvl18 = i;
      if (t_hit < 0 && dbg_q >= 6'(DEPTH)) t_hit = i;

      check(i);
      @(posedge clk);

      // model update, using exactly the inputs the design just sampled. sz is
      // captured first because the design decides do_push and do_pop from the
      // same pre-edge occupancy; reading the DUT's own cnt here instead would
      // make the model agree with a buggy design by construction.
      sz = model.size();
      if (pop  && sz > 0)     void'(model.pop_front());
      if (push && sz < DEPTH) model.push_back(data_i);

      @(negedge clk);
    end

    $display("STIM2 npush=%0d npop=%0d nidle=%0d cross=%0d",
             n_push, n_pop, n_idle, cross_sum);
    $display("STIM q0=%0d thit=%0d tdet=%0d maxq=%0d lvl6=%0d lvl12=%0d lvl18=%0d of %0d cycles",
             q0, t_hit, t_detect, max_q, lvl6, lvl12, lvl18, checked);
    if (fails == 0) $display("SIM_RESULT PASS n=%0d fails=0", checked);
    else            $display("SIM_RESULT FAIL n=%0d fails=%0d", checked, fails);
    $finish;
  end
endmodule
