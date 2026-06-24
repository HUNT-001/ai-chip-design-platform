// backends/sim/sim_main.cpp — Verilator harness, AVA v2.0.0
//
// Changes from v1:
//  SCHEMA   All commit-log fields renamed to v2.0.0:
//             mode → priv,  rd → regs,  csr → csrs,  mem → memwrites
//           Added mandatory fields per record:
//             schemaversion "2.0.0", runid, hart 0, instrwidth 4,
//             fpregs null, src "rtl"
//           Added trap sub-fields: epc, tvec, isinterrupt
//           Added memreads (load address + data captured from memory bus)
//  COVERAGE Coverage file renamed rtl.coverage.dat → coverage.dat
//  TRACE    FST native format (--trace-fst) replaces VCD; 5-20x smaller.
//           Compiled with -DVM_TRACE_FST instead of -DVM_TRACE.
//           Falls back to VCD if only VM_TRACE is defined.
//  FLUSH    --flush-every N: commit log flushed every N instructions.
//           Prevents losing the entire log on simulator crash.
//  RISCOF   --sig-out <path> --sig-begin <hex> --sig-end <hex>:
//           Dumps mem[sig_begin..sig_end) as RISCOF hex words at teardown.
//  MANIFEST --runid <str>: injected by run_rtl.py from orchestrator manifest.
//
// Build flags (issued by run_rtl.py, shown for documentation):
//   verilator --cc --exe --build --coverage --coverage-underscore \
//             --assert -O2 --trace-fst                             \
//             -CFLAGS "-DVM_TRACE_FST -DDUT_HEADER='\"Vcpu_top.h\"'" \
//             -CFLAGS "-I<sim_src_dir>"
//
// DUT ports required (see rtl/example_cpu/cpu_top.v and docs/interfaces.md):
//   commit_trap_epc [31:0]  — NEW v2.0.0
//   commit_trap_tvec[31:0]  — NEW v2.0.0

#include <cassert>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

#include "verilated.h"
#include "verilated_cov.h"

// Tracing: FST preferred (5-20x smaller than VCD, natively supported by GTKWave)
#ifdef VM_TRACE_FST
#  include "verilated_fst_c.h"
#  define TRACE_CLASS VerilatedFstC
#elif defined(VM_TRACE)
#  include "verilated_vcd_c.h"
#  define TRACE_CLASS VerilatedVcdC
#endif

#ifndef DUT_HEADER
#  define DUT_HEADER "Vcpu_top.h"
#endif
#include DUT_HEADER
#include "elf_loader.h"

// ── CLI arguments ─────────────────────────────────────────────────────────────
struct Args {
    std::string elf;
    std::string runid         = "unknown";
    std::string commitlog_out = "rtl.commitlog.jsonl";
    std::string coverage_out  = "coverage.dat";     // renamed from rtl.coverage.dat
    std::string trace_out     = "";                 // .fst or .vcd; empty = disabled
    std::string sig_out       = "";                 // RISCOF signature output path

    uint32_t sig_begin = 0x80002000u;
    uint32_t sig_end   = 0x80002040u;

    uint64_t max_insns   = 100000;
    uint64_t flush_every = 1000;       // flush commit log every N instructions
    uint32_t mem_base    = 0x80000000u;
    uint32_t mem_size    = 64u * 1024 * 1024;
    int      seed        = 42;
    bool     verbose     = false;
};

static Args parse_args(int argc, char** argv) {
    Args a;
    for (int i = 1; i < argc; ) {
        std::string k = argv[i++];
        if      (k == "--elf"        && i < argc) a.elf          = argv[i++];
        else if (k == "--runid"      && i < argc) a.runid        = argv[i++];
        else if (k == "--commit"     && i < argc) a.commitlog_out= argv[i++];
        else if (k == "--cov"        && i < argc) a.coverage_out = argv[i++];
        else if (k == "--trace"      && i < argc) a.trace_out    = argv[i++];
        else if (k == "--sig-out"    && i < argc) a.sig_out      = argv[i++];
        else if (k == "--sig-begin"  && i < argc) a.sig_begin    = std::stoul(argv[i++], nullptr, 16);
        else if (k == "--sig-end"    && i < argc) a.sig_end      = std::stoul(argv[i++], nullptr, 16);
        else if (k == "--maxinsns"   && i < argc) a.max_insns    = std::stoull(argv[i++]);
        else if (k == "--flush-every"&& i < argc) a.flush_every  = std::stoull(argv[i++]);
        else if (k == "--membase"    && i < argc) a.mem_base     = std::stoul(argv[i++], nullptr, 16);
        else if (k == "--memsize"    && i < argc) a.mem_size     = std::stoul(argv[i++]);
        else if (k == "--seed"       && i < argc) a.seed         = std::stoi(argv[i++]);
        else if (k == "--verbose")                a.verbose      = true;
        // Plusargs from Verilator (ignore gracefully)
        else if (k.size() > 5 && k.substr(0, 5) == "+sig=") a.sig_out = k.substr(5);
        else { std::cerr << "[sim] Unknown arg: " << k << "\n"; std::exit(1); }
    }
    if (a.elf.empty()) { std::cerr << "[sim] --elf required\n"; std::exit(1); }
    return a;
}

// ── JSON helpers ──────────────────────────────────────────────────────────────
static std::string hex32(uint32_t v) {
    std::ostringstream s;
    s << "0x" << std::hex << v;
    return s.str();
}
static std::string jstr(const std::string& s) { return "\"" + s + "\""; }
static std::string jbool(bool b) { return b ? "true" : "false"; }

// Build one AVA v2.0.0 commit record.
// memreads/memwrites are pre-formatted JSON arrays (may be "[]").
static std::string make_commit_json(
    const std::string& runid,
    uint64_t  seq,
    uint32_t  pc,
    uint32_t  instr,
    int       priv,              // 0=U 1=S 3=M
    bool      rd_we,
    uint8_t   rd_addr,
    uint32_t  rd_data,
    const std::string& memreads_json,
    const std::string& memwrites_json,
    bool      trap_valid,
    uint32_t  trap_cause,
    uint32_t  trap_epc,
    uint32_t  trap_tvec,
    uint32_t  trap_tval,
    bool      is_mret)
{
    static const char* PRIV_STR[] = {"U", "", "S", "M"};

    std::ostringstream o;
    o << "{"
      << "\"schemaversion\":\"2.0.0\","
      << "\"runid\":"    << jstr(runid)        << ","
      << "\"hart\":0,"
      << "\"seq\":"      << seq                << ","
      << "\"pc\":"       << jstr(hex32(pc))    << ","
      << "\"instr\":"    << jstr(hex32(instr)) << ","
      << "\"instrwidth\":4,"
      << "\"priv\":"     << jstr(PRIV_STR[priv & 3]) << ","
      << "\"src\":\"rtl\","
      << "\"regs\":";

    // regs (formerly rd)
    if (rd_we && rd_addr != 0)
        o << "{\"x" << (int)rd_addr << "\":" << jstr(hex32(rd_data)) << "}";
    else
        o << "{}";

    o << ",\"csrs\":{}";          // CSR writes: RTL currently emits via trap record
    o << ",\"fpregs\":null";
    o << ",\"memwrites\":" << memwrites_json;
    o << ",\"memreads\":"  << memreads_json;

    if (trap_valid) {
        bool is_interrupt = (trap_cause >> 31) & 1;
        o << ",\"trap\":{"
          << "\"cause\":"       << jstr(hex32(trap_cause)) << ","
          << "\"epc\":"         << jstr(hex32(trap_epc))   << ","  // NEW
          << "\"tvec\":"        << jstr(hex32(trap_tvec))  << ","  // NEW
          << "\"tval\":"        << jstr(hex32(trap_tval))  << ","
          << "\"isinterrupt\":" << jbool(is_interrupt)     << ","  // NEW
          << "\"is_ret\":"      << jbool(is_mret)
          << "}";
    }

    o << "}";
    return o.str();
}

// ── Memory model ──────────────────────────────────────────────────────────────
class Memory {
public:
    Memory(uint32_t base, uint32_t sz)
        : base_(base), size_(sz), data_(sz, 0) {}

    uint32_t load_elf(const std::string& path) {
        LoadedElf info = elf_load(path, data_.data(), size_, base_);
        std::cerr << "[mem] ELF entry=0x" << std::hex << info.entry
                  << " base=0x" << info.load_base << std::dec << "\n";
        return info.entry;
    }

    uint32_t read32(uint32_t addr) const {
        if (addr < base_ || addr + 4 > base_ + size_) return 0xDEADBEEFu;
        uint32_t o = addr - base_;
        return (uint32_t)data_[o]
             | ((uint32_t)data_[o+1] << 8)
             | ((uint32_t)data_[o+2] << 16)
             | ((uint32_t)data_[o+3] << 24);
    }

    void write32(uint32_t addr, uint32_t val, uint8_t strb) {
        if (addr < base_ || addr + 4 > base_ + size_) return;
        uint32_t o = addr - base_;
        for (int b = 0; b < 4; b++)
            if (strb & (1u << b)) data_[o + b] = (val >> (8 * b)) & 0xFF;
    }

    // HTIF tohost polling (standard test termination)
    uint32_t read_tohost() const {
        static const uint32_t CANDS[] = {
            0x80001000u, 0x80002000u, 0x80003000u
        };
        for (auto a : CANDS)
            if (a >= base_ && a + 4 <= base_ + size_) {
                uint32_t v = read32(a);
                if (v) return v;
            }
        return 0;
    }

    // RISCOF signature dump: mem[begin..end) as hex words
    void dump_signature(const std::string& path,
                        uint32_t begin_addr, uint32_t end_addr) const {
        if (path.empty() || begin_addr >= end_addr) return;
        std::ofstream sf(path);
        if (!sf) {
            std::cerr << "[sig] Cannot open signature file: " << path << "\n";
            return;
        }
        for (uint32_t addr = begin_addr; addr < end_addr; addr += 4) {
            uint32_t word = read32(addr);
            sf << std::setw(8) << std::setfill('0') << std::hex << word << "\n";
        }
        std::cerr << "[sig] Signature written: " << path
                  << " (" << (end_addr - begin_addr) / 4 << " words)\n";
    }

private:
    uint32_t base_, size_;
    std::vector<uint8_t> data_;
};

// ── Pending memory-bus transaction (for memreads/memwrites tracking) ──────────
struct MemTxn {
    bool     valid  = false;
    bool     is_wr  = false;
    uint32_t addr   = 0;
    uint32_t data   = 0;
    uint8_t  strb   = 0;
    uint8_t  size   = 4;
};

static std::string format_memreads(const MemTxn& t) {
    if (!t.valid || t.is_wr) return "[]";
    std::ostringstream o;
    o << "[{\"addr\":" << jstr(hex32(t.addr))
      << ",\"data\":" << jstr(hex32(t.data))
      << ",\"size\":" << (int)t.size << "}]";
    return o.str();
}

static std::string format_memwrites(const MemTxn& t) {
    if (!t.valid || !t.is_wr) return "[]";
    std::ostringstream o;
    o << "[{\"addr\":" << jstr(hex32(t.addr))
      << ",\"data\":" << jstr(hex32(t.data))
      << ",\"size\":" << (int)t.size
      << ",\"strb\":" << jstr(hex32(t.strb)) << "}]";
    return o.str();
}

// ── Main ──────────────────────────────────────────────────────────────────────
int main(int argc, char** argv) {
    const std::unique_ptr<VerilatedContext> ctx(new VerilatedContext);
    ctx->commandArgs(argc, argv);
    Args args = parse_args(argc, argv);
    ctx->randReset(args.seed);

    const std::unique_ptr<DUT_TOP> dut(new DUT_TOP(ctx.get(), "TOP"));

    // Memory model
    Memory mem(args.mem_base, args.mem_size);
    mem.load_elf(args.elf);

    // Commit log output
    std::ofstream commit_f(args.commitlog_out);
    if (!commit_f)
        throw std::runtime_error("Cannot open commit log: " + args.commitlog_out);

    // Optional FST/VCD tracing
#if defined(VM_TRACE_FST) || defined(VM_TRACE)
    std::unique_ptr<TRACE_CLASS> tracer;
    if (!args.trace_out.empty()) {
        ctx->traceEverOn(true);
        tracer.reset(new TRACE_CLASS);
        dut->trace(tracer.get(), 99);
        tracer->open(args.trace_out.c_str());
        std::cerr << "[sim] Trace: " << args.trace_out << "\n";
    }
#else
    if (!args.trace_out.empty())
        std::cerr << "[sim] Warning: trace requested but not compiled in.\n";
#endif

    // Reset sequence (8 half-cycles)
    dut->clk = 0; dut->rst_n = 0;
    dut->mem_resp_rdata = 0; dut->mem_resp_ready = 1;
    for (int i = 0; i < 8; i++) { dut->clk = !dut->clk; dut->eval(); }
    dut->rst_n = 1;

    uint64_t tick    = 0;
    uint64_t seq     = 0;
    uint32_t tohost  = 0;
    MemTxn   pending_txn;   // last memory transaction this cycle

    std::cerr << "[sim] runid=" << args.runid
              << " max_insns=" << args.max_insns
              << " flush_every=" << args.flush_every
              << " seed=" << args.seed << "\n";

    // ── Main simulation loop ─────────────────────────────────────────────────
    while (!ctx->gotFinish() && seq < args.max_insns && tohost == 0) {

        // ── Rising edge ──────────────────────────────────────────────────────
        dut->clk = 1;
        dut->eval();

        // ── Memory bus service (combinational after posedge) ─────────────────
        pending_txn.valid = false;
        if (dut->mem_req_valid) {
            pending_txn.valid = true;
            pending_txn.addr  = dut->mem_req_addr;
            pending_txn.is_wr = dut->mem_req_we;
            pending_txn.strb  = dut->mem_req_wstrb;
            pending_txn.size  = 4;

            if (dut->mem_req_we) {
                mem.write32(dut->mem_req_addr, dut->mem_req_wdata, dut->mem_req_wstrb);
                pending_txn.data  = dut->mem_req_wdata;
                dut->mem_resp_rdata = 0;
            } else {
                uint32_t rd_data   = mem.read32(dut->mem_req_addr);
                pending_txn.data   = rd_data;
                dut->mem_resp_rdata = rd_data;
            }
            dut->mem_resp_ready = 1;
            dut->eval();  // propagate memory response combinationally
        }

        // ── Commit monitor (sample at posedge after memory is stable) ────────
        if (dut->commit_valid) {
            // Only associate pending_txn with this commit if it's not an
            // instruction fetch (fetch addr == commit_pc means it was a fetch)
            bool is_data_txn = pending_txn.valid &&
                               (pending_txn.addr != dut->commit_pc);

            std::string mr = is_data_txn ? format_memreads(pending_txn)  : "[]";
            std::string mw = is_data_txn ? format_memwrites(pending_txn) : "[]";

            std::string rec = make_commit_json(
                args.runid, seq,
                dut->commit_pc, dut->commit_instr,
                dut->commit_priv_mode & 3,
                dut->commit_rd_we,
                dut->commit_rd_addr & 0x1F,
                dut->commit_rd_data,
                mr, mw,
                dut->commit_trap_valid,
                dut->commit_trap_cause,
                dut->commit_trap_epc,    // NEW v2.0.0
                dut->commit_trap_tvec,   // NEW v2.0.0
                dut->commit_trap_tval,
                dut->commit_is_mret);

            commit_f << rec << "\n";

            if (args.verbose)
                std::cerr << "[commit " << seq << "] " << rec << "\n";

            seq++;

            // --flush-every: periodic flush to survive crashes
            if (args.flush_every > 0 && seq % args.flush_every == 0) {
                commit_f.flush();
                if (args.verbose)
                    std::cerr << "[sim] Flushed commit log at seq=" << seq << "\n";
            }
        }

        // ── Trace dump ───────────────────────────────────────────────────────
#if defined(VM_TRACE_FST) || defined(VM_TRACE)
        if (tracer) tracer->dump(tick);
#endif
        tick++;

        // ── Falling edge ─────────────────────────────────────────────────────
        dut->clk = 0;
        dut->eval();
#if defined(VM_TRACE_FST) || defined(VM_TRACE)
        if (tracer) tracer->dump(tick);
#endif
        tick++;

        // ── Termination checks ───────────────────────────────────────────────
        tohost = mem.read_tohost();
        if (tick > args.max_insns * 10) {
            std::cerr << "[sim] Cycle timeout at tick=" << tick << "\n";
            break;
        }
    }

    // ── Teardown ─────────────────────────────────────────────────────────────
    dut->final();
    commit_f.flush();
    commit_f.close();

    // Coverage database — written to coverage.dat (not rtl.coverage.dat)
    ctx->coveragep()->write(args.coverage_out.c_str());
    std::cerr << "[sim] Coverage: " << args.coverage_out << "\n";

    // RISCOF signature dump
    if (!args.sig_out.empty())
        mem.dump_signature(args.sig_out, args.sig_begin, args.sig_end);

#if defined(VM_TRACE_FST) || defined(VM_TRACE)
    if (tracer) { tracer->close(); std::cerr << "[sim] Trace closed.\n"; }
#endif

    std::cerr << "[sim] Done: retired=" << seq
              << " cycles=" << tick / 2
              << " tohost=" << tohost << "\n";

    return (tohost == 0 || tohost == 1) ? 0 : 1;
}
