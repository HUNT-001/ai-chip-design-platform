// rtl/example_cpu/rv32im_core.v — RV32IM core, AVA v2.0.0
//
// Changes from v1:
//  * EXECUTE and WRITEBACK merged for all non-memory instructions
//    (LUI/AUIPC/JAL/JALR/BRANCH/IMM/REG/FENCE): 3 cycles instead of 4.
//    Memory ops (LOAD/STORE) still use MEMWAIT; TRAP is unchanged.
//    Throughput gain: ~20% for ALU-heavy workloads.
//  * Added commit_trap_epc and commit_trap_tvec output ports (AVA v2.0.0
//    schema requires trap.epc and trap.tvec in every trap record).
//  * All RTL semantics unchanged (RV32IM spec-compliant).

`default_nettype none
`timescale 1ns/1ps

module rv32im_core (
    input  wire        clk,
    input  wire        rst_n,

    // Memory bus (single-cycle request / combinational response)
    output reg         mem_req_valid,
    output reg         mem_req_we,
    output reg  [31:0] mem_req_addr,
    output reg  [31:0] mem_req_wdata,
    output reg  [3:0]  mem_req_wstrb,
    input  wire [31:0] mem_resp_rdata,
    input  wire        mem_resp_ready,

    // Commit monitor — one-cycle pulse per retired instruction
    output reg         commit_valid,
    output reg  [31:0] commit_pc,
    output reg  [31:0] commit_instr,
    output reg  [4:0]  commit_rd_addr,
    output reg  [31:0] commit_rd_data,
    output reg         commit_rd_we,
    output reg  [1:0]  commit_priv_mode,
    output reg         commit_trap_valid,
    output reg  [31:0] commit_trap_cause,
    output reg  [31:0] commit_trap_epc,    // NEW v2.0.0 — mepc saved on trap
    output reg  [31:0] commit_trap_tvec,   // NEW v2.0.0 — mtvec handler address
    output reg  [31:0] commit_trap_tval,
    output reg         commit_is_mret
);

// ── Register file ────────────────────────────────────────────────────────────
reg [31:0] rf [0:31];
integer idx;

// ── FSM states ────────────────────────────────────────────────────────────────
// Optimised: no separate S_WRITEBACK for pure-ALU instructions.
// ALU path:    S_FETCH → S_DECODE → S_EXECUTE  (3 cycles, commit in EXECUTE)
// Memory path: S_FETCH → S_DECODE → S_EXECUTE → S_MEMWAIT → S_LOADWB (5+)
// Trap path:   S_FETCH → S_DECODE → S_EXECUTE → S_TRAP     (4 cycles)
localparam S_FETCH   = 3'd0;
localparam S_DECODE  = 3'd1;
localparam S_EXECUTE = 3'd2;   // ALU: compute + commit + RF write all here
localparam S_MEMWAIT = 3'd3;
localparam S_LOADWB  = 3'd4;   // load writeback (was S_WRITEBACK, now load-only)
localparam S_TRAP    = 3'd5;

reg [2:0]  state;
reg [31:0] pc, ir;
reg [31:0] load_data;
reg [4:0]  load_rd;
reg [1:0]  mem_width;
reg        mem_signed_ext;
reg [31:0] trap_cause_r, trap_tval_r;
reg        is_mret_r;

// ── Instruction decode ────────────────────────────────────────────────────────
wire [6:0] opcode = ir[6:0];
wire [4:0] rd_f   = ir[11:7];
wire [2:0] funct3 = ir[14:12];
wire [4:0] rs1_f  = ir[19:15];
wire [4:0] rs2_f  = ir[24:20];
wire [6:0] funct7 = ir[31:25];

wire [31:0] imm_i = {{20{ir[31]}}, ir[31:20]};
wire [31:0] imm_s = {{20{ir[31]}}, ir[31:25], ir[11:7]};
wire [31:0] imm_b = {{19{ir[31]}}, ir[31], ir[7], ir[30:25], ir[11:8], 1'b0};
wire [31:0] imm_u = {ir[31:12], 12'b0};
wire [31:0] imm_j = {{11{ir[31]}}, ir[31], ir[19:12], ir[20], ir[30:21], 1'b0};

wire [31:0] rs1 = (rs1_f == 0) ? 32'b0 : rf[rs1_f];
wire [31:0] rs2 = (rs2_f == 0) ? 32'b0 : rf[rs2_f];

// ── M-extension (fully combinational) ────────────────────────────────────────
wire [63:0] mul_ss  = $signed(rs1) * $signed(rs2);
wire [63:0] mul_su  = $signed(rs1) * {1'b0, rs2};
wire [63:0] mul_uu  = rs1 * rs2;
wire        dz      = (rs2 == 32'b0);
wire        div_ovf = ($signed(rs1) == 32'sh80000000) && (rs2 == 32'hFFFFFFFF);
wire [31:0] div_q_s = dz ? 32'hFFFFFFFF : div_ovf ? 32'h80000000 : $signed(rs1) / $signed(rs2);
wire [31:0] div_r_s = dz ? rs1          : div_ovf ? 32'b0        : $signed(rs1) % $signed(rs2);
wire [31:0] div_q_u = dz ? 32'hFFFFFFFF : rs1 / rs2;
wire [31:0] div_r_u = dz ? rs1          : rs1 % rs2;

// ── CSR file (Machine mode) ───────────────────────────────────────────────────
reg [31:0] csr_mstatus, csr_mie, csr_mtvec, csr_mscratch;
reg [31:0] csr_mepc, csr_mcause, csr_mtval, csr_mcycle, csr_minstret;

localparam MSTATUS  = 12'h300;
localparam MIE      = 12'h304;
localparam MTVEC    = 12'h305;
localparam MSCRATCH = 12'h340;
localparam MEPC     = 12'h341;
localparam MCAUSE   = 12'h342;
localparam MTVAL    = 12'h343;
localparam MCYCLE   = 12'hC00;
localparam MINSTRET = 12'hC02;

function [31:0] csr_read;
    input [11:0] a;
    case (a)
        MSTATUS:  csr_read = csr_mstatus;
        12'h301:  csr_read = 32'h40101100;  // misa RV32IM
        MIE:      csr_read = csr_mie;
        MTVEC:    csr_read = csr_mtvec;
        MSCRATCH: csr_read = csr_mscratch;
        MEPC:     csr_read = csr_mepc;
        MCAUSE:   csr_read = csr_mcause;
        MTVAL:    csr_read = csr_mtval;
        MCYCLE:   csr_read = csr_mcycle;
        MINSTRET: csr_read = csr_minstret;
        default:  csr_read = 32'b0;
    endcase
endfunction

// ── Opcode constants ──────────────────────────────────────────────────────────
localparam OP_LUI    = 7'h37;
localparam OP_AUIPC  = 7'h17;
localparam OP_JAL    = 7'h6F;
localparam OP_JALR   = 7'h67;
localparam OP_BRANCH = 7'h63;
localparam OP_LOAD   = 7'h03;
localparam OP_STORE  = 7'h23;
localparam OP_IMM    = 7'h13;
localparam OP_REG    = 7'h33;
localparam OP_SYSTEM = 7'h73;
localparam OP_FENCE  = 7'h0F;

// ── Main FSM ──────────────────────────────────────────────────────────────────
always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
        state <= S_FETCH; pc <= 32'h80000000;
        mem_req_valid <= 0; mem_req_we <= 0;
        mem_req_addr <= 0; mem_req_wdata <= 0; mem_req_wstrb <= 0;
        commit_valid <= 0; commit_trap_valid <= 0; commit_is_mret <= 0;
        commit_trap_epc <= 0; commit_trap_tvec <= 0;
        csr_mstatus <= 32'h00001800;
        csr_mie <= 0; csr_mtvec <= 32'h80000004;
        csr_mscratch <= 0; csr_mepc <= 0; csr_mcause <= 0;
        csr_mtval <= 0; csr_mcycle <= 0; csr_minstret <= 0;
        for (idx = 0; idx < 32; idx = idx + 1) rf[idx] <= 0;
    end else begin
        // Default: clear single-cycle strobes
        commit_valid      <= 0;
        commit_trap_valid <= 0;
        commit_is_mret    <= 0;
        csr_mcycle <= csr_mcycle + 1;

        case (state)

        // ── FETCH: issue instruction read ───────────────────────────────────
        S_FETCH: begin
            mem_req_valid <= 1;
            mem_req_we    <= 0;
            mem_req_addr  <= pc;
            mem_req_wstrb <= 0;
            state <= S_DECODE;
        end

        // ── DECODE: wait for instruction memory response ─────────────────────
        S_DECODE: begin
            if (mem_resp_ready) begin
                mem_req_valid <= 0;
                ir    <= mem_resp_rdata;
                state <= S_EXECUTE;
            end
        end

        // ── EXECUTE ──────────────────────────────────────────────────────────
        // ALU instructions: compute result, write RF, emit commit, go to S_FETCH.
        // Memory/SYSTEM/TRAP: set up request, transition to appropriate next state.
        // Optimisation: no separate S_WRITEBACK for ALU — saves one cycle per insn.
        S_EXECUTE: begin

            case (opcode)

            // ── LUI ─────────────────────────────────────────────────────────
            OP_LUI: begin
                if (rd_f != 0) rf[rd_f] <= imm_u;
                _commit_alu(pc, ir, rd_f, imm_u, (rd_f != 0));
                pc    <= pc + 4;
                state <= S_FETCH;
            end

            // ── AUIPC ───────────────────────────────────────────────────────
            OP_AUIPC: begin
                begin : auipc_b
                    reg [31:0] r; r = pc + imm_u;
                    if (rd_f != 0) rf[rd_f] <= r;
                    _commit_alu(pc, ir, rd_f, r, (rd_f != 0));
                end
                pc    <= pc + 4;
                state <= S_FETCH;
            end

            // ── JAL ─────────────────────────────────────────────────────────
            OP_JAL: begin
                if (rd_f != 0) rf[rd_f] <= pc + 4;
                _commit_alu(pc, ir, rd_f, pc + 4, (rd_f != 0));
                pc    <= pc + imm_j;
                state <= S_FETCH;
            end

            // ── JALR ────────────────────────────────────────────────────────
            OP_JALR: begin
                begin : jalr_b
                    reg [31:0] tgt; tgt = (rs1 + imm_i) & ~32'b1;
                    if (rd_f != 0) rf[rd_f] <= pc + 4;
                    _commit_alu(pc, ir, rd_f, pc + 4, (rd_f != 0));
                    pc <= tgt;
                end
                state <= S_FETCH;
            end

            // ── BRANCH ──────────────────────────────────────────────────────
            OP_BRANCH: begin : br
                reg taken; taken = 0;
                case (funct3)
                    3'b000: taken = (rs1 == rs2);
                    3'b001: taken = (rs1 != rs2);
                    3'b100: taken = ($signed(rs1) < $signed(rs2));
                    3'b101: taken = !($signed(rs1) < $signed(rs2));
                    3'b110: taken = (rs1 < rs2);
                    3'b111: taken = !(rs1 < rs2);
                endcase
                _commit_alu(pc, ir, 5'd0, 32'b0, 1'b0);
                pc    <= taken ? pc + imm_b : pc + 4;
                state <= S_FETCH;
            end

            // ── LOAD ────────────────────────────────────────────────────────
            OP_LOAD: begin
                mem_req_valid   <= 1;
                mem_req_we      <= 0;
                mem_req_addr    <= rs1 + imm_i;
                mem_req_wstrb   <= 0;
                mem_width       <= funct3[1:0];
                mem_signed_ext  <= !funct3[2];
                load_rd         <= rd_f;
                state           <= S_MEMWAIT;
            end

            // ── STORE ───────────────────────────────────────────────────────
            OP_STORE: begin : st
                reg [31:0] sa, sd; reg [3:0] ss;
                sa = rs1 + imm_s;
                case (funct3[1:0])
                    2'b00: begin ss = 4'b0001 << sa[1:0]; sd = {4{rs2[7:0]}};  end
                    2'b01: begin ss = sa[1] ? 4'b1100 : 4'b0011; sd = {2{rs2[15:0]}}; end
                    default: begin ss = 4'b1111; sd = rs2; end
                endcase
                mem_req_valid <= 1; mem_req_we <= 1;
                mem_req_addr  <= sa; mem_req_wdata <= sd; mem_req_wstrb <= ss;
                state <= S_MEMWAIT;
            end

            // ── OP-IMM ──────────────────────────────────────────────────────
            OP_IMM: begin : opi
                reg [31:0] r;
                case (funct3)
                    3'b000: r = rs1 + imm_i;
                    3'b010: r = {31'b0, $signed(rs1) < $signed(imm_i)};
                    3'b011: r = {31'b0, rs1 < imm_i};
                    3'b100: r = rs1 ^ imm_i;
                    3'b110: r = rs1 | imm_i;
                    3'b111: r = rs1 & imm_i;
                    3'b001: r = rs1 << imm_i[4:0];
                    3'b101: r = ir[30] ? ($signed(rs1) >>> imm_i[4:0]) : (rs1 >> imm_i[4:0]);
                    default: r = 0;
                endcase
                if (rd_f != 0) rf[rd_f] <= r;
                _commit_alu(pc, ir, rd_f, r, (rd_f != 0));
                pc    <= pc + 4;
                state <= S_FETCH;
            end

            // ── OP-REG ──────────────────────────────────────────────────────
            OP_REG: begin : opr
                reg [31:0] r;
                if (funct7 == 7'h01) begin
                    case (funct3)
                        3'b000: r = mul_uu[31:0];
                        3'b001: r = mul_ss[63:32];
                        3'b010: r = mul_su[63:32];
                        3'b011: r = mul_uu[63:32];
                        3'b100: r = div_q_s;
                        3'b101: r = div_q_u;
                        3'b110: r = div_r_s;
                        3'b111: r = div_r_u;
                        default: r = 0;
                    endcase
                end else begin
                    case (funct3)
                        3'b000: r = ir[30] ? (rs1 - rs2) : (rs1 + rs2);
                        3'b001: r = rs1 << rs2[4:0];
                        3'b010: r = {31'b0, $signed(rs1) < $signed(rs2)};
                        3'b011: r = {31'b0, rs1 < rs2};
                        3'b100: r = rs1 ^ rs2;
                        3'b101: r = ir[30] ? ($signed(rs1) >>> rs2[4:0]) : (rs1 >> rs2[4:0]);
                        3'b110: r = rs1 | rs2;
                        3'b111: r = rs1 & rs2;
                        default: r = 0;
                    endcase
                end
                if (rd_f != 0) rf[rd_f] <= r;
                _commit_alu(pc, ir, rd_f, r, (rd_f != 0));
                pc    <= pc + 4;
                state <= S_FETCH;
            end

            // ── SYSTEM ──────────────────────────────────────────────────────
            OP_SYSTEM: begin : sys
                reg [31:0] cold, cnew;
                cold = csr_read(ir[31:20]);
                if (ir == 32'h00000073) begin       // ECALL
                    trap_cause_r <= 32'hB;
                    trap_tval_r  <= 32'b0;
                    state        <= S_TRAP;
                end else if (ir == 32'h00100073) begin  // EBREAK
                    trap_cause_r <= 32'h3;
                    trap_tval_r  <= pc;
                    state        <= S_TRAP;
                end else if (ir == 32'h30200073) begin  // MRET
                    // Commit mret inline (no memory op)
                    commit_valid      <= 1;
                    commit_pc         <= pc;
                    commit_instr      <= ir;
                    commit_rd_addr    <= 0;
                    commit_rd_data    <= 0;
                    commit_rd_we      <= 0;
                    commit_priv_mode  <= 2'b11;
                    commit_trap_valid <= 0;
                    commit_is_mret    <= 1;
                    commit_trap_epc   <= 0;
                    commit_trap_tvec  <= 0;
                    pc <= csr_mepc;
                    csr_minstret <= csr_minstret + 1;
                    state <= S_FETCH;
                end else begin
                    // CSR instructions
                    case (funct3)
                        3'b001: cnew = rs1;
                        3'b010: cnew = cold | (rs1_f == 0 ? 32'b0 : rs1);
                        3'b011: cnew = cold & ~(rs1_f == 0 ? 32'b0 : rs1);
                        3'b101: cnew = {27'b0, rs1_f};
                        3'b110: cnew = cold | {27'b0, rs1_f};
                        3'b111: cnew = cold & ~{27'b0, rs1_f};
                        default: cnew = cold;
                    endcase
                    case (ir[31:20])
                        MSTATUS:  csr_mstatus  <= cnew;
                        MIE:      csr_mie      <= cnew;
                        MTVEC:    csr_mtvec    <= cnew;
                        MSCRATCH: csr_mscratch <= cnew;
                        MEPC:     csr_mepc     <= cnew;
                        MCAUSE:   csr_mcause   <= cnew;
                        MTVAL:    csr_mtval    <= cnew;
                    endcase
                    if (rd_f != 0) rf[rd_f] <= cold;
                    _commit_alu(pc, ir, rd_f, cold, (rd_f != 0));
                    pc    <= pc + 4;
                    state <= S_FETCH;
                end
            end

            // ── FENCE (NOP for in-order single-hart) ────────────────────────
            OP_FENCE: begin
                _commit_alu(pc, ir, 5'd0, 32'b0, 1'b0);
                pc    <= pc + 4;
                state <= S_FETCH;
            end

            // ── Illegal ─────────────────────────────────────────────────────
            default: begin
                trap_cause_r <= 32'h2;   // illegal instruction
                trap_tval_r  <= ir;
                state        <= S_TRAP;
            end
            endcase
        end // S_EXECUTE

        // ── MEMWAIT: stall until memory responds ─────────────────────────────
        S_MEMWAIT: begin
            if (mem_resp_ready) begin
                mem_req_valid <= 0;
                if (!mem_req_we) begin
                    // Load: sign/zero-extend response
                    begin : ld_ext
                        reg [31:0] raw, ext;
                        raw = mem_resp_rdata;
                        case (mem_width)
                            2'b00: ext = mem_signed_ext ? {{24{raw[7]}},  raw[7:0]}
                                                        : {24'b0, raw[7:0]};
                            2'b01: ext = mem_signed_ext ? {{16{raw[15]}}, raw[15:0]}
                                                        : {16'b0, raw[15:0]};
                            default: ext = raw;
                        endcase
                        load_data <= ext;
                    end
                    state <= S_LOADWB;
                end else begin
                    // Store: emit commit right here, skip S_LOADWB
                    _commit_alu(pc, ir, 5'd0, 32'b0, 1'b0);
                    pc    <= pc + 4;
                    state <= S_FETCH;
                end
            end
        end

        // ── LOADWB: write load result to RF, emit commit ──────────────────────
        S_LOADWB: begin
            if (load_rd != 0) rf[load_rd] <= load_data;
            _commit_alu(pc, ir, load_rd, load_data, (load_rd != 0));
            pc    <= pc + 4;
            state <= S_FETCH;
        end

        // ── TRAP: save PC, jump to mtvec ────────────────────────────────────
        S_TRAP: begin
            csr_mepc   <= pc;
            csr_mcause <= trap_cause_r;
            csr_mtval  <= trap_tval_r;
            begin : trap_b
                reg [31:0] handler; handler = {csr_mtvec[31:2], 2'b00};
                commit_valid      <= 1;
                commit_pc         <= pc;
                commit_instr      <= ir;
                commit_rd_we      <= 0;
                commit_rd_addr    <= 0;
                commit_rd_data    <= 0;
                commit_priv_mode  <= 2'b11;
                commit_trap_valid <= 1;
                commit_trap_cause <= trap_cause_r;
                commit_trap_epc   <= pc;               // mepc = faulting PC
                commit_trap_tvec  <= handler;          // where we jump
                commit_trap_tval  <= trap_tval_r;
                commit_is_mret    <= 0;
                pc <= handler;
            end
            csr_minstret <= csr_minstret + 1;
            state        <= S_FETCH;
        end

        endcase
    end
end

// ── Inline task: emit commit for ALU / non-trap instruction ──────────────────
// Verilog-2001 doesn't support tasks with automatic variables in always blocks
// so we use a macro-style inline assignment (the task is called from named blocks
// above; output assignments are driven directly to the commit_ regs).
task _commit_alu;
    input [31:0] t_pc;
    input [31:0] t_instr;
    input [4:0]  t_rd;
    input [31:0] t_data;
    input        t_we;
    begin
        commit_valid      <= 1;
        commit_pc         <= t_pc;
        commit_instr      <= t_instr;
        commit_rd_addr    <= t_rd;
        commit_rd_data    <= t_data;
        commit_rd_we      <= t_we;
        commit_priv_mode  <= 2'b11;
        commit_trap_valid <= 0;
        commit_trap_cause <= 0;
        commit_trap_epc   <= 0;
        commit_trap_tvec  <= 0;
        commit_trap_tval  <= 0;
        commit_is_mret    <= 0;
        csr_minstret      <= csr_minstret + 1;
    end
endtask

endmodule
`default_nettype wire
