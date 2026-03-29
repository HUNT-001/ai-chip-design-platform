
   // ----------------------------------------
   // A long test sequence.  Maybe intended for bulk regressions?

   class input_sequence_long extends input_sequence_base;

      // register with factory
      `uvm_object_utils(input_sequence_long)

      // constructor
      function new (string name ="");
         super.new(name);
      endfunction

      // task body is required for this class, defined in parent.
      task body;
         my_config cfg;
         int num_tx;

         // Did we get a number of transactions in the config database?
         if (uvm_config_db #(my_config)::get(null, "", "config", cfg)) begin
            num_tx = cfg.num_tx;
         end
         else begin
            num_tx = 1000;  // sensible default
         end;

         `uvm_info(get_full_name(), $sformatf("num_tx = %0d", num_tx), UVM_LOW)

         // Prevent sim from exiting normally.  
         // starting_phase defined in parent.
         if (starting_phase != null)
            starting_phase.raise_objection(this);

         // And here we use that parameter from the config database to drive 
         // in random transactions.
         repeat(num_tx) begin
            // make yourself a transaction.
            // req defined in parent object
            req = input_transaction::type_id::create("req");
            // blocking call to synchronize with the driver.
            start_item(req);
            // proceeding - we're in sync
            // could do more here than just randomize().
            if (!req.randomize())
               `uvm_error(get_full_name(), "Transaction randomize failed.")
            // release the driver - your transaction is ready.
            finish_item(req);
         end 

         if (starting_phase != null)
            starting_phase.drop_objection(this);

      endtask: body

   endclass : input_sequence_long

