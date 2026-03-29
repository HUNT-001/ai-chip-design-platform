// ----------------------------------------
// Define a transaction for inputs to the unit.

class input_transaction extends uvm_sequence_item;

   // Register with object factory.
   `uvm_object_utils(input_transaction)

   // Fields we'll be randomizing in this object.
   // FIXME : there's a lot of redundancy between op1 and op2.  Would it 
   // make since to make a subclass for operands?  Leave this for now
   // as a possible future enhancement.

   // Are the operands signed or unsigned?
   rand bit op1_signedp;
   rand bit op2_signedp;
   // Signs of the operands?
   rand bit op1_sign;
   rand bit op2_sign;
   // Opcode
   rand logic [`MD_OP_WIDTH-1:0] opcode;
   // Output mux control.
   rand logic [`MD_OUT_SEL_WIDTH-1:0] mux_select;
   // Next two signals are the operands.
   rand logic [`XPR_LEN-1:0] op1;
   rand logic [`XPR_LEN-1:0] op2;

   // generation constraints

   // Really need to know if this is signed before we sweat the sign itself.
   constraint c_sign_order_op1 { solve op1_signedp before op1_sign; }
   constraint c_sign_order_op2 { solve op2_signedp before op2_sign; }

   // Need to keep both operands signed or unsigned.  Don't mix and match.
   constraint c_signed_ops_1 { solve op1_signedp before op2_signedp; }
   constraint c_signed_ops_2 { op2_signedp == op1_signedp;           }

   // We want to ensure values 0, 1, 2, -1, and -2 occur more often than
   // statistically expected.  0 and 1 are "magic" values in multiplication
   // and division.  2 simply shifts by one bit.  Constraints are soft in 
   // case a sequence wants to override the value later.  I'm using the 
   // :/ dist syntax because for every 1 time I get a zero (5% of the time)
   // I want 17 "regular" numbers (85% of the time).

   constraint c_op1_value { soft op1 dist { 0 :/1 , 1 :/ 1 , 2 :/ 1, [3:$] :/ 17 }; }
   constraint c_op2_value { soft op2 dist { 0 :/1 , 1 :/ 1 , 2 :/ 1, [3:$] :/ 17 }; }

   // Now make the sign consistent with the value.
   constraint c_op1_sign { op1_signedp -> op1_sign == op1[`XPR_LEN-1]; }
   constraint c_op2_sign { op2_signedp -> op2_sign == op2[`XPR_LEN-1]; }

   // Require the opcode to be a legal value.
   // FIXME what if we give it an illegal opcode? What happens then?
   constraint c_opcode_legal { opcode inside { `MD_OP_MUL, `MD_OP_DIV, `MD_OP_REM }; }

   // Constraints on rounding mode.
   constraint c_mux_select_1 { solve opcode before mux_select; }
   constraint c_mux_select_2 { if (opcode == `MD_OP_REM) (mux_select == `MD_OUT_REM); }
   constraint c_mux_select_3 { if (opcode != `MD_OP_REM) (mux_select != `MD_OUT_REM); }

   // class constructor.
   function new (string name ="");
      super.new(name);
   endfunction

   // This function dumps the whole contents of the object.  Expected use is
   // debugging and extended error reporting.
   function void print_full();
      // FIXME create a string with these information and add an uvm_info
      $write("Input transaction:\n");
      $write("   Opcode: ");
      case (opcode)
         `MD_OP_MUL: $write("MD_OP_MUL");
         `MD_OP_DIV: $write("MD_OP_DIV");
         `MD_OP_REM: $write("MD_OP_REM");
         default:    $write("unknown  ");
      endcase
      $write("   Rounding mode: ");
      case (mux_select)
         `MD_OUT_LO:  $write("LO ");
         `MD_OUT_HI:  $write("HI ");
         `MD_OUT_REM: $write("REM");
      endcase
      $write("\n");
      $write("   Operand 1: ");
      if(op1_signedp) begin
         $write("  signed");
         if(op1_sign)
            $write(" - ");
         else
            $write(" + ");
      end
      $write("%8x\n", op1);
      $write("   Operand 2: ");
      if(op2_signedp) begin
         $write("  signed");
         if(op2_sign)
            $write(" - ");
         else
            $write(" + ");
      end
      $write("%8x\n", op2);
   endfunction

   // for printing.
   function string convert2string;
      return $sformatf("opcode=%x %s op1=%x %s op2=%x", opcode, (op1_signedp?"(s)":"   "), op1, (op2_signedp?"(s)":"   "), op2);
   endfunction

   // for copy
   function void do_copy(uvm_object rhs);
      input_transaction tx;

      // Because parent may have fields to copy, too.
      super.do_copy(rhs);

      $cast(tx, rhs);
      op1_signedp = tx.op1_signedp;
      op2_signedp = tx.op2_signedp;
      op1_sign    = tx.op1_sign;
      op2_sign    = tx.op2_sign;
      opcode      = tx.opcode;
      mux_select  = tx.mux_select;
      op1         = tx.op1;
      op2         = tx.op2;
   endfunction

   // for comparisons
   function bit do_compare(uvm_object rhs, uvm_comparer comparer);
      input_transaction tx;
      bit status = 1;

      // Because parent may have fields to compare, too.
      status &= super.do_compare(rhs, comparer);

      $cast(tx, rhs);
      status &= (op1_signedp == tx.op1_signedp);
      status &= (op2_signedp == tx.op2_signedp);
      status &= (op1_sign    == tx.op1_sign);
      status &= (op2_sign    == tx.op2_sign);
      status &= (opcode      == tx.opcode);
      status &= (mux_select  == tx.mux_select);
      status &= (op1         == tx.op1);
      status &= (op2         == tx.op2);

      return status;
   endfunction

endclass : input_transaction