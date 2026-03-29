
   // ----------------------------------------
   // An agent class to wrap up sequencer, driver, and monitor.

   class input_agent extends uvm_agent;
      protected uvm_active_passive_enum is_active = UVM_ACTIVE;

      typedef input_sequencer input_sequencer_t;

      input_sequencer_t sequencer;
      input_driver    driver;
      input_monitor   monitor;

      `uvm_component_utils_begin(input_agent)
         `uvm_field_enum(uvm_active_passive_enum, is_active, UVM_ALL_ON)
      `uvm_component_utils_end

      // constructor
      function new (string name, uvm_component parent);
         super.new(name, parent);
      endfunction

      // build phase
      function void build_phase(uvm_phase phase);
         super.build_phase(phase);

         // we only build these for active agents
         if (is_active == UVM_ACTIVE) begin
            sequencer = input_sequencer::type_id::create("sequencer", this);
            driver    = input_driver   ::type_id::create("driver",    this);
         end

         // everyone gets a monitor
         monitor = input_monitor::type_id::create("monitor", this);

         // shout out
         `uvm_info(get_full_name(), "build stage complete.", UVM_LOW)
      endfunction : build_phase

      // connect phase
      function void connect_phase(uvm_phase phase);
         if(is_active == UVM_ACTIVE) 
            driver.seq_item_port.connect(sequencer.seq_item_export);

         `uvm_info(get_full_name(), "connect stage complete.", UVM_LOW)
      endfunction : connect_phase

   endclass : input_agent