module waveform;
   initial begin
      $dumpfile ("waveform.vcd");
      $dumpvars (0,picorv32);
      #1;
   end
endmodule
