# First stateful DUT — the real cv32e40p_fifo

Everything verified before this was **combinational**, where a bounded check with
free inputs is already exhaustive: one step covers the entire input space, so a
`bmc` pass legitimately counts as a proof. A FIFO has **state**, and that changes
what the same tool result *means*.

This slice exists to exercise the `bounded_pass` warrant — implemented since the
first hardening pass, unit-tested, but never before demonstrated on real
sequential RTL.

## The DUT

`corpus/cv32e40p/rtl/cv32e40p_fifo.sv`, byte-identical (`diff` empty), 168 lines,
no package dependencies. Wrapped at `DEPTH=4, DATA_WIDTH=4` so sv2v emits
concrete widths. A mutant copy asserts `full_o` one slot late, so a push can
drive the occupancy counter past `DEPTH` — that is the negative control.

## The property that carries the point

```
cnt_o <= DEPTH
```

An **inductive invariant**: true after reset, and preserved by every transition
because a push is blocked while `full_o` holds. Bounded model checking can only
report "no violation within 12 cycles" — the counter might still overflow at
cycle 13. k-induction settles it for all time.

| Stage | sby | Verdict recorded | Effect on risk |
|---|---|---|---|
| bounded | `fifo_bmc.sby`, `mode bmc depth 12` | `bounded_pass` | lowers `R`, **never discharges** |
| unbounded | `fifo_prove.sby`, `mode prove` | `proved` (deductive) | discharges the obligation |

## Result

```
bounded   : R = 15.000 -> 14.250   proved = []            (nothing closed)
unbounded : R = 15.000 ->  0.000   proved = all three
```

Same DUT, same properties, same solver — different warrant, and the kernel
accounts for the difference automatically. Twelve cycles of silence on a design
with state is evidence, not a guarantee, and the platform records it as exactly
that.

## Properties

| Obligation | Meaning |
|---|---|
| `fifo.cnt_bound` | occupancy never exceeds `DEPTH` (inductive) |
| `fifo.flags` | `full_o`/`empty_o` agree with the counter |
| `fifo.no_overflow` | a push against a full FIFO does not change occupancy |

## Run

```bash
cd voe_fifo/formal && bash gen_verilog.sh
sby -f fifo_bmc.sby   bug_cnt    2>&1 | tail -3   # MUST FAIL — gate first
sby -f fifo_prove.sby prove_cnt  2>&1 | tail -3   # expect PASS (real proof)
cd .. && python3 run_voe_fifo.py --real
```

Reset note: the DUT has an async active-low reset, so the harness holds `rst_ni`
low for one cycle and releases it, checking properties only once out of reset.
Without that the initial state is unconstrained and bounded checking fails for
the wrong reason.
