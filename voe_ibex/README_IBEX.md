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
| `rtl/ibex_alu.sv` | Unmodified copy of the corpus `ibex_alu`. |
| `rtl/ibex_alu_mut.sv` | Same file, module renamed, with ONE narrow injected defect (XOR wrong only when `operand_a == 0xCAFEF00D`) — mutation testing. |
| `formal/ibex_alu_fv.sv` | Formal harness: free inputs, independent RV32I golden matching Ibex's `result_o`/`comparison_result_o`/`is_equal_result_o`, per-op-class asserts. |
| `formal/ibex_alu.sby` | 4 clean per-class proofs (`prove_add/logic/shift/cmp`, expect pass) + 1 mutant job (`bug_logic`, expect counterexample), `mode prove` + Z3. |
| `sim/tb_ibex_alu.sv` | Verilator self-checking TB over RV32I base ops. |
| `run_voe_ibex.py` | The VOE with a 5-obligation Ibex board and Ibex-configured channels. |

The golden is derived directly from the RTL semantics: `result_o` mux
(ADD→`a+b`, SUB→`a-b`, logic bitwise, shifts by `b[4:0]`, compares→`{31'0,cmp}`),
comparator (`is_equal=(a-b==0)`, signed/unsigned GTE), so a passing proof means
the real Ibex ALU is equivalent to the reference for those ops — not a tautology.

## Run

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

> Note: yosys's built-in Verilog frontend must parse the full `ibex_alu.sv`.
> With `RV32BNone` the heavy RV32B generate block is elaborated away. If a
> specific SV construct trips the frontend on your toolchain version, paste the
> `sby`/yosys error and it's a localized harness/read fix — the flow and golden
> are unaffected.
