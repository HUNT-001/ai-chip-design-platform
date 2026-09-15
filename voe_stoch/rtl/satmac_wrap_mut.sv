// Parameter-fixing wrapper, matching the pattern used by the other DUTs so the
// plain-Verilog formal harness needs no parameter handling.
module satmac_wrap_mut (
    input  logic               clk_i,
    input  logic               rst_ni,
    input  logic               en_i,
    input  logic               clr_i,
    input  logic signed [7:0]  a_i,
    input  logic signed [7:0]  b_i,
    output logic signed [15:0] acc_o
);
  sat_mac_mut #(.W(8), .ACC(16)) u_mac (
      .clk_i(clk_i), .rst_ni(rst_ni), .en_i(en_i), .clr_i(clr_i),
      .a_i(a_i), .b_i(b_i), .acc_o(acc_o));
endmodule
