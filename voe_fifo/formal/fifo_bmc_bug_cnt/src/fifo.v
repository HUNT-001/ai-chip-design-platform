module cv32e40p_fifo (
	clk_i,
	rst_ni,
	flush_i,
	flush_but_first_i,
	testmode_i,
	full_o,
	empty_o,
	cnt_o,
	data_i,
	push_i,
	data_o,
	pop_i
);
	reg _sv2v_0;
	parameter [0:0] FALL_THROUGH = 1'b0;
	parameter [31:0] DATA_WIDTH = 32;
	parameter [31:0] DEPTH = 8;
	parameter [31:0] ADDR_DEPTH = (DEPTH > 1 ? $clog2(DEPTH) : 1);
	input wire clk_i;
	input wire rst_ni;
	input wire flush_i;
	input wire flush_but_first_i;
	input wire testmode_i;
	output wire full_o;
	output wire empty_o;
	output wire [ADDR_DEPTH:0] cnt_o;
	input wire [DATA_WIDTH - 1:0] data_i;
	input wire push_i;
	output reg [DATA_WIDTH - 1:0] data_o;
	input wire pop_i;
	localparam [31:0] FIFO_DEPTH = (DEPTH > 0 ? DEPTH : 1);
	reg gate_clock;
	reg [ADDR_DEPTH - 1:0] read_pointer_n;
	reg [ADDR_DEPTH - 1:0] read_pointer_q;
	reg [ADDR_DEPTH - 1:0] write_pointer_n;
	reg [ADDR_DEPTH - 1:0] write_pointer_q;
	reg [ADDR_DEPTH:0] status_cnt_n;
	reg [ADDR_DEPTH:0] status_cnt_q;
	reg [(FIFO_DEPTH * DATA_WIDTH) - 1:0] mem_n;
	reg [(FIFO_DEPTH * DATA_WIDTH) - 1:0] mem_q;
	assign cnt_o = status_cnt_q;
	generate
		if (DEPTH == 0) begin : gen_zero_depth
			assign empty_o = ~push_i;
			assign full_o = ~pop_i;
		end
		else begin : gen_non_zero_depth
			assign full_o = status_cnt_q == FIFO_DEPTH[ADDR_DEPTH:0];
			assign empty_o = (status_cnt_q == 0) & ~(FALL_THROUGH & push_i);
		end
	endgenerate
	always @(*) begin : read_write_comb
		if (_sv2v_0)
			;
		read_pointer_n = read_pointer_q;
		write_pointer_n = write_pointer_q;
		status_cnt_n = status_cnt_q;
		data_o = (DEPTH == 0 ? data_i : mem_q[read_pointer_q * DATA_WIDTH+:DATA_WIDTH]);
		mem_n = mem_q;
		gate_clock = 1'b1;
		if (push_i && ~full_o) begin
			mem_n[write_pointer_q * DATA_WIDTH+:DATA_WIDTH] = data_i;
			gate_clock = 1'b0;
			if (write_pointer_q == (FIFO_DEPTH[ADDR_DEPTH - 1:0] - 1))
				write_pointer_n = 1'sb0;
			else
				write_pointer_n = write_pointer_q + 1;
			status_cnt_n = status_cnt_q + 1;
		end
		if (pop_i && ~empty_o) begin
			if (read_pointer_n == (FIFO_DEPTH[ADDR_DEPTH - 1:0] - 1))
				read_pointer_n = 1'sb0;
			else
				read_pointer_n = read_pointer_q + 1;
			status_cnt_n = status_cnt_q - 1;
		end
		if (((push_i && pop_i) && ~full_o) && ~empty_o)
			status_cnt_n = status_cnt_q;
		if ((FALL_THROUGH && (status_cnt_q == 0)) && push_i) begin
			data_o = data_i;
			if (pop_i) begin
				status_cnt_n = status_cnt_q;
				read_pointer_n = read_pointer_q;
				write_pointer_n = write_pointer_q;
			end
		end
	end
	always @(posedge clk_i or negedge rst_ni)
		if (~rst_ni) begin
			read_pointer_q <= 1'sb0;
			write_pointer_q <= 1'sb0;
			status_cnt_q <= 1'sb0;
		end
		else
			(* full_case, parallel_case *)
			case (1'b1)
				flush_i: begin
					read_pointer_q <= 1'sb0;
					write_pointer_q <= 1'sb0;
					status_cnt_q <= 1'sb0;
				end
				flush_but_first_i: begin
					read_pointer_q <= (status_cnt_q > 0 ? read_pointer_q : {ADDR_DEPTH {1'sb0}});
					write_pointer_q <= (status_cnt_q > 0 ? read_pointer_q + 1 : {ADDR_DEPTH {1'sb0}});
					status_cnt_q <= (status_cnt_q > 0 ? 1'b1 : {(ADDR_DEPTH >= 0 ? ADDR_DEPTH + 1 : 1 - ADDR_DEPTH) {1'sb0}});
				end
				default: begin
					read_pointer_q <= read_pointer_n;
					write_pointer_q <= write_pointer_n;
					status_cnt_q <= status_cnt_n;
				end
			endcase
	always @(posedge clk_i or negedge rst_ni)
		if (~rst_ni)
			mem_q <= 1'sb0;
		else if (!gate_clock)
			mem_q <= mem_n;
	initial _sv2v_0 = 0;
endmodule
module fifo_wrap (
	clk_i,
	rst_ni,
	flush_i,
	flush_but_first_i,
	testmode_i,
	full_o,
	empty_o,
	cnt_o,
	data_i,
	push_i,
	data_o,
	pop_i
);
	input wire clk_i;
	input wire rst_ni;
	input wire flush_i;
	input wire flush_but_first_i;
	input wire testmode_i;
	output wire full_o;
	output wire empty_o;
	output wire [2:0] cnt_o;
	input wire [3:0] data_i;
	input wire push_i;
	output wire [3:0] data_o;
	input wire pop_i;
	cv32e40p_fifo #(
		.FALL_THROUGH(1'b0),
		.DATA_WIDTH(4),
		.DEPTH(4)
	) u_fifo(
		.clk_i(clk_i),
		.rst_ni(rst_ni),
		.flush_i(flush_i),
		.flush_but_first_i(flush_but_first_i),
		.testmode_i(testmode_i),
		.full_o(full_o),
		.empty_o(empty_o),
		.cnt_o(cnt_o),
		.data_i(data_i),
		.push_i(push_i),
		.data_o(data_o),
		.pop_i(pop_i)
	);
endmodule
