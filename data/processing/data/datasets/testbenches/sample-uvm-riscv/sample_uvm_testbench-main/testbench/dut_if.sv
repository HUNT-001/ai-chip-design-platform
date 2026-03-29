
// ------------------------------------------------------------------------------
// An interface for the DUT.  DUT doesn't need it.  But it makes it so much more
// convenient to connect UVM objects to the interface.

interface dut_if;
   // Clock and reset from system.
   logic                         clk;
   logic                         reset;
   // Valid bit for "req" logics.
   logic                         req_valid;
   // Unit ready to receive a request.
   logic                         req_ready;
   // Are logics signed?  It's integer arithmetic, no explicit sign bit.
   logic                         req_in_1_signed;
   logic                         req_in_2_signed;
   // Opcode
   logic [`MD_OP_WIDTH-1:0]      req_op;
   // Trying to get 64 bits out on a 32-bit bus.  This controls the output mux.
   logic [`MD_OUT_SEL_WIDTH-1:0] req_out_sel;
   // Next two signals are the operands.
   logic [`XPR_LEN-1:0]          req_in_1;
   logic [`XPR_LEN-1:0]          req_in_2;
   // Valid bit on output, necessary on a variable-latency operation.
   logic                        resp_valid;
   // Arithmetic result.  
   logic [`XPR_LEN-1:0]         resp_result;
endinterface : dut_if
