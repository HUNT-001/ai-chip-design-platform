// Verilator self-checking testbench for the Phase-3 ALU DUT.
// Drives random vectors, compares against an in-TB golden, and prints ONE
// machine-parseable result line the SimChannel adapter reads:
//     SIM_RESULT PASS n=<N> fails=0
//     SIM_RESULT FAIL n=<N> fails=<F>
// Build good/buggy variants by defining INJECT_BUG at compile time.
//   good : verilator --binary --timing ... rtl/alu.sv sim/tb_alu.sv
//   buggy: add  -DINJECT_BUG=1
`ifndef INJECT_BUG
`define INJECT_BUG 0
`endif
`ifndef NVEC
`define NVEC 20000
`endif

module tb_alu;
  logic [2:0]  op;
  logic [31:0] a, b, y, g;
  int          fails = 0;
  int          seed  = 1;

  alu #(.INJECT_BUG(`INJECT_BUG)) dut (.op_i(op), .a_i(a), .b_i(b), .y_o(y));

  function automatic logic [31:0] golden(input logic [2:0] o,
                                         input logic [31:0] x,
                                         input logic [31:0] z);
    unique case (o)
      3'd0:    golden = x + z;
      3'd1:    golden = x - z;
      3'd2:    golden = x & z;
      3'd3:    golden = x | z;
      3'd4:    golden = x ^ z;
      3'd5:    golden = {31'b0, ($signed(x) < $signed(z))};
      3'd6:    golden = {31'b0, (x < z)};
      default: golden = x;
    endcase
  endfunction

  initial begin
    if ($value$plusargs("seed=%d", seed)) void'($urandom(seed));
    for (int i = 0; i < `NVEC; i++) begin
      op = $urandom() % 8;
      a  = $urandom();
      b  = $urandom();
      #1;
      g = golden(op, a, b);
      if (y !== g) begin
        fails++;
        if (fails <= 5)
          $display("MISMATCH op=%0d a=%08x b=%08x y=%08x g=%08x", op, a, b, y, g);
      end
    end
    if (fails == 0) $display("SIM_RESULT PASS n=%0d fails=0", `NVEC);
    else            $display("SIM_RESULT FAIL n=%0d fails=%0d", `NVEC, fails);
    $finish;
  end
endmodule
