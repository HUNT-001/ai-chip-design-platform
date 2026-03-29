// ----------------------------------------
// A second test.

class muldiv_test_3 extends muldiv_test_base;

   // Always, register with class factory.
   `uvm_component_utils(muldiv_test_3)
   
   // Instantiate the environment.
   muldiv_env m_env;

   // This test wants some config action.
   my_config cfg;

   // Container for command line argument to catch the pass-by-reference
   // write from the UVM command line parser.
   string s_num_tx;

   // Need a pointer to the UVM command line parser.
   uvm_cmdline_processor cmdline;

   // constructor class
   function new(string name, uvm_component parent);
      super.new(name, parent);
   endfunction

   // build phase
   function void build_phase(uvm_phase phase);
      // Build yourself that environment you declared above.
      m_env = muldiv_env::type_id::create("m_env", this);

      // Allocate the configuration object.
      cfg = new;

      // Get a handle on the command line parser.  This is a singleton so
      // the build process is weird.
      cmdline = uvm_cmdline_processor::get_inst();

      // Query the parser for the one argument we care about.
      void' (cmdline.get_arg_value("+num_tx=", s_num_tx));

      // Convert that string result to an integer.
      if(s_num_tx.len() > 0)  // and there was an argument...
         cfg.num_tx = s_num_tx.atoi();
      else                    // ...else provide a default.
         cfg.num_tx = 100;

      `uvm_info(get_full_name(), $sformatf("num_tx argument = %s using %0d.", s_num_tx, cfg.num_tx), UVM_LOW)

      // Set number of expected transactions via the config DB.
      uvm_config_db #(my_config)::set(null, "", "config", cfg);
   endfunction

   // Run phase task.
   task run_phase(uvm_phase phase);
      input_sequence_base seq;
      // Override base type with derived type.
      input_sequence_base::type_id::set_type_override(input_sequence_long::get_type());
      seq = input_sequence_base::type_id::create("seq");
      `uvm_info(get_full_name(), "Running muldiv_test_3 now.", UVM_LOW)
      if (!seq.randomize())
         `uvm_error(get_full_name(), "Randomize failed.")
      seq.starting_phase = phase;
      seq.start( m_env.in_agent.sequencer );
   endtask

endclass : muldiv_test_3
