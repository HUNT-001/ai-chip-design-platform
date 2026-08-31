// Plain-Verilog formal harness for the real cv32e40p_fifo (DEPTH=4, WIDTH=4).
// Read DIRECTLY by yosys — never through sv2v, which strips assert/assume.
//
// This is the first STATEFUL DUT on the platform, and it exists to exercise the
// distinction the kernel encodes but combinational logic could never show:
//
//   CLASS_CNT_BOUND (cnt_o <= DEPTH) is an INDUCTIVE invariant. A bounded model
//   check can only confirm it for the cycles it unrolls — the counter might
//   still overflow at cycle N+1 — so a bmc pass is recorded as `bounded_pass`
//   and does NOT discharge risk. k-induction proves it for all time, and only
//   that is recorded as a deductive proof.
//
// Reset: the DUT has an async active-low reset, so the harness holds rst_ni low
// for the first cycle and releases it. Without this the initial state is
// unconstrained and bmc fails immediately for the wrong reason.
`ifndef DUT
`define DUT fifo_wrap
`endif
`define DEPTH 3'd4

module fifo_fv (
    input wire       clk,
    input wire       flush,
    input wire       flush_but_first,
    input wire       testmode,
    input wire [3:0] din,
    input wire       push,
    input wire       pop
);
  // reset sequencing: rst_ni low in cycle 0, high thereafter
  reg [1:0] cyc = 2'd0;
  always @(posedge clk) if (cyc != 2'd3) cyc <= cyc + 2'd1;
  wire rst_n = (cyc != 2'd0);
  wire settled = (cyc != 2'd0);      // check properties only out of reset

  wire       full, empty;
  wire [2:0] cnt;
  wire [2:0] dbg_cnt;            // formal observation taps (see fifo_wrap.sv)
  wire [1:0] dbg_rd, dbg_wr;
  wire [15:0] dbg_mem;        // mem_q, flattened (DEPTH 4 x WIDTH 4)
  wire [3:0] dout;

  `DUT dut (
      .clk_i             (clk),
      .rst_ni            (rst_n),
      .flush_i           (flush),
      .flush_but_first_i (flush_but_first),
      .testmode_i        (testmode),
      .full_o            (full),
      .empty_o           (empty),
      .cnt_o             (cnt),
      .dbg_cnt_o         (dbg_cnt),
      .dbg_rd_o          (dbg_rd),
      .dbg_wr_o          (dbg_wr),
      .dbg_mem_o         (dbg_mem),
      .data_i            (din),
      .push_i            (push),
      .data_o            (dout),
      .pop_i             (pop)
  );

`ifdef CLASS_CNT_BOUND
  // The occupancy counter may never exceed the FIFO depth. Overflowing it would
  // corrupt the full/empty flags and silently drop or duplicate entries.
  // Inductive: true in reset, and preserved by every transition because a push
  // is blocked while full_o holds.
  always @(posedge clk) if (settled) assert (cnt <= `DEPTH);
`endif

`ifdef CLASS_FLAGS
  // Status flags must agree with the counter (FALL_THROUGH=0).
  always @(posedge clk) if (settled) begin
    assert (full  == (cnt == `DEPTH));
    assert (empty == (cnt == 3'd0));
  end
`endif

`ifdef CLASS_NO_OVERFLOW
  // A push against a full FIFO must not change occupancy: no silent overwrite.
  reg       p_full, p_push, p_pop, p_settled, p_flush, p_fbf;
  reg [2:0] p_cnt;
  always @(posedge clk) begin
    p_full <= full; p_push <= push; p_pop <= pop; p_cnt <= cnt;
    p_settled <= settled; p_flush <= flush; p_fbf <= flush_but_first;
  end
  always @(posedge clk) begin
    if (settled && p_settled && !p_flush && !p_fbf &&
        p_full && p_push && !p_pop)
      assert (cnt == p_cnt);
  end
`endif

`ifdef USE_LEMMA_CNT
  // ASSUMED, not asserted. Sound ONLY because CLASS_CNT_BOUND is proved as its
  // own obligation by k-induction; the judgment that results from a run with
  // this define set therefore carries `cnt_bound` in its Basis.assumptions, and
  // the kernel must refuse to treat it as discharged until that lemma holds.
  // An assumption that is never discharged is how a proof becomes vacuous.
  always @(posedge clk) if (settled) assume (cnt <= `DEPTH);
`endif

`ifdef SHADOW
  // A REFERENCE MODEL of the FIFO, kept separate from the properties that use
  // it so a LEMMA can be proved without also asserting the property it is meant
  // to support. Bundling them is why `state_match` could never pass: the task
  // defined CLASS_DATA_INTEGRITY too, so it failed on the integrity assertion
  // (line ~151) rather than on its own.
  //
  // The shadow is RESET TO ZERO because the DUT resets its memory to zero
  // (`mem_q <= '0` in cv32e40p_fifo.sv). Leaving it uninitialised meant the two
  // memories disagreed from cycle 0 by construction, which no lemma could fix.
  //
  // flush and flush_but_first are MIRRORED, not skipped: they reset the DUT's
  // pointers, so skipping a cycle desyncs the models permanently and the
  // assertion then fires somewhere unrelated. testmode is deliberately absent —
  // it is never used in this FIFO's datapath.
  reg [3:0] shadow [0:3];
  reg [2:0] s_cnt;
  reg [1:0] s_rd, s_wr;
  integer i;

  // The model decides acceptance from its OWN occupancy, never from the DUT's
  // full_o/empty_o. Gating on the DUT's status outputs makes the model inherit
  // whatever those outputs get wrong: the mutant asserts full_o one slot late,
  // the shadow then accepted the same illegal 5th push, wrapped s_wr the same
  // way and corrupted the same slot — the two agreed and the negative control
  // detected NOTHING. It only appeared to work while unrelated modelling bugs
  // happened to desync the models. A reference model that reads the signals it
  // is meant to check is not a reference model.
  wire s_full  = (s_cnt == `DEPTH);
  wire s_empty = (s_cnt == 3'd0);
  wire do_push = push && !s_full;
  wire do_pop  = pop  && !s_empty;

  // MEMORY. Mirrors the DUT's own memory block, which is SEPARATE from its
  // pointer block and has NO flush handling:
  //     if (~rst_ni) mem_q <= '0; else if (!gate_clock) mem_q <= mem_n;
  // gate_clock falls only on `push_i && ~full_o`, so a cycle with flush AND an
  // accepted push still WRITES. An earlier version put the write inside an
  // `else if (flush)` chain, so the shadow skipped that write while the DUT
  // performed it — the memories then differed at step 3 while every pointer
  // still agreed, which is exactly what the counterexample showed.
  always @(posedge clk) begin
    if (!rst_n) begin
      for (i = 0; i < 4; i = i + 1) shadow[i] <= 4'd0;
    end else if (do_push) begin
      shadow[s_wr] <= din;
    end
  end

  // POINTERS AND COUNT. Here flush DOES take priority, matching the DUT's
  // `case (1'b1) flush_i: ... flush_but_first_i: ... default: ...`.
  always @(posedge clk) begin
    if (!rst_n) begin
      s_cnt <= 3'd0; s_rd <= 2'd0; s_wr <= 2'd0;
    end else if (flush) begin
      s_cnt <= 3'd0; s_rd <= 2'd0; s_wr <= 2'd0;
    end else if (flush_but_first) begin
      s_rd  <= (s_cnt > 3'd0) ? s_rd : 2'd0;
      s_wr  <= (s_cnt > 3'd0) ? (s_rd + 2'd1) : 2'd0;
      s_cnt <= (s_cnt > 3'd0) ? 3'd1 : 3'd0;
    end else begin
      if (do_push) s_wr <= s_wr + 2'd1;
      if (do_pop)  s_rd <= s_rd + 2'd1;
      case ({do_push, do_pop})
        2'b10:   s_cnt <= s_cnt + 3'd1;
        2'b01:   s_cnt <= s_cnt - 3'd1;
        default: ;
      endcase
    end
  end
`endif

`ifdef CLASS_DATA_INTEGRITY
  // The DEPENDENT property: true, but NOT inductive on its own. Base case
  // passes 12 steps on the good DUT and the mutant is caught at step 7, so the
  // model is validated in both directions before any of this is interpreted.
  always @(posedge clk) if (settled) begin
    assert (s_cnt == cnt);                       // models agree on occupancy
    if (!empty) assert (dout == shadow[s_rd]);   // and on the data itself
  end
`endif

`ifdef CLASS_STATE_MATCH
  // The STRENGTHENING lemma, arrived at by measurement, not by assumption.
  //
  //   `cnt <= DEPTH`      measurably did NOT help: integrity's induction failed
  //                       identically with and without it.
  //   pointers + count    also insufficient on its own — induction still fails,
  //                       because from an arbitrary state the two MEMORIES may
  //                       disagree even when every pointer agrees.
  //
  // So the invariant relating the two machines must cover the storage as well.
  // This is a white-box claim about DUT internals, stated as its own obligation
  // and PROVED before anything is permitted to assume it.
  always @(posedge clk) if (settled) begin
    assert (s_cnt == dbg_cnt);
    assert (s_rd  == dbg_rd);
    assert (s_wr  == dbg_wr);
    assert (shadow[0] == dbg_mem[3:0]);
    assert (shadow[1] == dbg_mem[7:4]);
    assert (shadow[2] == dbg_mem[11:8]);
    assert (shadow[3] == dbg_mem[15:12]);
  end
`endif

`ifdef USE_LEMMA_STATE
  // ASSUMED. Sound only because CLASS_STATE_MATCH is proved as its own
  // obligation; until it is, a judgment resting on this carries an undischarged
  // assumption and the kernel must refuse to treat it as closed.
  always @(posedge clk) if (settled) begin
    assume (s_cnt == dbg_cnt);
    assume (s_rd  == dbg_rd);
    assume (s_wr  == dbg_wr);
    assume (shadow[0] == dbg_mem[3:0]);
    assume (shadow[1] == dbg_mem[7:4]);
    assume (shadow[2] == dbg_mem[11:8]);
    assume (shadow[3] == dbg_mem[15:12]);
  end
`endif

`ifdef CLASS_TAP_CONTROL
  // CONNECTIVITY CONTROL. `cnt_o` IS `status_cnt_q` inside the FIFO, so if the
  // observation tap is wired up this is true by construction and must PASS.
  // If the tap is undriven — the defect that invalidated the first state_match
  // run — this FAILS immediately and loudly, instead of quietly turning the
  // lemma into an assumption about a floating wire.
  always @(posedge clk) if (settled) assert (dbg_cnt == cnt);
`endif

endmodule
