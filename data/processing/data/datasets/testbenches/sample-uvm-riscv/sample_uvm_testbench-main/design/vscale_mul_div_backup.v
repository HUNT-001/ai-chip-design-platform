//
// gkemp note - this file came from vscale with no comments.  
// Added comments are mine.  File functionality not changed.
// 

// ------------------------------------------------------------------------------
// Include defined values.  

`include "vscale_md_constants.vh"
`include "vscale_ctrl_constants.vh"
`include "rv32_opcodes.vh"

// ------------------------------------------------------------------------------
// Module interface.  Descriptive comments added.

module vscale_mul_div(
                      // Clock and reset from system.
                      input                         clk,
                      input                         reset,
                      // Valid bit for "req" inputs.
                      input                         req_valid,
                      // Unit ready to receive a request
                      output                        req_ready,
                      // Are inputs signed?  It's integer arithmetic, no explicit sign bit.
                      input                         req_in_1_signed,
                      input                         req_in_2_signed,
                      // Opcode
                      input [`MD_OP_WIDTH-1:0]      req_op,
                      // Trying to get 64 bits out on a 32-bit bus.  This controls the output mux.
                      input [`MD_OUT_SEL_WIDTH-1:0] req_out_sel,
                      // Next two signals are the operands.
                      input [`XPR_LEN-1:0]          req_in_1,
                      input [`XPR_LEN-1:0]          req_in_2,
                      // Valid bit on output, necessary on a variable-latency operation.
                      output                        resp_valid,
                      // Arithmetic result.  
                      output [`XPR_LEN-1:0]         resp_result
                      );

// ------------------------------------------------------------------------------
// local parameters, should have been an enumerated data type in SystemVerilog.

   localparam md_state_width = 2;
   localparam s_idle = 0;
   localparam s_compute = 1;
   localparam s_setup_output = 2;
   localparam s_done = 3;

// ------------------------------------------------------------------------------
// local signals.

   reg [md_state_width-1:0]                         state;
   reg [md_state_width-1:0]                         next_state;
   reg [`MD_OP_WIDTH-1:0]                           op;
   reg [`MD_OUT_SEL_WIDTH-1:0]                      out_sel;
   reg                                              negate_output;
   reg [`DOUBLE_XPR_LEN-1:0]                        a;
   reg [`DOUBLE_XPR_LEN-1:0]                        b;
   reg [`LOG2_XPR_LEN-1:0]                          counter;
   reg [`DOUBLE_XPR_LEN-1:0]                        result;

   wire [`XPR_LEN-1:0]                              abs_in_1;
   wire                                             sign_in_1;
   wire [`XPR_LEN-1:0]                              abs_in_2;
   wire                                             sign_in_2;

   wire                                             a_geq;
   wire [`DOUBLE_XPR_LEN-1:0]                       result_muxed;
   wire [`DOUBLE_XPR_LEN-1:0]                       result_muxed_negated;
   wire [`XPR_LEN-1:0]                              final_result;

// ------------------------------------------------------------------------------
// function returns absolute value of input.

   function [`XPR_LEN-1:0] abs_input;
      input [`XPR_LEN-1:0]                          data;
      input                                         is_signed;
      begin
         abs_input = (data[`XPR_LEN-1] == 1'b1 && is_signed) ? -data : data;
      end
   endfunction // if

// ------------------------------------------------------------------------------
// random logic.

   assign req_ready = (state == s_idle);
   assign resp_valid = (state == s_done);
   assign resp_result = result[`XPR_LEN-1:0];

   assign abs_in_1 = abs_input(req_in_1,req_in_1_signed);
   assign sign_in_1 = req_in_1_signed && req_in_1[`XPR_LEN-1];
   assign abs_in_2 = abs_input(req_in_2,req_in_2_signed);
   assign sign_in_2 = req_in_2_signed && req_in_2[`XPR_LEN-1];

   assign a_geq = a >= b;
   assign result_muxed = (out_sel == `MD_OUT_REM) ? a : result;
   assign result_muxed_negated = (negate_output) ? -result_muxed : result_muxed;
   assign final_result = (out_sel == `MD_OUT_HI) ? result_muxed_negated[`XPR_LEN+:`XPR_LEN] : result_muxed_negated[0+:`XPR_LEN];

// ------------------------------------------------------------------------------

// a resetting D flip-flop.

   always @(posedge clk) begin
      if (reset) begin
         state <= s_idle;
      end else begin
         state <= next_state;
      end
   end

// SystemVerilog's always_ff is a better choice but not Verilog compliant.
// This is the state machine for the divider.

   always @(*) begin
      case (state)
        s_idle : next_state = (req_valid) ? s_compute : s_idle;
        s_compute : next_state = (counter == 0) ? s_setup_output : s_compute;
        s_setup_output : next_state = s_done;
        s_done : next_state = s_idle;
        default : next_state = s_idle;
      endcase // case (state)
   end

// This is the multiplier/divider itself.

   always @(posedge clk) begin
      case (state)
        s_idle : begin
           if (reset) begin  // gkemp FIXME this is a bug fix.
              op <= '0;
              out_sel <= '0;
           end
           else if (req_valid) begin  // accept valid request
              result <= 0;
              a <= {`XPR_LEN'b0,abs_in_1};
              b <= {abs_in_2,`XPR_LEN'b0} >> 1;
              // gkemp FIXME Verilog race condition on next line, replaced op with req_op to fix.
              negate_output <= (req_op == `MD_OP_REM) ? sign_in_1 : sign_in_1 ^ sign_in_2;
              out_sel <= req_out_sel;
              op <= req_op;
              counter <= `XPR_LEN - 1;
           end  // Else, nothing?  No, state machine stays idle.
        end
        s_compute : begin
           counter <= counter - 1;
           b <= b >> 1;
           if (op == `MD_OP_MUL) begin
              if (a[counter]) begin
                 result <= result + b;
              end
           end else begin
              b <= b >> 1;
              if (a_geq) begin
                 a <= a - b;
                 result <= (`DOUBLE_XPR_LEN'b1 << counter) | result;
              end
           end
        end // case: s_compute
        s_setup_output : begin
           result <= {`XPR_LEN'b0,final_result};
        end
      endcase // case (state)
   end // always @ (posedge clk)

endmodule // vscale_mul_div

