// Constrained-random self-checking testbench for pfifo.
//
// REFERENCE MODEL INDEPENDENCE. The model is a SystemVerilog queue implementing
// the SPECIFICATION of the FIFO — push appends, pop removes the head,
// flush_but_first keeps the head entry and discards the rest. It has no read or
// write pointer at all, so it cannot share the mutant's pointer bug even in
// principle. This is deliberate and it is the lesson of the fifo coupling probe,
// where a reference model that read the DUT's own full_o inherited the mutant's
// defect and made the negative control silently vacuous for five iterations.
//
// The dbg_rd_o / dbg_wr_o taps ARE read here, but only by the instrumentation
// counters. The checker never touches them. Keeping that boundary visible
// matters: a checker that reads DUT internals is how a testbench stops being an
// independent oracle, and the counters exist precisely so the corner rate can be
// OBSERVED rather than inferred from the detection rate it is meant to validate.
//
// CAMPAIGN LENGTH COMES FROM NVEC, NOT A LOCAL DEFAULT. SimChannel.build()
// passes campaign length as -DNVEC. tb_satmac.sv read `NCYC` instead and
// defaulted it to 20000, so its campaign length never actually varied with what
// the caller asked for -- harmless while every caller asked for 20000, and a
// silent flat line the moment anything swept it. This bench reads NVEC.
`ifndef DUT
`define DUT pfifo_wrap
`endif
`ifndef NVEC
`define NVEC 20000
`endif

module tb_pfifo;
  logic clk = 1'b0, rst_n = 1'b0;
  logic flush, fbf, push, pop;
  logic [15:0] data_i, data_o;
  logic full, empty;
  logic [3:0] cnt;
  logic [2:0] dbg_rd, dbg_wr;

  // --- the reference model: a queue, with no notion of a pointer -----------
  logic [15:0] model[$];

  int fails = 0, checked = 0;
  // instrumentation. NOT used by the checker.
  int n_trigger = 0, n_fbf = 0, n_full = 0;
  logic [31:0] r, d;
  // Declared HERE rather than inside the loop body on purpose. A variable
  // declared inside a begin/end of an initial block is STATIC by default in
  // SystemVerilog, so `int sz = model.size();` would be initialised once at
  // time zero and never again -- it would read 0 on every iteration and the
  // model would silently accept pushes into a full FIFO. Declaring at module
  // scope and assigning procedurally removes the trap rather than relying on
  // remembering it.
  int sz;
  logic [15:0] head;

  always #5 clk = ~clk;

  `DUT dut (.clk_i(clk), .rst_ni(rst_n), .flush_i(flush),
            .flush_but_first_i(fbf), .data_i(data_i), .push_i(push),
            .pop_i(pop), .data_o(data_o), .full_o(full), .empty_o(empty),
            .cnt_o(cnt), .dbg_rd_o(dbg_rd), .dbg_wr_o(dbg_wr));

  task automatic check(input int i);
    checked++;
    if (cnt !== 4'(model.size())) begin
      fails++;
      if (fails <= 3)
        $display("MISMATCH i=%0d cnt dut=%0d ref=%0d", i, cnt, model.size());
    end else if (empty !== (model.size() == 0)) begin
      fails++;
      if (fails <= 3) $display("MISMATCH i=%0d empty dut=%0b ref=%0b",
                               i, empty, (model.size() == 0));
    end else if (full !== (model.size() == 6)) begin
      fails++;
      if (fails <= 3) $display("MISMATCH i=%0d full dut=%0b ref=%0b",
                               i, full, (model.size() == 6));
    end else if (model.size() > 0 && data_o !== model[0]) begin
      fails++;
      if (fails <= 3)
        $display("MISMATCH i=%0d head dut=%04h ref=%04h (rd=%0d wr=%0d)",
                 i, data_o, model[0], dbg_rd, dbg_wr);
    end
  endtask

  initial begin
    flush = 0; fbf = 0; push = 0; pop = 0; data_i = '0;
    repeat (2) @(posedge clk);
    rst_n = 1'b1;
    @(posedge clk);

    for (int i = 0; i < `NVEC; i++) begin
      @(negedge clk);

      // ONE draw, separated bit lanes. Two consecutive $urandom() calls are
      // measurably correlated in Verilator -- on sat_mac the joint corner rate
      // ran 2x its independent prediction at ~3.6 sigma while both marginals
      // were exactly uniform. Slicing one draw is the fix that was validated
      // there, and characterise.py re-measures rather than assuming it here.
      r      = $urandom();
      push   = (r[3:0]   < 4'd10);        // ~62.5%: a prefetcher fills eagerly
      pop    = (r[11:8]  < 4'd8);         // ~50%:  the consumer is slower
      fbf    = (r[21:14] == 8'd0);        // ~1/256:  branch-misprediction rate
      flush  = (r[31:22] == 10'd0);       // ~1/1024: pipeline flush, rarer
      d      = $urandom();
      data_i = d[15:0];

      // instrumentation, read from the taps BEFORE the edge, so it describes
      // the state the DUT is about to act on.
      if (full) n_full++;
      if (fbf && !flush && cnt != 0) begin
        n_fbf++;
        // the mutated line is only reachable here: non-empty flush_but_first
        // with the head sitting on the last memory slot. Anywhere else
        // rd_q + 1 and nxt(rd_q) are the same value.
        if (dbg_rd == 3'd5) n_trigger++;
      end

      check(i);
      @(posedge clk);

      // model update, using exactly the inputs the DUT just sampled
      if (flush) begin
        model.delete();
      end else if (fbf) begin
        if (model.size() > 0) begin
          head = model[0];
          model.delete();
          model.push_back(head);
        end
      end else begin
        // Capture the occupancy BEFORE either operation. The DUT decides
        // do_push and do_pop from the same pre-edge count, so a model that
        // popped first and then tested the post-pop size would accept a push
        // the DUT rejected. Reading the DUT's own cnt here instead would be
        // the independence violation this bench is built to avoid -- it would
        // make the model agree with a buggy DUT by construction.
        sz = model.size();
        if (pop  && sz > 0)     void'(model.pop_front());
        if (push && sz != 6)    model.push_back(data_i);
      end
    end

    // EXACT format SimChannel parses: /SIM_RESULT (PASS|FAIL) n=\d+ fails=\d+/
    $display("STIM trigger=%0d fbf=%0d full=%0d of %0d cycles",
             n_trigger, n_fbf, n_full, checked);
    if (fails == 0) $display("SIM_RESULT PASS n=%0d fails=0", checked);
    else            $display("SIM_RESULT FAIL n=%0d fails=%0d", checked, fails);
    $finish;
  end
endmodule
