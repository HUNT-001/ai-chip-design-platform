# cv32e40p_alu — auto-generated verification environment (AVA AGENT_B)

Synthesised from the parsed RTL interface. **Structure is complete and
compilable; the reference model is a scaffold you must fill** (a generator
cannot infer the DUT's intended function).

- Clock: `clk`  Reset: `rst_n` (active-low)
- Data ports: 16  (13 in, 3 out)
- Detected buses: none auto-detected

## Files
- `cv32e40p_alu_if.sv` — interface + driver/monitor clocking blocks
- `cv32e40p_alu_pkg.sv` — UVM item/driver/monitor/agent/scoreboard/coverage/env/seqs + **refmodel (fill me)**
- `cv32e40p_alu_tb_top.sv` — top: clock/reset gen, DUT connect, `run_test()`
- `cv32e40p_alu_tests.sv` — smoke + constrained-random tests
- `cv32e40p_alu_assertions.sv` — SVA (reset + detected handshakes); `bind` to DUT
- `cv32e40p_alu_smoke_tb.sv` — plain-SV self-checking smoke (no UVM; iverilog)
- `cocotb/` — Python cocotb test (verilator/icarus)
- `cv32e40p_alu.f`, `Makefile`, `regression.yaml`

## Run
```
make TEST=cv32e40p_alu_random_test SIM=xcelium   # UVM
make smoke                                     # plain-SV (iverilog)
cd cocotb && make SIM=verilator                # cocotb
```

## Next step
Open `cv32e40p_alu_pkg.sv`, find `cv32e40p_alu_refmodel::predict()`, and implement
the expected-output computation. The scoreboard already wires it to the monitor.
