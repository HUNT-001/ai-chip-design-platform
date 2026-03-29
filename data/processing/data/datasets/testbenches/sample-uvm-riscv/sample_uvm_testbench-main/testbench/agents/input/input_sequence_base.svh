// ----------------------------------------
// A basic test sequence.  As the name implies there may be more.

class input_sequence_base extends uvm_sequence #(input_transaction);

   // register with factory
   `uvm_object_utils(input_sequence_base)

   // constructor
   function new (string name ="");
      super.new(name);
   endfunction

   // task body is required for this class, defined in parent.
   task body;

      // Prevent sim from exiting normally.  
      // starting_phase defined in parent.
      if (starting_phase != null)
         starting_phase.raise_objection(this);

      // just chuck in some random objects for now.
      repeat(10) begin
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

endclass : input_sequence_base
