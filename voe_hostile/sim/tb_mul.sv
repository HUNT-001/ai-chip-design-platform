// Self-checking testbench for the formal-hostile multiplier board.
// Simulation's advantage here is exactly the one it has in real verification:
// it cannot prove a multiplier correct, but it finds a dense defect in a
// handful of vectors while the solver is still bit-blasting.
`ifndef DUT
`define DUT mul_wrap
`endif
`ifndef NVEC
`define NVEC 20000
`endif

module tb_mul;
  logic [31:0] a, b;
  logic [63:0] y, g;
  int fails = 0;

  `DUT dut (.a(a), .b(b), .y(y));

  initial begin
    for (int i = 0; i < `NVEC; i++) begin
      a = $urandom();
      b = $urandom();
      #1;
      g = {32'd0, a} * {32'd0, b};
      if (y !== g) begin
        fails++;
        if (fails <= 5) $display("MISMATCH a=%08x b=%08x y=%016x g=%016x", a, b, y, g);
      end
    end
    if (fails == 0) $display("SIM_RESULT PASS n=%0d fails=0", `NVEC);
    else            $display("SIM_RESULT FAIL n=%0d fails=%0d", `NVEC, fails);
    $finish;
  end
endmodule
