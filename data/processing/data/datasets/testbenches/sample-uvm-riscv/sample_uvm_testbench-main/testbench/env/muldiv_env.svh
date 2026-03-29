
   // ----------------------------------------
   // Environment class for the whole mess.
   // Right now this is overkill since we only have the one agent.

   class muldiv_env extends uvm_env;

      // Register with factory
      `uvm_component_utils(muldiv_env)

      // More agents than the CIA.
      input_agent  in_agent;
      output_agent out_agent;

      // A scoreboard to check the results.
      muldiv_scoreboard m_sbd;
      // A little coverage on unit input.
      input_coverage m_cov;

      // constructor
      function new(string name, uvm_component parent);
         super.new(name, parent);
      endfunction : new

      // build phase actions
      function void build_phase(uvm_phase phase);
         in_agent  = input_agent      ::type_id::create("in_agent" , this);
         out_agent = output_agent     ::type_id::create("out_agent", this);
         m_sbd     = muldiv_scoreboard::type_id::create("m_sbd",     this);
         m_cov     = input_coverage   ::type_id::create("m_cov",     this);
      endfunction : build_phase

      // Connect phase actions - need to connect scoreboard to agent monitors.
      function void connect_phase(uvm_phase phase);
         in_agent.monitor.tx_collection_port.connect(m_sbd.tx_in_collected.analysis_export);
         out_agent.monitor.tx_collection_port.connect(m_sbd.tx_out_collected.analysis_export);
         // m_cov is a child of uvm_subscriber and has an analysis port already
         in_agent.monitor.tx_collection_port.connect( m_cov.analysis_export );
      endfunction : connect_phase

   endclass : muldiv_env

