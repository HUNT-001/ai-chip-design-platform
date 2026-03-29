
// ----------------------------------------
// A second test.

class muldiv_test_2 extends muldiv_test_base;

   // always, register with class factory
   `uvm_component_utils(muldiv_test_2)

   // instantiate the environment
   muldiv_env m_env;

   // This test wants some config action.
   my_config cfg;

   // constructor class
   function new(string name, uvm_component parent);
      super.new(name, parent);
   endfunction

   // Build phase, build yourself that environment you declared above.
   function void build_phase(uvm_phase phase);
      m_env = muldiv_env::type_id::create("m_env", this);

      // Set number of expected transactions.  Use config DB to share this.
      cfg = new;
      cfg.num_tx = 250;
      uvm_config_db #(my_config)::set(null, "", "config", cfg);
   endfunction


   // Run phase task.
   task run_phase(uvm_phase phase);
      input_sequence_base seq;
      // Override base type with derived type.
      input_sequence_base::type_id::set_type_override(input_sequence_long::get_type());
      seq = input_sequence_base::type_id::create("seq");
      `uvm_info(get_full_name(), "Running muldiv_test_2 now.", UVM_LOW)
      if (!seq.randomize())
         `uvm_error(get_full_name(), "Randomize failed.")
      seq.starting_phase = phase;
      seq.start( m_env.in_agent.sequencer );
   endtask

endclass : muldiv_test_2

