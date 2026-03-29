
// ----------------------------------------
// A coverage module for the test input.

class input_coverage extends uvm_subscriber #(input_transaction);

   input_transaction tx;
   int tx_count;

   // Register with the factory.
   `uvm_component_utils(input_coverage)

   // Define cover group.
   // For now not covering data values.  Might be a gap.
   covergroup cov_operations;
      // 4 elements per group, only 3 are valid.
      // FIXME this would be a good application of ignore_bins if that was
      // a widely supported feature.
      coverpoint tx.opcode 
      {
         bins mul = {`MD_OP_MUL};
         bins div = {`MD_OP_DIV};
         bins rem = {`MD_OP_REM};
      }
      coverpoint tx.mux_select
      {
         bins lo  = {`MD_OUT_LO };
         bins hi  = {`MD_OUT_HI };
         bins rem = {`MD_OUT_REM};
      }

      // Cross opcodes with mux selects.
      ops_mux: cross tx.opcode, tx.mux_select;
   endgroup : cov_operations

   // Constructor, which also has to allocate objects.
   function new(string name, uvm_component parent);
      super.new(name, parent);
      cov_operations = new();
   endfunction : new

   // This write() method receives data from the subscriber port.
   // Some simulators require the argument to be named consistent with the
   // parent. 
   virtual function void write(input_transaction t);
      // Sample the transaction data.
      tx = t;
      // Since we don't have a sample event we have to do this manually.
      cov_operations.sample();
      // This is local action to count number of transactions.
      tx_count++;
   endfunction : write

   // Report summary info after simulation.
   function void report_phase(uvm_phase phase);
      `uvm_info(get_type_name(), $sformatf("Number of transactions = %0d", tx_count), UVM_LOW)
      `uvm_info(get_type_name(), $sformatf("Current coverage = %2.2f", cov_operations.get_coverage()), UVM_LOW)
   endfunction : report_phase

endclass : input_coverage
