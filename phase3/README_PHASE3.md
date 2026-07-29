# Phase 3 — One Autonomous Engineer on Real Evidence

This is the Phase-3 milestone of the VOE roadmap: a single autonomous
verification engineer that plans by the **frozen VSA kernel** and gathers
**real evidence** from real open-source tools — Verilator (simulation) and
SymbiYosys (formal) — folding each result into the kernel as a typed,
witnessed judgment and driving residual risk `R` down.

Nothing here changes the kernel. `docs/vsa_reference.py` is imported unmodified;
the only swap versus its built-in demo is that the *simulated* DUT is replaced
by real tool output. Hypothesis generation is heuristic / manifest-driven — no
LLM — exactly the Phase-3 plan ("build one engineer before many").

## Files

| File | Role |
|---|---|
| `rtl/alu.sv` | Self-contained synthesizable ALU DUT. `INJECT_BUG` plants a narrow `a==DEAD_BEEF` ADD off-by-one. |
| `formal/alu_fv.sv` | Formal harness: free inputs + independent golden + equivalence assertion. |
| `formal/alu.sby` | SymbiYosys job, tasks `good` (expect proof) and `buggy` (expect counterexample), smtbmc + Z3. |
| `sim/tb_alu.sv` | Verilator self-checking testbench; prints one `SIM_RESULT` line. |
| `evidence_channels.py` | `FormalChannel` (sby) and `SimChannel` (Verilator) → evidence dicts with real witness paths. `--mock` for tool-free runs. |
| `engineer.py` | Imports the frozen kernel; autonomous loop: plan by utility → gather evidence → typed judgment → recompute `R` → re-check laws each step. |

## Run

Tool-free (validates the pipeline + kernel law checks; no toolchain needed):

```bash
cd phase3
python3 engineer.py --mock
```

Real evidence (needs the Tier-A toolchain — `verilator`, `sby`, `z3` — on PATH):

```bash
cd phase3
python3 engineer.py --real
```

Run the channels directly if you want to see raw tool verdicts:

```bash
python3 evidence_channels.py            # real tools if present, else auto-mock
cd formal && sby -f alu.sby good        # just the formal proof
cd formal && sby -f alu.sby buggy       # just the counterexample
# sim only:
verilator --binary --timing -Wno-fatal --top-module tb_alu -Mdir /tmp/good \
    -DNVEC=20000 rtl/alu.sv sim/tb_alu.sv -o Vtb && /tmp/good/Vtb +seed=1
```

## Expected result

```
clean property : sim pass ×3  →  formal PROVED (deductive)      → risk 0
buggy property : sim pass ×3  →  formal COUNTEREXAMPLE          → bug found
final R = 0.000 ; laws all-hold every step ; every belief owns a witness
```

The buggy DUT **passing random simulation but failing formal** is the live
demonstration of **Sem-1 (formal dominance)** — the reason the kernel types
deductive evidence above inductive — on genuine tool output, not a scripted
story.

## How this maps to the kernel

- **Wit-1 (no unjustified knowledge):** every `Judgment` is constructed with a
  witness = a real artifact path (sby status/trace, sim log). Construction
  raises without one.
- **Sem-1 (formal dominance):** a formal proof is `DEDUCTIVE`; its property
  leaves the residual pool. A counterexample marks the property disproven
  (a found bug) — also out of the pool, logged.
- **Struct-2:** `R` is always recomputed, never stored.
- **Safe-1 (prior-robustness):** `R` uses the prior-free Clopper–Pearson bound;
  no optimistic prior can lower it.
- **Fire-1 (generation/adjudication firewall):** channels only *produce*
  evidence; nothing enters `R` without an adjudicated witness.

`n_eff` is incremented conservatively (+1 effective sample per distinct-seed
sim run), not by raw vector count — correlated stimulus must not inflate
confidence (attack-sheet item 3.2). Coverage-weighted effective-N is the next
refinement.

## Pointing this at real Ibex (the next config change, not a rewrite)

The pipeline is DUT-agnostic. To verify `corpus/ibex_rtl/ibex_alu.sv` instead of
the toy ALU:

1. Add `ibex_pkg.sv` (and any imported pkg) to `formal/` and `sim/` file lists —
   `ibex_alu` needs `ibex_pkg::alu_op_e`.
2. Write an `ibex_alu_fv.sv` harness: instantiate `ibex_alu`, drive
   `operator_i/operand_a_i/operand_b_i` free, assert `result_o`/
   `comparison_result_o` against a golden (reuse AVA's `bitmanip_verifier` /
   `alu` goldens for the Zb ops).
3. Point `alu.sby` `[files]` and `read -sv` at the Ibex sources; set the top to
   the new harness.
4. Add real properties to `engineer.py`'s manifest (one per op class). The
   engineer loop, kernel wiring, and law checks are unchanged.

Everything above plugs into the **frozen kernel**; if any step needed a kernel
change it would be rejected or re-levelled per
`docs/VSA_KERNEL_FREEZE_AND_ROADMAP.md`.
