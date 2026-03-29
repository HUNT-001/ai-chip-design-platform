// ----------------------------------------
// Define a transaction for outputs from the unit.

class output_transaction extends uvm_sequence_item;

  // Register with object factory.
  `uvm_object_utils(output_transaction)

  // Output fields we need to capture.
  // FIXME - we'll need an ID field for out of order extension.
  logic [`XPR_LEN-1:0] result;

  // Class constructor.
  function new (string name ="");
    super.new(name);
  endfunction

  // For printing.
  function string convert2string;
    return $sformatf("result = %x", result);
  endfunction

  // For copy.
  function void do_copy(uvm_object rhs);
    output_transaction tx;

    // Because parent may have fields to copy, too.
    super.do_copy(rhs);

    $cast(tx, rhs);
    result = tx.result;
  endfunction

  // For comparisons.
  function bit do_compare(uvm_object rhs, uvm_comparer comparer);
    output_transaction tx;
    bit status = 1;

    // Because parent may have fields to compare, too.
    status &= super.do_compare(rhs, comparer);

    $cast(tx, rhs);
    status &= (result == tx.result);

    return status;
  endfunction

endclass : output_transaction