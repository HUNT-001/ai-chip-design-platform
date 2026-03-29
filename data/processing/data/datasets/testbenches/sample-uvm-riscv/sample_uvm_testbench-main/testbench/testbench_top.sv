// ------------------------------------------------------------------------------
//
// Top module for vscale mul-div unit.
// Demonstrator for UVM application to unit-level testing.
//
// (c) 2016 Gregory A. Kemp
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
// http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
// 
// ------------------------------------------------------------------------------

// ------------------------------------------------------------------------------
// Top-level module for unit test environment.

 // import my package.
import vscale_mul_div_pkg::*; 

module top;
   // Name the test via command-line interface.
   string test_name;

   // Get a handle on the UVM command line parser.
   uvm_cmdline_processor cmdline;

   // Declare an interface (with null ports) for the DUT.
   dut_if dif ();

   // Instantiate the DUT.
   // Module interface is ugly because module is old-school Verilog and
   // doesn't understand the SystemVerilog interface class I'm using.
   vscale_mul_div dut(
      dif.clk,
      dif.reset,
      dif.req_valid,
      dif.req_ready,
      dif.req_in_1_signed,
      dif.req_in_2_signed,
      dif.req_op,
      dif.req_out_sel,
      dif.req_in_1,
      dif.req_in_2,
      dif.resp_valid,
      dif.resp_result
   );

   // Make a clock.
   initial begin
      dif.clk = 1'b1;
      forever #5 dif.clk = ~dif.clk;
   end
   
   // Assert reset for DUT.
   // FIXME add assertion control here, too?
   initial begin
      dif.reset = 1'b1;
      #20 dif.reset = 1'b0;
   end

   // Test control block.
   initial begin
      // Enable dumping for waveform view.
      $dumpfile("dump.vcd");
      $dumpvars;
      // Register DUT interface with factory.
      uvm_config_db #(virtual dut_if)::set(null, "*", "dut_if", dif);
      // If set, then run_test will call $finish after all phases are executed.
      uvm_top.finish_on_completion = 1;

      `ifdef COMMAND_LINE_TEST_NAME
         // Get handle to the command line processor singleton object.
         cmdline = uvm_cmdline_processor::get_inst();
         // And read that command line value.  Result written to test_name arugment.
         void' (cmdline.get_arg_value("+test=", test_name));
         // Safety value in case user didn't provide an option.
         // FIXME validate input - right now we just die if it's not a legal test case.
         if(test_name.len() == 0) test_name = "muldiv_test_3";
      `endif
      
      // Whatever it was, run the test.
      run_test(test_name);
   end

   // End-of-test housekeeping.
   final begin
      // Print coverage results.
      `uvm_info("", $sformatf("Overall coverage = %02.2f", $get_coverage()), UVM_NONE)
   end

endmodule : top

