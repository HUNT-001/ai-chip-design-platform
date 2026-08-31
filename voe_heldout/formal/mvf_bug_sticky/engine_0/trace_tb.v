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
  reg [0:0] PI_d;
  wire [0:0] PI_clk = clock;
  reg [0:0] PI_clear;
  reg [0:0] PI_sample;
  mvf_fv UUT (
    .d(PI_d),
    .clk(PI_clk),
    .clear(PI_clear),
    .sample(PI_sample)
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
    // UUT.$auto$async2sync.\cc:116:execute$80  = 1'b1;
    UUT.cyc = 2'b00;
    UUT.dut.u_filter._witness_.anyinit_procdff_65 = 4'b0001;
    UUT.dut.u_filter._witness_.anyinit_procdff_70 = 1'b0;
    UUT.p_clear = 1'b0;
    UUT.p_q = 1'b0;
    UUT.p_settled = 1'b1;

    // state 0
    PI_d = 1'b0;
    PI_clear = 1'b0;
    PI_sample = 1'b0;
  end
  always @(posedge clock) begin
    // state 1
    if (cycle == 0) begin
      PI_d <= 1'b1;
      PI_clear <= 1'b0;
      PI_sample <= 1'b1;
    end

    // state 2
    if (cycle == 1) begin
      PI_d <= 1'b1;
      PI_clear <= 1'b0;
      PI_sample <= 1'b1;
    end

    // state 3
    if (cycle == 2) begin
      PI_d <= 1'b1;
      PI_clear <= 1'b0;
      PI_sample <= 1'b1;
    end

    // state 4
    if (cycle == 3) begin
      PI_d <= 1'b1;
      PI_clear <= 1'b0;
      PI_sample <= 1'b1;
    end

    // state 5
    if (cycle == 4) begin
      PI_d <= 1'b1;
      PI_clear <= 1'b0;
      PI_sample <= 1'b1;
    end

    // state 6
    if (cycle == 5) begin
      PI_d <= 1'b1;
      PI_clear <= 1'b0;
      PI_sample <= 1'b1;
    end

    // state 7
    if (cycle == 6) begin
      PI_d <= 1'b1;
      PI_clear <= 1'b0;
      PI_sample <= 1'b1;
    end

    // state 8
    if (cycle == 7) begin
      PI_d <= 1'b1;
      PI_clear <= 1'b0;
      PI_sample <= 1'b1;
    end

    // state 9
    if (cycle == 8) begin
      PI_d <= 1'b1;
      PI_clear <= 1'b0;
      PI_sample <= 1'b1;
    end

    // state 10
    if (cycle == 9) begin
      PI_d <= 1'b1;
      PI_clear <= 1'b0;
      PI_sample <= 1'b1;
    end

    // state 11
    if (cycle == 10) begin
      PI_d <= 1'b1;
      PI_clear <= 1'b0;
      PI_sample <= 1'b1;
    end

    // state 12
    if (cycle == 11) begin
      PI_d <= 1'b0;
      PI_clear <= 1'b0;
      PI_sample <= 1'b1;
    end

    // state 13
    if (cycle == 12) begin
      PI_d <= 1'b1;
      PI_clear <= 1'b0;
      PI_sample <= 1'b1;
    end

    // state 14
    if (cycle == 13) begin
      PI_d <= 1'b0;
      PI_clear <= 1'b0;
      PI_sample <= 1'b0;
    end

    genclock <= cycle < 14;
    cycle <= cycle + 1;
  end
endmodule
