`ifndef VERILATOR
module testbench;
  reg [4095:0] vcdfile;
  reg clock;
`else
module testbench(input clock, output reg genclock);
  initial genclock = 1;
`endif
  reg genclock = 1;
  reg [31:0] cycle = 0;
  wire [0:0] PI_clk = clock;
  reg [0:0] PI_clear;
  reg [0:0] PI_sample;
  reg [0:0] PI_d;
  mvf_fv UUT (
    .clk(PI_clk),
    .clear(PI_clear),
    .sample(PI_sample),
    .d(PI_d)
  );
`ifndef VERILATOR
  initial begin
    if ($value$plusargs("vcd=%s", vcdfile)) begin
      $dumpfile(vcdfile);
      $dumpvars(0, testbench);
    end
    #5 clock = 0;
    while (genclock) begin
      #5 clock = 0;
      #5 clock = 1;
    end
  end
`endif
  initial begin
`ifndef VERILATOR
    #1;
`endif
    // UUT.$auto$async2sync.\cc:107:execute$76  = 1'b0;
    // UUT.$auto$async2sync.\cc:116:execute$80  = 1'b0;
    UUT.cyc = 2'b00;
    UUT.dut.u_filter._witness_.anyinit_procdff_65 = 4'b0000;
    UUT.dut.u_filter._witness_.anyinit_procdff_70 = 1'b0;
    UUT.p_clear = 1'b0;
    UUT.p_q = 1'b0;
    UUT.p_settled = 1'b0;

    // state 0
    PI_clear = 1'b0;
    PI_sample = 1'b0;
    PI_d = 1'b0;
  end
  always @(posedge clock) begin
    // state 1
    if (cycle == 0) begin
      PI_clear <= 1'b0;
      PI_sample <= 1'b1;
      PI_d <= 1'b1;
    end

    // state 2
    if (cycle == 1) begin
      PI_clear <= 1'b1;
      PI_sample <= 1'b1;
      PI_d <= 1'b0;
    end

    // state 3
    if (cycle == 2) begin
      PI_clear <= 1'b0;
      PI_sample <= 1'b1;
      PI_d <= 1'b1;
    end

    // state 4
    if (cycle == 3) begin
      PI_clear <= 1'b0;
      PI_sample <= 1'b0;
      PI_d <= 1'b1;
    end

    // state 5
    if (cycle == 4) begin
      PI_clear <= 1'b1;
      PI_sample <= 1'b0;
      PI_d <= 1'b0;
    end

    // state 6
    if (cycle == 5) begin
      PI_clear <= 1'b1;
      PI_sample <= 1'b1;
      PI_d <= 1'b1;
    end

    // state 7
    if (cycle == 6) begin
      PI_clear <= 1'b0;
      PI_sample <= 1'b0;
      PI_d <= 1'b0;
    end

    // state 8
    if (cycle == 7) begin
      PI_clear <= 1'b0;
      PI_sample <= 1'b1;
      PI_d <= 1'b1;
    end

    // state 9
    if (cycle == 8) begin
      PI_clear <= 1'b0;
      PI_sample <= 1'b0;
      PI_d <= 1'b0;
    end

    // state 10
    if (cycle == 9) begin
      PI_clear <= 1'b0;
      PI_sample <= 1'b1;
      PI_d <= 1'b1;
    end

    // state 11
    if (cycle == 10) begin
      PI_clear <= 1'b0;
      PI_sample <= 1'b1;
      PI_d <= 1'b1;
    end

    // state 12
    if (cycle == 11) begin
      PI_clear <= 1'b0;
      PI_sample <= 1'b1;
      PI_d <= 1'b1;
    end

    // state 13
    if (cycle == 12) begin
      PI_clear <= 1'b0;
      PI_sample <= 1'b0;
      PI_d <= 1'b0;
    end

    // state 14
    if (cycle == 13) begin
      PI_clear <= 1'b0;
      PI_sample <= 1'b0;
      PI_d <= 1'b0;
    end

    // state 15
    if (cycle == 14) begin
      PI_clear <= 1'b0;
      PI_sample <= 1'b1;
      PI_d <= 1'b1;
    end

    // state 16
    if (cycle == 15) begin
      PI_clear <= 1'b0;
      PI_sample <= 1'b1;
      PI_d <= 1'b1;
    end

    // state 17
    if (cycle == 16) begin
      PI_clear <= 1'b0;
      PI_sample <= 1'b1;
      PI_d <= 1'b1;
    end

    // state 18
    if (cycle == 17) begin
      PI_clear <= 1'b0;
      PI_sample <= 1'b1;
      PI_d <= 1'b1;
    end

    // state 19
    if (cycle == 18) begin
      PI_clear <= 1'b0;
      PI_sample <= 1'b1;
      PI_d <= 1'b1;
    end

    // state 20
    if (cycle == 19) begin
      PI_clear <= 1'b0;
      PI_sample <= 1'b1;
      PI_d <= 1'b1;
    end

    // state 21
    if (cycle == 20) begin
      PI_clear <= 1'b0;
      PI_sample <= 1'b1;
      PI_d <= 1'b1;
    end

    // state 22
    if (cycle == 21) begin
      PI_clear <= 1'b0;
      PI_sample <= 1'b1;
      PI_d <= 1'b0;
    end

    // state 23
    if (cycle == 22) begin
      PI_clear <= 1'b1;
      PI_sample <= 1'b1;
      PI_d <= 1'b1;
    end

    // state 24
    if (cycle == 23) begin
      PI_clear <= 1'b0;
      PI_sample <= 1'b0;
      PI_d <= 1'b0;
    end

    genclock <= cycle < 24;
    cycle <= cycle + 1;
  end
endmodule
