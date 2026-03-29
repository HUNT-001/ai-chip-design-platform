// ----------------------------------------
// A scoreboard to check results.

class muldiv_scoreboard extends uvm_scoreboard;
   // An object to receive data from the agent's analysis port.
   uvm_tlm_analysis_fifo #(input_transaction)  tx_in_collected;
   uvm_tlm_analysis_fifo #(output_transaction) tx_out_collected;
   // Something to hold transactions.
   input_transaction input_tx;
   output_transaction output_tx;

   // Always, register with the factory.
   `uvm_component_utils(muldiv_scoreboard)

   // Stock constructor.
   function new(string name, uvm_component parent);
      super.new(name, parent);
   endfunction

   // Build-phase function to create objects.
   function void build_phase(uvm_phase phase);
      super.build_phase(phase);
      tx_in_collected = new("tx_in_collected", this);
      tx_out_collected = new("tx_out_collected", this);
      input_tx = input_transaction::type_id::create("input_tx");
      output_tx = output_transaction::type_id::create("output_tx");
      `uvm_info(get_full_name(), "Build phase complete.", UVM_LOW)
   endfunction : build_phase

   // Run-phase task only starts the data collection process.
   virtual task run_phase(uvm_phase phase);
      super.run_phase(phase);  // who knows what your parents are up to?
      watcher();
   endtask : run_phase

   // Get data from the analysis ports (plural) and run it through the checker.
   virtual task watcher();
      forever begin 
         tx_in_collected.get(input_tx);
         tx_out_collected.get(output_tx);
         compare_data();
      end
   endtask : watcher

   function [`XPR_LEN-1:0] get_absolute_value(logic [`XPR_LEN-1:0] operand, bit sign);
      // sign : 0 is positive, 1 is negative
      get_absolute_value = (sign) ? (~(operand - 1)) : operand;
   endfunction : get_absolute_value

   // Here, finally, we compare what we saw with what we expected.
   virtual task compare_data();
      logic [`DOUBLE_XPR_LEN-1:0] full_result;
      logic [`XPR_LEN-1:0] my_op1;
      logic [`XPR_LEN-1:0] my_op2;

      // Used for human-readable error reporting.
      string my_opcode;
      string my_round;

      // Get absolute values of inputs.
      my_op1 = get_absolute_value(input_tx.op1, input_tx.op1_sign);
      my_op2 = get_absolute_value(input_tx.op2, input_tx.op2_sign);

      // Predict value based on opcodes and operand signs.
      // Signed operations are not consistent.  REM is weird.
      case (input_tx.opcode)
         `MD_OP_MUL: begin
               full_result = my_op1 * my_op2;
               // Now factor in the signs.  If signed, and signs aren't the 
               // same, find 2s complement of full result.
               if ((input_tx.op1_signedp && input_tx.op2_signedp) && (input_tx.op1_sign != input_tx.op2_sign)) 
                  full_result = ~full_result + 1;
               my_opcode = "MD_OP_MUL";
            end
         `MD_OP_DIV: begin
               full_result = my_op1 / my_op2;
               // Now factor in the signs.  If signed, and signs aren't the 
               // same, find 2s complement of full result.
               if ((input_tx.op1_signedp && input_tx.op2_signedp) && (input_tx.op1_sign != input_tx.op2_sign)) 
                  full_result = ~full_result + 1;
               my_opcode = "MD_OP_DIV";
            end
         `MD_OP_REM: begin
               full_result = my_op1 % my_op2;
               // If dividend is negative, this implementation produces a 
               // negative remainder.  Seems wrong but definion allows it.
               if (input_tx.op1_signedp && input_tx.op1_sign) begin
                  full_result = (~full_result) + 1;
               end 
               my_opcode = "MD_OP_REM";
            end
         default: begin
            // If we're here we saw an undefined opcode.
            `uvm_error(get_full_name(), $sformatf("Unknown opcode %x seen.", input_tx.opcode))
            end
      endcase

      // Compare observed result based on mux setting.
      // Four-state compare to guard against unknowns.
      case (input_tx.mux_select)
         `MD_OUT_LO: begin
            my_round = "MD_OUT_LO";
            if (full_result[`XPR_LEN-1:0] !== output_tx.result) begin
               `uvm_error(get_full_name(), $sformatf("Expected value %x does not match observed value %x.  Opcode %s, round mode %s.", full_result[`XPR_LEN-1:0], output_tx.result, my_opcode, my_round))
               input_tx.print_full();
            end
         end
         `MD_OUT_HI: begin
            my_round = "MD_OUT_HI";
            if (full_result[`DOUBLE_XPR_LEN-1:`XPR_LEN] !== output_tx.result) begin
               `uvm_error(get_full_name(), $sformatf("Expected value %x does not match observed value %x.  Opcode %s, round mode %s.", full_result[`DOUBLE_XPR_LEN-1:`XPR_LEN], output_tx.result, my_opcode, my_round))
               input_tx.print_full();
            end
         end
         `MD_OUT_REM: begin
            my_round = "MD_OUT_REM";
            if (full_result[`XPR_LEN-1:0] !== output_tx.result) begin
               `uvm_error(get_full_name(), $sformatf("Expected value %x does not match observed value %x.  Opcode %s, round mode %s.", full_result[`XPR_LEN-1:0], output_tx.result, my_opcode, my_round))
               input_tx.print_full();
            end
         end
      endcase

   endtask : compare_data

endclass : muldiv_scoreboard