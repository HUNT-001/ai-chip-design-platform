// ----------------------------------------
// Monitor for output transactions.

class output_monitor extends uvm_monitor;

   // Register with factory.
   `uvm_component_utils(output_monitor)

   // Virtual interface.
   virtual dut_if dif;

   // Need an analysis port to broadcast what we find.
   uvm_analysis_port #(output_transaction) tx_collection_port;

   // Containers for observed transactions.
   output_transaction tx_collected;
   output_transaction tx_clone;

   // constructor
   function new (string name, uvm_component parent);
      super.new(name, parent);
   endfunction : new

   // build operations
   function void build_phase(uvm_phase phase);
      // parent builder
      super.build_phase(phase);

      // Get an interface from the config db.
      if ( !uvm_config_db #(virtual dut_if)::get(this, "", "dut_if", dif))
         `uvm_error(get_full_name(), "uvm_config_db::get() call failed.")

      // Build a new analysis port.
      tx_collection_port = new("tx_collection_port", this);

      // Allocate transactions.
      tx_collected = output_transaction::type_id::create("tx_collected");
      tx_clone     = output_transaction::type_id::create("tx_clone"    );
   endfunction : build_phase

   // This spawns off collect_transactions() task.
   virtual task run_phase(uvm_phase phase);
      fork
         collect_transactions();
      join
   endtask : run_phase

   // Guess what this does?
   virtual task collect_transactions();
      forever begin

         // Here we sync with the valid bit as this is a multi-cycle
         // unit.  If this were out-of-order we'd need to get ID too.
         @(posedge dif.resp_valid);
         tx_collected.result = dif.resp_result;

         // Deep copy, not a handle assignment.
         tx_clone.do_copy(tx_collected);

         // Put that transaction on the port.
         tx_collection_port.write(tx_clone);
      end
   endtask : collect_transactions

endclass : output_monitor
