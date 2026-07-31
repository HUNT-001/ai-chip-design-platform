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
  reg [31:0] PI_a;
  reg [6:0] PI_op;
  wire [0:0] PI_clk = clock;
  reg [31:0] PI_b;
  ibex_alu_fv UUT (
    .a(PI_a),
    .op(PI_op),
    .clk(PI_clk),
    .b(PI_b)
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
    // UUT.$auto$async2sync.\cc:107:execute$163  = 1'b0;
    // UUT.$auto$async2sync.\cc:116:execute$167  = 1'b1;
    UUT.dut.$auto$proc_rom.\cc:155:do_switch$92 [6'b001000] = 1'b0;
    UUT.dut.$auto$proc_rom.\cc:155:do_switch$92 [6'b001001] = 1'b0;

    // state 0
    PI_a = 32'b11111111111111111111111111111111;
    PI_op = 7'b0001000;
    PI_b = 32'b11100110110111100100100000110000;
  end
  always @(posedge clock) begin
    // state 1
    if (cycle == 0) begin
      PI_a <= 32'b01111111111111111111111111111111;
      PI_op <= 7'b0001001;
      PI_b <= 32'b01111111111111111111111111111111;
    end

    genclock <= cycle < 1;
    cycle <= cycle + 1;
  end
endmodule
