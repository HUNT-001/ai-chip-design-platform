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
  reg [0:0] PI_en;
  lfsr_fv UUT (
    .clk(PI_clk),
    .en(PI_en)
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
    // UUT.$auto$async2sync.\cc:107:execute$73  = 1'b0;
    // UUT.$auto$async2sync.\cc:116:execute$71  = 1'b0;
    // UUT.$auto$async2sync.\cc:116:execute$77  = 1'b0;
    UUT.cyc = 2'b01;
    UUT.dut.u_lfsr._witness_.anyinit_procdff_62 = 8'b10100000;

    // state 0
    PI_en = 1'b0;
  end
  always @(posedge clock) begin
    // state 1
    if (cycle == 0) begin
      PI_en <= 1'b0;
    end

    // state 2
    if (cycle == 1) begin
      PI_en <= 1'b0;
    end

    // state 3
    if (cycle == 2) begin
      PI_en <= 1'b1;
    end

    // state 4
    if (cycle == 3) begin
      PI_en <= 1'b0;
    end

    // state 5
    if (cycle == 4) begin
      PI_en <= 1'b0;
    end

    // state 6
    if (cycle == 5) begin
      PI_en <= 1'b0;
    end

    // state 7
    if (cycle == 6) begin
      PI_en <= 1'b0;
    end

    // state 8
    if (cycle == 7) begin
      PI_en <= 1'b0;
    end

    // state 9
    if (cycle == 8) begin
      PI_en <= 1'b0;
    end

    // state 10
    if (cycle == 9) begin
      PI_en <= 1'b1;
    end

    // state 11
    if (cycle == 10) begin
      PI_en <= 1'b0;
    end

    // state 12
    if (cycle == 11) begin
      PI_en <= 1'b0;
    end

    genclock <= cycle < 12;
    cycle <= cycle + 1;
  end
endmodule
