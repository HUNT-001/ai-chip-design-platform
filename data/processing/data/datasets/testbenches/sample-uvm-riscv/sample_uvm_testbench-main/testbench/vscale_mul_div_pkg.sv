// ------------------------------------------------------------------------------
// Define UVM testbench package.

// Import UVM packages.
import uvm_pkg::*;

package vscale_mul_div_pkg;

   // Include UVM macros 
   `include "uvm_macros.svh"

   // Include testbench commons
   `include "my_config.svh"

   // Include agents/input
   `include "agents/input/input_transaction.svh"
   `include "agents/input/input_sequence_base.svh"
   `include "agents/input/input_sequence_long.svh"
   `include "agents/input/input_driver.svh"
   `include "agents/input/input_monitor.svh"
   `include "agents/input/input_sequencer.svh"
   `include "agents/input/input_agent.svh"
      
   // Include agents/output
   `include "agents/output/output_transaction.svh"
   `include "agents/output/output_monitor.svh"
   `include "agents/output/output_agent.svh"
   
   // Include env
   `include "env/input_coverage.svh"
   `include "env/muldiv_scoreboard.svh"
   `include "env/muldiv_env.svh"

   // Include tests
   `include "tests/muldiv_test_base.svh"
   `include "tests/muldiv_test_1.svh"
   `include "tests/muldiv_test_2.svh"
   `include "tests/muldiv_test_3.svh"
   
endpackage : vscale_mul_div_pkg