// rtl/example_cpu/cpu_top.v — Top-level wrapper (AVA v2.0.0).
// Added commit_trap_epc and commit_trap_tvec to match v2.0.0 commit interface.
`default_nettype none
`timescale 1ns/1ps

module cpu_top (
    input  wire        clk,
    input  wire        rst_n,

    // Memory bus
    output wire        mem_req_valid,
    output wire        mem_req_we,
    output wire [31:0] mem_req_addr,
    output wire [31:0] mem_req_wdata,
    output wire [3:0]  mem_req_wstrb,
    input  wire [31:0] mem_resp_rdata,
    input  wire        mem_resp_ready,

    // Commit monitor (v2.0.0 interface)
    output wire        commit_valid,
    output wire [31:0] commit_pc,
    output wire [31:0] commit_instr,
    output wire [4:0]  commit_rd_addr,
    output wire [31:0] commit_rd_data,
    output wire        commit_rd_we,
    output wire [1:0]  commit_priv_mode,
    output wire        commit_trap_valid,
    output wire [31:0] commit_trap_cause,
    output wire [31:0] commit_trap_epc,    // NEW v2.0.0
    output wire [31:0] commit_trap_tvec,   // NEW v2.0.0
    output wire [31:0] commit_trap_tval,
    output wire        commit_is_mret
);

rv32im_core u_core (
    .clk               (clk),
    .rst_n             (rst_n),
    .mem_req_valid     (mem_req_valid),
    .mem_req_we        (mem_req_we),
    .mem_req_addr      (mem_req_addr),
    .mem_req_wdata     (mem_req_wdata),
    .mem_req_wstrb     (mem_req_wstrb),
    .mem_resp_rdata    (mem_resp_rdata),
    .mem_resp_ready    (mem_resp_ready),
    .commit_valid      (commit_valid),
    .commit_pc         (commit_pc),
    .commit_instr      (commit_instr),
    .commit_rd_addr    (commit_rd_addr),
    .commit_rd_data    (commit_rd_data),
    .commit_rd_we      (commit_rd_we),
    .commit_priv_mode  (commit_priv_mode),
    .commit_trap_valid (commit_trap_valid),
    .commit_trap_cause (commit_trap_cause),
    .commit_trap_epc   (commit_trap_epc),
    .commit_trap_tvec  (commit_trap_tvec),
    .commit_trap_tval  (commit_trap_tval),
    .commit_is_mret    (commit_is_mret)
);

endmodule
`default_nettype wire
