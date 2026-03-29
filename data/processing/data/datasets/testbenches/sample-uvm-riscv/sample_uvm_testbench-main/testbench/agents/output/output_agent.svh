// ----------------------------------------
// This agent class watches the output of the unit.

class output_agent extends uvm_agent;
   protected uvm_active_passive_enum is_active = UVM_PASSIVE;

   output_monitor   monitor;

   `uvm_component_utils_begin(output_agent)
      `uvm_field_enum(uvm_active_passive_enum, is_active, UVM_ALL_ON)
   `uvm_component_utils_end

   // constructor
   function new (string name, uvm_component parent);
      super.new(name, parent);
   endfunction

   // build phase
   function void build_phase(uvm_phase phase);
      super.build_phase(phase);

      // Everyone gets a monitor.
      monitor = output_monitor::type_id::create("monitor", this);

      // Shout out!
      `uvm_info(get_full_name(), "Build stage complete.", UVM_LOW)
   endfunction : build_phase

endclass : output_agent