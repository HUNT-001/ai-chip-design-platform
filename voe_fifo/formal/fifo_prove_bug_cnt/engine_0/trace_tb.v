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
  reg [0:0] PI_flush_but_first;
  reg [0:0] PI_flush;
  reg [3:0] PI_din;
  reg [0:0] PI_testmode;
  reg [0:0] PI_pop;
  reg [0:0] PI_push;
  fifo_fv UUT (
    .clk(PI_clk),
    .flush_but_first(PI_flush_but_first),
    .flush(PI_flush),
    .din(PI_din),
    .testmode(PI_testmode),
    .pop(PI_pop),
    .push(PI_push)
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
    // UUT.$auto$async2sync.\cc:107:execute$239  = 1'b0;
    // UUT.$auto$async2sync.\cc:116:execute$243  = 1'b1;
    UUT.cyc = 2'b00;
    UUT.dut.u_fifo._witness_.anyinit_procdff_207 = 16'b0000000000000100;
    UUT.dut.u_fifo._witness_.anyinit_procdff_212 = 2'b01;
    UUT.dut.u_fifo._witness_.anyinit_procdff_217 = 2'b01;
    UUT.dut.u_fifo._witness_.anyinit_procdff_222 = 3'b000;

    // state 0
    PI_flush_but_first = 1'b0;
    PI_flush = 1'b0;
    PI_din = 4'b0001;
    PI_testmode = 1'b0;
    PI_pop = 1'b0;
    PI_push = 1'b1;
  end
  always @(posedge clock) begin
    // state 1
    if (cycle == 0) begin
      PI_flush_but_first <= 1'b0;
      PI_flush <= 1'b0;
      PI_din <= 4'b0000;
      PI_testmode <= 1'b0;
      PI_pop <= 1'b0;
      PI_push <= 1'b1;
    end

    // state 2
    if (cycle == 1) begin
      PI_flush_but_first <= 1'b0;
      PI_flush <= 1'b0;
      PI_din <= 4'b1101;
      PI_testmode <= 1'b0;
      PI_pop <= 1'b0;
      PI_push <= 1'b1;
    end

    // state 3
    if (cycle == 2) begin
      PI_flush_but_first <= 1'b0;
      PI_flush <= 1'b0;
      PI_din <= 4'b0000;
      PI_testmode <= 1'b0;
      PI_pop <= 1'b0;
      PI_push <= 1'b1;
    end

    // state 4
    if (cycle == 3) begin
      PI_flush_but_first <= 1'b0;
      PI_flush <= 1'b0;
      PI_din <= 4'b0000;
      PI_testmode <= 1'b0;
      PI_pop <= 1'b0;
      PI_push <= 1'b1;
    end

    // state 5
    if (cycle == 4) begin
      PI_flush_but_first <= 1'b0;
      PI_flush <= 1'b0;
      PI_din <= 4'b0000;
      PI_testmode <= 1'b0;
      PI_pop <= 1'b0;
      PI_push <= 1'b1;
    end

    // state 6
    if (cycle == 5) begin
      PI_flush_but_first <= 1'b1;
      PI_flush <= 1'b0;
      PI_din <= 4'b0000;
      PI_testmode <= 1'b0;
      PI_pop <= 1'b1;
      PI_push <= 1'b1;
    end

    // state 7
    if (cycle == 6) begin
      PI_flush_but_first <= 1'b0;
      PI_flush <= 1'b0;
      PI_din <= 4'b0000;
      PI_testmode <= 1'b0;
      PI_pop <= 1'b0;
      PI_push <= 1'b0;
    end

    genclock <= cycle < 7;
    cycle <= cycle + 1;
  end
endmodule
