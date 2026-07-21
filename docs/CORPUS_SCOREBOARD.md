# RTL Corpus Scoreboard

Structural analysis of the checked-in `corpus/` RTL, produced by
`AGENT_H/rtl_graph.py`. This is the validation evidence that the parser
generalises across houses/styles, not just Ibex.

Regenerate with:

```bash
python -m AGENT_H.rtl_graph --rtl-dir corpus/<repo> --json /tmp/<repo>.json
```

## Results (parser v2.58.0)

| Repo | Modules | LOC | FSMs | Clone pairs | Assertions | Comb-loop modules |
|---|--:|--:|--:|--:|--:|--:|
| Cores-VeeR-EH1 | 117 | 26,379 | 1 | 959* | 32 | 3 |
| black-parrot | 159 | 36,145 | 3 | 164 | 15 | 13 |
| cv32e40p | 153 | 35,518 | 6 | 144 | 170 | 7 |
| cva6 | 387 | 79,048 | 3 | 436 | 572 | 30 |
| ibex (alu only) | 1 | 1,392 | 0 | 0 | 0 | 0 |

\* 820 of VeeR's 959 clone pairs are the `ram_NxM` parametrised-memory family —
a genuine structural finding, not noise.

## What this validates

- **FSM extraction generalises.** Recovered exactly: Ibex `ibex_controller`
  (10 states, 17/17 transitions), VeeR JTAG TAP (16 states, 32 transitions),
  cv32e40p controller/debug/mult (6/3/5 states). Different naming idioms
  (`*_cs/*_ns`, `state/nstate`, `*_CS`), direct and ternary next-state
  assignments, deeply nested case arms — all handled.
- **Assertion accounting is real** — 572 in CVA6, 170 in cv32e40p, across
  backtick-macro, `assert property`, and immediate-`assert` idioms.
- **Combinational-loop detection is trustworthy on real code** — 0 false
  positives on the Ibex ALU (versioned/SSA resolution of blocking assignments),
  while the counts above flag modules worth a human look (they are *candidates*;
  some are generate-heavy modules the parser cannot fully elaborate — see the
  per-module `parse_warnings`).
- **Clone detection finds real duplication** — VeeR RAM macros, flip-flop
  library-cell variants (`rvdff_fpga`/`rvdffs_fpga`), adder variants.

## Honest limits

- The parser is structural, **not** an elaborator: `generate` blocks and
  parameters are not evaluated. Modules using them carry a `parse_warnings`
  note, and their instance/loop results are best-effort.
- `riscv-dv` and `core-v-verif` are generator / UVM-verification repos, not RTL;
  their SystemVerilog is testbench code, so RTL-graph metrics there are sparse
  by design. They are valuable for **AGENT_B** (testbench-generation reference),
  not for the RTL graph.
- Chisel-based cores (riscv-boom, and anything emitting Verilog from Scala) are
  only analysable if their **generated** Verilog is checked in; the parser does
  not read Chisel/Scala.
