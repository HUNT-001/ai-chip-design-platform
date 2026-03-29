// ----------------------------------------
// Monitor for input transactions.

class input_monitor extends uvm_monitor;
  // register with factory
  `uvm_component_utils(input_monitor)

  // virtual interface
  virtual dut_if dif;

  // Need an analysis port to broadcast what we find.
  uvm_analysis_port #(input_transaction) tx_collection_port;

  // Containers for observed transactions.
  input_transaction tx_collected;
  input_transaction tx_clone;

  // constructor
  function new (string name, uvm_component parent);
      super.new(name, parent);
  endfunction : new

  // build operations
  function void build_phase(uvm_phase phase);
    // parent builder
    super.build_phase(phase);

    // Get an interface from the config db.
    if ( !uvm_config_db #(virtual dut_if)::get(this, "", "dut_if", dif))
      `uvm_error(get_full_name(), "uvm_config_db::get() call failed.")

    // Build a new analysis port.
    tx_collection_port = new("tx_collection_port", this);

    // Allocate transactions.
    tx_collected = input_transaction::type_id::create("tx_collected");
    tx_clone     = input_transaction::type_id::create("tx_clone"    );
  endfunction : build_phase

  // This spawns off collect_transactions() task.
  virtual task run_phase(uvm_phase phase);
      fork
        collect_transactions();
      join
  endtask : run_phase

  // Guess what this does?
  virtual task collect_transactions();
      forever begin

        // FIXME - will need a tag if unit goes out-of-order.
        // Sync with rising clock edge.
        @(posedge dif.clk);
        // Only write if the we have a valid input.
        if (dif.req_valid) begin
            tx_collected.op1_signedp = dif.req_in_1_signed;
            tx_collected.op2_signedp = dif.req_in_2_signed;
            tx_collected.opcode      = dif.req_op;
            tx_collected.mux_select  = dif.req_out_sel;
            tx_collected.op1         = dif.req_in_1;
            tx_collected.op2         = dif.req_in_2;
            // have to compute sign from observed signals
            tx_collected.op1_sign    = (dif.req_in_1_signed)?(dif.req_in_1[`XPR_LEN-1]):'0;
            tx_collected.op2_sign    = (dif.req_in_2_signed)?(dif.req_in_2[`XPR_LEN-1]):'0;

            // deep copy, not a handle assignment.
            tx_clone.do_copy(tx_collected);

            // put that transaction on the port.
            tx_collection_port.write(tx_clone);
        end
      end
  endtask : collect_transactions

endclass : input_monitor