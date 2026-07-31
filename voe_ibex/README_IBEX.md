# VOE scaled to the real ibex_alu

Same VOE, same frozen kernel, real DUT. The board now holds genuine
verification obligations over lowRISC's **ibex_alu** (configured `RV32BNone`),
the actual core ALU from `corpus/ibex_rtl/`. This is the "make it foolproof"
step: proving the pipeline works on real, dependency-carrying RTL rather than a
toy — which is exactly what surfaces real-tool friction.

## What's here (corpus original never modified)

| File | Role |
|---|---|
| `rtl/ibex_pkg.sv` | Minimal vendored package — the two enums `ibex_alu` needs (`alu_op_e`, `rv32b_e`) with the real lowRISC encoding. |
| `rtl/ibex_alu.sv` | **Byte-identical** copy of the corpus `ibex_alu` (`diff` is empty). |
| `rtl/ibex_alu_mut.sv` | Same file, module renamed, with ONE narrow injected defect (XOR wrong only when `operand_a == 0xCAFEF00D`) — mutation testing. |
| `formal/gen_verilog.sh` | Runs **sv2v** to convert ONLY the DUT (pkg + core) to plain Verilog yosys can read. sv2v strips assertions, so the harness is NOT passed through it. |
| `formal/ibex_alu_fv.v` | Plain-Verilog assertion harness, read directly by yosys so `assert`/`assume` survive: free inputs, independent RV32I golden for `result_o`/`comparison_result_o`/`is_equal_result_o`, numeric opcodes matching sv2v's enum encoding. |
| `formal/ibex_alu.sby` | Hybrid read (sv2v'd DUT + direct harness): 4 clean per-class proofs (expect pass) + 1 mutant job (`bug_logic`, expect counterexample). `mode bmc depth 1` — exhaustive for a combinational DUT — + Z3. |
| `sim/tb_ibex_alu.sv` | Verilator self-checking TB over RV32I base ops. |
| `run_voe_ibex.py` | The VOE with a 5-obligation Ibex board and Ibex-configured channels. |

The golden is derived directly from the RTL semantics: `result_o` mux
(ADD→`a+b`, SUB→`a-b`, logic bitwise, shifts by `b[4:0]`, compares→`{31'0,cmp}`),
comparator (`is_equal=(a-b==0)`, signed/unsigned GTE), so a passing proof means
the real Ibex ALU is equivalent to the reference for those ops — not a tautology.

## Run

Simulation needs only Verilator (already installed). Formal needs **sv2v** once
(prebuilt Linux binary from https://github.com/zachjs/sv2v/releases):

```bash
cd ~/tools
wget https://github.com/zachjs/sv2v/releases/latest/download/sv2v-Linux.zip
unzip sv2v-Linux.zip && sudo cp sv2v-*/sv2v /usr/local/bin/
sv2v --version        # confirm

# one-time: convert SV -> plain Verilog for the formal jobs
cd /mnt/e/ai-chip-design-platform/voe_ibex/formal && bash gen_verilog.sh
```

Then:

```bash
cd voe_ibex
python3 run_voe_ibex.py --mock     # pipeline + laws + reputation, no tools
python3 run_voe_ibex.py --real     # real verilator + sby on ibex_alu
```

Direct formal, if you want to see raw verdicts:

```bash
cd formal
sby -f ibex_alu.sby prove_add      # expect PASS (proof)
sby -f ibex_alu.sby prove_cmp      # expect PASS
sby -f ibex_alu.sby bug_logic      # expect FAIL + trace.vcd (the mutant)
```

## Expected (real)

```
ibex_alu.add/logic/shift/cmp  -> proved (deductive)   : the real core ALU, verified
ibex_alu.logic[mut]           -> explorer sim PASSES (defect is 1-in-2^32),
                                 skeptic formal returns the counterexample
final R = 0 ; all kernel laws hold every step ; reputation ranks the engineers
```

Only the DUT and the channel configuration changed from the toy slice — the
kernel, the bus, the ledger, the reputation service and the workers are the same
code. That is the robustness claim: the VOE is DUT-agnostic, and it now stands
on a real RISC-V core ALU.

> Frontend note (hybrid): yosys's built-in reader can't parse Ibex's full
> SystemVerilog, so **sv2v** converts the byte-identical DUT to plain Verilog.
> But sv2v also strips `assert`/`assume` (it targets synthesizable Verilog), so
> the assertion harness is written in plain Verilog and read **directly** by
> yosys instead. The `bug_logic` job is the self-check that this worked: it MUST
> return a counterexample. (An earlier all-sv2v flow made every proof pass
> vacuously; `bug_logic` passing is exactly the signal that caught it.)
> Verilator parses the SV directly, so the simulation path is unchanged.
