class input_sequencer extends uvm_sequencer #(input_transaction);
  `uvm_component_param_utils(input_sequencer)
  
  function new(string name = "input_sequencer", uvm_component parent = null);
      super.new(name, parent);
  endfunction: new

endclass: input_sequencer