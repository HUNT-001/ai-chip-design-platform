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
  reg [0:0] PI_flush_but_first;
  reg [3:0] PI_din;
  reg [0:0] PI_flush;
  reg [0:0] PI_push;
  reg [0:0] PI_pop;
  reg [0:0] PI_testmode;
  wire [0:0] PI_clk = clock;
  fifo_fv UUT (
    .flush_but_first(PI_flush_but_first),
    .din(PI_din),
    .flush(PI_flush),
    .push(PI_push),
    .pop(PI_pop),
    .testmode(PI_testmode),
    .clk(PI_clk)
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
    // UUT.$auto$async2sync.\cc:116:execute$243  = 1'b0;
    UUT.cyc = 2'b01;
    UUT.dut.u_fifo._witness_.anyinit_procdff_207 = 16'b1000000000100000;
    UUT.dut.u_fifo._witness_.anyinit_procdff_212 = 2'b00;
    UUT.dut.u_fifo._witness_.anyinit_procdff_217 = 2'b01;
    UUT.dut.u_fifo._witness_.anyinit_procdff_222 = 3'b011;

    // state 0
    PI_flush_but_first = 1'b0;
    PI_din = 4'b0000;
    PI_flush = 1'b0;
    PI_push = 1'b1;
    PI_pop = 1'b0;
    PI_testmode = 1'b0;
  end
  always @(posedge clock) begin
    // state 1
    if (cycle == 0) begin
      PI_flush_but_first <= 1'b0;
      PI_din <= 4'b0000;
      PI_flush <= 1'b0;
      PI_push <= 1'b0;
      PI_pop <= 1'b0;
      PI_testmode <= 1'b0;
    end

    // state 2
    if (cycle == 1) begin
      PI_flush_but_first <= 1'b1;
      PI_din <= 4'b1000;
      PI_flush <= 1'b0;
      PI_push <= 1'b0;
      PI_pop <= 1'b1;
      PI_testmode <= 1'b0;
    end

    // state 3
    if (cycle == 2) begin
      PI_flush_but_first <= 1'b0;
      PI_din <= 4'b0000;
      PI_flush <= 1'b0;
      PI_push <= 1'b1;
      PI_pop <= 1'b0;
      PI_testmode <= 1'b0;
    end

    // state 4
    if (cycle == 3) begin
      PI_flush_but_first <= 1'b0;
      PI_din <= 4'b0000;
      PI_flush <= 1'b0;
      PI_push <= 1'b0;
      PI_pop <= 1'b0;
      PI_testmode <= 1'b0;
    end

    // state 5
    if (cycle == 4) begin
      PI_flush_but_first <= 1'b0;
      PI_din <= 4'b0000;
      PI_flush <= 1'b0;
      PI_push <= 1'b1;
      PI_pop <= 1'b0;
      PI_testmode <= 1'b0;
    end

    // state 6
    if (cycle == 5) begin
      PI_flush_but_first <= 1'b0;
      PI_din <= 4'b0000;
      PI_flush <= 1'b0;
      PI_push <= 1'b1;
      PI_pop <= 1'b1;
      PI_testmode <= 1'b0;
    end

    // state 7
    if (cycle == 6) begin
      PI_flush_but_first <= 1'b0;
      PI_din <= 4'b0100;
      PI_flush <= 1'b0;
      PI_push <= 1'b1;
      PI_pop <= 1'b1;
      PI_testmode <= 1'b0;
    end

    // state 8
    if (cycle == 7) begin
      PI_flush_but_first <= 1'b0;
      PI_din <= 4'b0000;
      PI_flush <= 1'b0;
      PI_push <= 1'b1;
      PI_pop <= 1'b0;
      PI_testmode <= 1'b0;
    end

    // state 9
    if (cycle == 8) begin
      PI_flush_but_first <= 1'b0;
      PI_din <= 4'b0010;
      PI_flush <= 1'b0;
      PI_push <= 1'b1;
      PI_pop <= 1'b1;
      PI_testmode <= 1'b0;
    end

    // state 10
    if (cycle == 9) begin
      PI_flush_but_first <= 1'b0;
      PI_din <= 4'b0001;
      PI_flush <= 1'b0;
      PI_push <= 1'b1;
      PI_pop <= 1'b0;
      PI_testmode <= 1'b0;
    end

    // state 11
    if (cycle == 10) begin
      PI_flush_but_first <= 1'b0;
      PI_din <= 4'b0000;
      PI_flush <= 1'b1;
      PI_push <= 1'b0;
      PI_pop <= 1'b0;
      PI_testmode <= 1'b0;
    end

    // state 12
    if (cycle == 11) begin
      PI_flush_but_first <= 1'b0;
      PI_din <= 4'b0000;
      PI_flush <= 1'b0;
      PI_push <= 1'b0;
      PI_pop <= 1'b0;
      PI_testmode <= 1'b0;
    end

    genclock <= cycle < 12;
    cycle <= cycle + 1;
  end
endmodule
