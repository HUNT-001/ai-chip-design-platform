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
  reg [31:0] PI_b;
  wire [0:0] PI_clk = clock;
  reg [6:0] PI_op;
  reg [31:0] PI_a;
  ibex_alu_fv UUT (
    .b(PI_b),
    .clk(PI_clk),
    .op(PI_op),
    .a(PI_a)
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
    // UUT.$auto$async2sync.\cc:107:execute$215  = 1'b0;
    // UUT.$auto$async2sync.\cc:116:execute$219  = 1'b1;
    UUT.dut.$auto$proc_rom.\cc:155:do_switch$143 [6'b000010] = 1'b0;
    UUT.dut.$auto$proc_rom.\cc:155:do_switch$143 [6'b000011] = 1'b0;

    // state 0
    PI_b = 32'b00110110010000010000011111110010;
    PI_op = 7'b0000010;
    PI_a = 32'b11001010111111101111000000001101;
  end
  always @(posedge clock) begin
    // state 1
    if (cycle == 0) begin
      PI_b <= 32'b00000000101000000100110000000101;
      PI_op <= 7'b0000011;
      PI_a <= 32'b10011100000001101010000000110010;
    end

    genclock <= cycle < 1;
    cycle <= cycle + 1;
  end
endmodule
