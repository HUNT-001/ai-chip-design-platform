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
  reg [0:0] PI_testmode;
  reg [3:0] PI_din;
  reg [0:0] PI_flush;
  wire [0:0] PI_clk = clock;
  reg [0:0] PI_flush_but_first;
  reg [0:0] PI_pop;
  reg [0:0] PI_push;
  fifo_fv UUT (
    .testmode(PI_testmode),
    .din(PI_din),
    .flush(PI_flush),
    .clk(PI_clk),
    .flush_but_first(PI_flush_but_first),
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
    // UUT.$auto$async2sync.\cc:107:execute$384  = 1'b0;
    // UUT.$auto$async2sync.\cc:107:execute$390  = 1'b0;
    // UUT.$auto$async2sync.\cc:116:execute$388  = 1'b1;
    // UUT.$auto$async2sync.\cc:116:execute$394  = 1'b1;
    UUT.cyc = 2'b00;
    UUT.dut.u_fifo._witness_.anyinit_procdff_346 = 16'b0000000000000001;
    UUT.dut.u_fifo._witness_.anyinit_procdff_351 = 2'b01;
    UUT.dut.u_fifo._witness_.anyinit_procdff_356 = 2'b01;
    UUT.dut.u_fifo._witness_.anyinit_procdff_361 = 3'b000;
    UUT.s_cnt = 3'b001;
    UUT.s_rd = 2'b01;
    UUT.s_wr = 2'b01;
    UUT.shadow[2'b01] = 4'b0001;
    UUT.shadow[2'b00] = 4'b0000;

    // state 0
    PI_testmode = 1'b0;
    PI_din = 4'b0001;
    PI_flush = 1'b0;
    PI_flush_but_first = 1'b0;
    PI_pop = 1'b0;
    PI_push = 1'b1;
  end
  always @(posedge clock) begin
    // state 1
    if (cycle == 0) begin
      PI_testmode <= 1'b0;
      PI_din <= 4'b1111;
      PI_flush <= 1'b0;
      PI_flush_but_first <= 1'b0;
      PI_pop <= 1'b0;
      PI_push <= 1'b1;
    end

    // state 2
    if (cycle == 1) begin
      PI_testmode <= 1'b0;
      PI_din <= 4'b0000;
      PI_flush <= 1'b0;
      PI_flush_but_first <= 1'b0;
      PI_pop <= 1'b0;
      PI_push <= 1'b1;
    end

    // state 3
    if (cycle == 2) begin
      PI_testmode <= 1'b0;
      PI_din <= 4'b0000;
      PI_flush <= 1'b0;
      PI_flush_but_first <= 1'b0;
      PI_pop <= 1'b0;
      PI_push <= 1'b1;
    end

    // state 4
    if (cycle == 3) begin
      PI_testmode <= 1'b0;
      PI_din <= 4'b0100;
      PI_flush <= 1'b0;
      PI_flush_but_first <= 1'b0;
      PI_pop <= 1'b0;
      PI_push <= 1'b1;
    end

    // state 5
    if (cycle == 4) begin
      PI_testmode <= 1'b0;
      PI_din <= 4'b0000;
      PI_flush <= 1'b0;
      PI_flush_but_first <= 1'b1;
      PI_pop <= 1'b1;
      PI_push <= 1'b1;
    end

    // state 6
    if (cycle == 5) begin
      PI_testmode <= 1'b0;
      PI_din <= 4'b0000;
      PI_flush <= 1'b0;
      PI_flush_but_first <= 1'b0;
      PI_pop <= 1'b1;
      PI_push <= 1'b0;
    end

    // state 7
    if (cycle == 6) begin
      PI_testmode <= 1'b0;
      PI_din <= 4'b0000;
      PI_flush <= 1'b0;
      PI_flush_but_first <= 1'b0;
      PI_pop <= 1'b0;
      PI_push <= 1'b0;
    end

    genclock <= cycle < 7;
    cycle <= cycle + 1;
  end
endmodule
