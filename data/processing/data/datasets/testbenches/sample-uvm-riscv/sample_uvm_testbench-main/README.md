# sample_uvm_testbench
A sample UVM testbench, in System Verilog, exercising a RISC-V arithmetic unit.

## What's in this repository?

I pulled the Vscale RISC-V CPU design from github on 15 June 2016.
* https://github.com/ucb-bar/vscale
* No tags, last commit was ad150b1 on 11 April 2016.

The RISC-V CPU architecture is an open-source RISC design originating at 
University of California Berkeley.  RISC-V, and the Vscale instantiation 
of the architecture, are free for use.  That was particularly appealing for
my application.

I chose the multiple-divide unit for my testbench for a few reasons:
1. It was simple and small enough so that my focus would be on the testbench.
1. It had clear and clean interfaces which were well suited to UVM.
1. Potential code reviewers could be assumed to be familiar with multiplication and division.

## Design Section

Elements of the vscale RISC-V design in this repository:

* vscale LICENSE.  
    * Documents the license for the vscale CPU from UC.  
    * Fulfills legal requirement that I acknowledge UC's copyright.

* vscale_mul_div_original.v 
    * The original, unedited file from GitHub.  
    * This is the Verilog implementation of the multiply-divide unit in the CPU.

* vscale_mul_div.v This version of the multiply-divide unit has three significant changes:
    1. I commented the file extensively to help with my own understanding.
    1. I fixed a reset bug where two state elements were not initialized.
    1. I fixed a Verilog read-before-write race condition on a signal.

* Three files with defines used by the multiply-divide unit.  Some defines are reused in my testbench.
    * rv32_opcodes.vh
    * vscale_ctrl_constants.vh
    * vscale_md_constants.vh
   
## Testbench Section

My testbench code is entirely in one file:

 * testbench_top.sv
    1. I define several UVM objects to implement a testbench.
        1. input and output transactions.
        1. agents for the input (active) and output (passive) interfaces for the unit.
        1. sequences to drive in test stimulus.
        1. a scoreboard to check results.
        1. a stub checker module to record test input.
    1. I instantiate my UVM objects in an environment.
    1. There are three different test classes depending on your needs.
    1. A top-level model to instantiate the DUT, interface, and testbench.  This
       also handles some necessary modeling like clock generation and reset
       assertion.
    5. In a few places I use a command line parser to get runtime options.


## Using the files

I intended this to be used with [EDA Playground](https://www.edaplayground.com/).
* You will need to register.  It will want an organizational email.  I used my IEEE email.  Consider whether you want your current employer to know you're looking at this.
* The one testbench file should be added to the left-hand pane, next to "testbench".
* The design files should be added to the right-hand pane, next to "design".
* Select the following options in the left-hand panel:
    * Testbench+Design = SystemVerilog / Verilog
    * UVM/OVM = UVM 1.2
    * Other Libraries = None
    * Tools & Simulators = Synopsys VCS 2020.03 (Not tested with other simulators)
* The Run button up at the top runs the job.

## FIXME List and Enhancements

* The testbench should probably be in multiple files. Current implmementation violates the "one screen" design paradigm.
* Need to scrub the whole thing to see if my opinions have changed in the last five years.


