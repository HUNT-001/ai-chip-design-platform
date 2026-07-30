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
  reg [2:0] PI_op;
  reg [31:0] PI_a;
  reg [31:0] PI_b;
  wire [0:0] PI_clk = clock;
  alu_fv UUT (
    .op(PI_op),
    .a(PI_a),
    .b(PI_b),
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
    // UUT.$auto$async2sync.\cc:116:execute$60  = 1'b0;

    // state 0
    PI_op = 3'b001;
    PI_a = 32'b10100000010010001000001001000000;
    PI_b = 32'b01101001110110110110011011011100;
  end
  always @(posedge clock) begin
    // state 1
    if (cycle == 0) begin
      PI_op <= 3'b000;
      PI_a <= 32'b11011110101011011011111011101111;
      PI_b <= 32'b00100001010100100100000100000010;
    end

    // state 2
    if (cycle == 1) begin
      PI_op <= 3'b010;
      PI_a <= 32'b11011110101011011011111011101111;
      PI_b <= 32'b00011111001111000010110011111001;
    end

    genclock <= cycle < 2;
    cycle <= cycle + 1;
  end
endmodule
