
   // ----------------------------------------
   // A class for your test.  This is kind of special, the one thing so far 
   // that we HAD to have.
   // FIXME : can I make a generic test class and then extend specific tests
   //         with their own run_phase options?  Something to explore later.

   class muldiv_test_1 extends muldiv_test_base;

      // Always, register with class factory.
      `uvm_component_utils(muldiv_test_1)

      // instantiate the environment
      muldiv_env m_env;

      // constructor class
      function new(string name, uvm_component parent);
         super.new(name, parent);
      endfunction

      // Build phase, build yourself that environment you declared above.
      function void build_phase(uvm_phase phase);
         m_env = muldiv_env::type_id::create("m_env", this);
      endfunction

      // Run phase task, the body of this task creates and runs an input sequence.
      task run_phase(uvm_phase phase);
         input_sequence_base seq;
         seq = input_sequence_base::type_id::create("seq");
         `uvm_info(get_full_name(), "Running muldiv_test_1 now.", UVM_LOW)
         if (!seq.randomize())
            `uvm_error(get_full_name(), "Randomize failed.")
         seq.starting_phase = phase;
         seq.start( m_env.in_agent.sequencer );
      endtask

   endclass : muldiv_test_1
