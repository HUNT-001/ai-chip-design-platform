// ----------------------------------------
// class for driver on the interface.

class input_driver extends uvm_driver #(input_transaction);
  // register with factory
  `uvm_component_utils(input_driver)

  // instantiate the interface
  // I don't think this must be virtual.
  virtual dut_if dif;

  function new (string name, uvm_component parent);
    super.new(name, parent);
  endfunction

  function void build_phase (uvm_phase phase);
    // get inferface from config database, throw an error if this fails.
    if ( !uvm_config_db #(virtual dut_if)::get(this, "", "dut_if", dif))
      `uvm_error(get_full_name(), "uvm_config_db::get() call failed.")
  endfunction

  task run_phase(uvm_phase phase);
    // Initialize interface to known values.
    // req_valid is the key.  If this is zero the other signals are ignored.
    dif.req_valid       <= '0;

    // No reason to start looking for transactions until reset drops.
    @(negedge dif.reset);

    forever begin
      // Get yourself a transaction from the sequencer.
      seq_item_port.get_next_item(req);

      // Wait for posedge clock AND the unit is ready for a transaction.
      // This may be more than one clock.
      wait (dif.req_ready);
      @(posedge dif.clk);
      dif.req_valid       <= '1;
      dif.req_in_1_signed <= req.op1_signedp;
      dif.req_in_2_signed <= req.op2_signedp;
      dif.req_op          <= req.opcode;
      dif.req_out_sel     <= req.mux_select;
      dif.req_in_1        <= req.op1;
      dif.req_in_2        <= req.op2;
      @(posedge dif.clk);
      dif.req_valid       <= '0;

      seq_item_port.item_done();
    end
  endtask : run_phase

endclass : input_driver
