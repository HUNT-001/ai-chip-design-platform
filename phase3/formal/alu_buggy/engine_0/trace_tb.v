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
  reg [31:0] PI_a;
  reg [2:0] PI_op;
  wire [0:0] PI_clk = clock;
  alu_fv UUT (
    .b(PI_b),
    .a(PI_a),
    .op(PI_op),
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
    // UUT.$auto$async2sync.\cc:107:execute$56  = 1'b0;
    // UUT.$auto$async2sync.\cc:116:execute$60  = 1'b1;

    // state 0
    PI_b = 32'b10011111010011010100110011110000;
    PI_a = 32'b11011110101011011011111011101111;
    PI_op = 3'b000;
  end
  always @(posedge clock) begin
    // state 1
    if (cycle == 0) begin
      PI_b <= 32'b00100001010100100100000100000010;
      PI_a <= 32'b11011110101011011011111011101111;
      PI_op <= 3'b000;
    end

    genclock <= cycle < 1;
    cycle <= cycle + 1;
  end
endmodule
