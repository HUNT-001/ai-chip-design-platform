// ----------------------------------------
// A second test.

class muldiv_test_base extends uvm_test;

  // Always, register with class factory.
  `uvm_component_utils(muldiv_test_base)
  
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

  function void end_of_elaboration_phase(uvm_phase phase);
     if($test$plusargs("ELABORATION_PHASE")) begin
         super.end_of_elaboration_phase(phase);
         uvm_top.print_topology();
     end
  endfunction: end_of_elaboration_phase

endclass : muldiv_test_base
