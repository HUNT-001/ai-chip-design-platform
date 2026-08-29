module Multiplier (
	srcA,
	srcB,
	dst
);
	parameter BIT_WIDTH = 32;
	input wire signed [BIT_WIDTH - 1:0] srcA;
	input wire signed [BIT_WIDTH - 1:0] srcB;
	output wire signed [(2 * BIT_WIDTH) - 1:0] dst;
	assign dst = srcA * srcB;
endmodule
module Multiplier_mut (
	srcA,
	srcB,
	dst
);
	parameter BIT_WIDTH = 32;
	input wire signed [BIT_WIDTH - 1:0] srcA;
	input wire signed [BIT_WIDTH - 1:0] srcB;
	output wire signed [(2 * BIT_WIDTH) - 1:0] dst;
	wire signed [(2 * BIT_WIDTH) - 1:0] raw;
	assign raw = srcA * srcB;
	assign dst = (srcB[3:0] == 4'hf ? raw ^ 64'h0000000000001000 : raw);
endmodule
module mul_wrap (
	a,
	b,
	y
);
	input wire [31:0] a;
	input wire [31:0] b;
	output wire [63:0] y;
	Multiplier #(.BIT_WIDTH(32)) u_mul(
		.srcA(a),
		.srcB(b),
		.dst(y)
	);
endmodule
module mul_wrap_mut (
	a,
	b,
	y
);
	input wire [31:0] a;
	input wire [31:0] b;
	output wire [63:0] y;
	Multiplier_mut #(.BIT_WIDTH(32)) u_mul(
		.srcA(a),
		.srcB(b),
		.dst(y)
	);
endmodule
