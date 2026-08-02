# AVA / VOE — Platform Overview, Architecture, Workflow & Proof of Concept

**Status date:** 2026-07-31 · **Kernel:** VSA v1.0 (FROZEN) · **Toolchain:** validated on
Verilator 5.049, Yosys 0.65, SymbiYosys, Z3 4.15.5, sv2v 0.0.13, Spike, RISC-V GCC 15.2.0, cocotb 2.1.0

This is the single document that explains what the project *is*, what has actually
been built, how the pieces fit together, and what has been demonstrated on real
hardware description code with real tools. Claims here are separated into
**demonstrated** (a command produced the output) and **designed / not yet
demonstrated**. That distinction is the point of the whole platform.

---

## 1. Executive summary

The project builds an **autonomous verification system** for RISC-V hardware, in
three layers:

1. **AVA** — a large library of *verification agents* (71 modules in `AGENT_H`
   alone, 12 agent packages `AGENT_A`…`AGENT_L`): golden-reference checkers for
   ISA behaviour, crypto, vectors, memory models, caches, buses, power, CDC,
   formal analysis, failure/bug/regression analytics. These are the **evidence
   producers**.
2. **VSA** (Verification State Algebra) — a **frozen mathematical kernel** that
   defines what it means to *know* something about a design: typed judgments,
   witnesses, residual risk `R`, exploration value, and laws that make unjustified
   or over-confident conclusions structurally impossible.
3. **VOE** (Verification Operating Environment) — the **society layer**: multiple
   heterogeneous autonomous engineers sharing one canonical knowledge state,
   scheduled by expected risk-reduction per cost, contributing through a typed
   judgment bus, and earning reputation computed purely from evidence.

The distinguishing idea: **the system cannot claim what it has not earned.** A
belief without a witness cannot even be constructed; a bounded check cannot be
recorded as a proof; simulation cannot certify what only formal can settle.

---

## 2. Architecture

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                        LAYER 3 — VOE (society / process)                     │
│                                                                              │
│   ┌────────────┐   proposals    ┌──────────────┐    grants (argmax U/cost)   │
│   │  Workers   │ ─────────────► │  Scheduler   │ ──────────────────────────┐ │
│   │ archetypes │                │   (voe.py)   │                           │ │
│   │ ─ skeptic  │ ◄───────────── └──────────────┘                           │ │
│   │ ─ explorer │    task+method         │                                  │ │
│   └─────┬──────┘                        │ charge                           │ │
│         │ execute                       ▼                                  │ │
│         │                        ┌──────────────┐                          │ │
│         │                        │ResourceLedger│  budget C, cost(a)       │ │
│         │                        │  (board.py)  │                          │ │
│         │                        └──────────────┘                          │ │
│         ▼                                                                  │ │
│   ┌──────────────┐  typed judgment  ┌──────────────┐   merge (confluent)   │ │
│   │  Evidence    │ ───────────────► │ JudgmentBus  │ ─────────────────┐    │ │
│   │  Channels    │  E ⊢ φ : κ       │   (bus.py)   │                  │    │ │
│   └──────┬───────┘                  └──────┬───────┘                  │    │ │
│          │                                 │ miscalibration events    │    │ │
│          │                                 ▼                          │    │ │
│          │                         ┌──────────────┐                   │    │ │
│          │                         │ Reputation   │ ◄─────────────────┘    │ │
│          │                         │(reputation.py)│  evidence only        │ │
│          │                         └──────────────┘                        │ │
└──────────┼─────────────────────────────────────────────────────────────────┼─┘
           │                                                                 │
           ▼                                    ┌────────────────────────────▼──┐
┌──────────────────────────┐                    │  LAYER 2 — VSA KERNEL (frozen) │
│  REAL TOOLS (evidence)   │                    │      docs/vsa_reference.py     │
│                          │                    │                                │
│  Verilator ──► sim pass  │                    │  Judgment(φ, warrant,           │
│    (inductive, n_eff)    │                    │           evidence, witness)    │
│                          │  ───────────────►  │    · witness=None ⇒ ValueError  │
│  SymbiYosys+Z3 ──► proof │   warranted        │  KnowledgeState  K              │
│    (deductive) or a      │   judgments        │  R(K)  residual risk (CP bound) │
│    counterexample trace  │                    │  X(K)  exploration value        │
│                          │                    │  U     utility = exploit+explore│
│  sv2v (SV→V for yosys)   │                    │  check_laws(): Wit-1, Struct-2, │
└──────────────────────────┘                    │                Sem-2', Safe-1   │
           ▲                                    └────────────────────────────────┘
           │
┌──────────┴───────────────────────────────────────────────────────────────────┐
│                    LAYER 1 — AVA agents + DUT corpus                          │
│  AGENT_A semantic · AGENT_B testbench-gen · AGENT_C/D tandem ISS ·            │
│  AGENT_E compliance · AGENT_F/G coverage+causal · AGENT_H (71 verifiers:      │
│  ISA, crypto, vector-crypto, memory model, cache, bus, power, CDC, formal,    │
│  analytics, dashboards) · AGENT_I/J/K/L                                       │
│  corpus/: Ibex, CVA6, cv32e40p, VeeR EH1, BlackParrot, RSD, core-v-verif,     │
│           riscv-dv  (1795 SV/V files)                                         │
└───────────────────────────────────────────────────────────────────────────────┘
```

### Why the kernel is frozen

Every VOE capability had to map onto an existing kernel primitive without
changing the kernel (`docs/VSA_KERNEL_FREEZE_AND_ROADMAP.md`). Several
"operating-system" properties are *consequences* of kernel laws rather than
features:

| VOE mechanism | Kernel basis |
|---|---|
| Concurrent contributions merge without corruption | **Struct-1** confluence |
| Nothing reaches sign-off without adjudicated evidence | **Wit-1** + **Fire-1** firewall |
| A counterexample settles a property over prior passes | **Sem-1** formal dominance |
| Scheduler attention = argmax risk-reduction / cost | Utility **𝒰** |
| Reputation from provenance + calibration | typed ownership **E ⊢ K** |
| Risk never silently improves | **Sem-2'**, **Safe-1** (prior-robust) |

---

## 3. The technical workflow, end to end

```
 (1) OBLIGATIONS            (2) PLAN                    (3) ACT
 ┌──────────────┐      ┌────────────────────┐   ┌───────────────────────────┐
 │ TaskBoard    │      │ each worker scores  │   │ chosen worker runs a real │
 │ φ + weight   │ ───► │ every open φ by 𝒰   │──►│ tool:                     │
 │ (per-op-class│      │ = exploit + λ·explore│   │  sim  → verilator binary  │
 │  properties) │      │   − cost            │   │  formal → sby + Z3        │
 └──────────────┘      │ scheduler grants the│   └─────────────┬─────────────┘
                       │ best utility/cost   │                 │
                       └────────────────────┘                  ▼
 (6) REPORT                 (5) LAWS                  (4) ADJUDICATE
 ┌──────────────┐      ┌────────────────────┐   ┌───────────────────────────┐
 │ signed off   │ ◄─── │ check_laws() every │◄──│ tool output → Evidence    │
 │ bugs found   │      │ step: Wit-1,       │   │ (status + witness path    │
 │ residual     │      │ Struct-2, Sem-2',  │   │  + sha256 stamp)          │
 │ reputation   │      │ Safe-1             │   │ → Judgment (typed warrant)│
 └──────────────┘      │ violation ⇒ HALT   │   │ → bus.publish() → merge   │
                       └────────────────────┘   │ → ledger.charge()         │
                                                └───────────────────────────┘
```

**Warrant typing (the core discipline).** Evidence is not a number, it is a
*kind*:

| Tool outcome | Warrant | Effect on residual risk `R` |
|---|---|---|
| Simulation pass | INDUCTIVE, `n_eff += 1` | lowers `R` gradually (Clopper–Pearson bound) |
| Bounded formal pass on a **stateful** DUT | INDUCTIVE (`bounded_pass`) | lowers `R`, **never to zero** |
| Unbounded proof (k-induction), or bmc on a **combinational** DUT | DEDUCTIVE | property leaves the risk pool entirely |
| Counterexample | disproven + witness trace | property leaves the pool as a **found bug** |

`n_eff` is incremented **+1 per distinct-seed run**, not per vector — correlated
stimulus must not inflate confidence (open item, §7).

---

## 4. Proof of concept — what was actually demonstrated

### PoC-A: one autonomous engineer, synthetic DUT (`phase3/`)

A small ALU with a defect that fires on exactly one input value
(`a == 0xDEADBEEF`). Real Verilator + real SymbiYosys.

```
step 1-3  sim    alu_equiv[clean] -> pass            R 9.750 → 8.158
step 4    formal alu_equiv[clean] -> proved          R 5.000
step 5-7  sim    alu_equiv[dut2]  -> pass  (3×)      R 4.750 → 3.158
step 8    formal alu_equiv[dut2]  -> counterexample  R 0.000
witnesses:
  alu_equiv[clean] [DEDUCTIVE] <- formal/alu_good/status#sha256:4d489b9bfaac73d7
  alu_equiv[dut2]  [INDUCTIVE] <- formal/alu_buggy/engine_0/trace.vcd#sha256:53229316fb17e69f
```

**The result that matters:** random simulation passed the buggy design three
times; formal caught it. Sem-1 (formal dominance) demonstrated on genuine tool
output, with tamper-evident witnesses.

### PoC-B: two engineers sharing one environment (`voe/`)

```
explorer sim ×4  → skeptic formal proves the clean property
explorer sim ×4  → skeptic formal returns the counterexample
                   [refutes explorer's pass]  ⇒ miscalibration recorded
reputation (evidence only): skeptic 1.000 (1 proof, 1 bug) · explorer 0.212
final R = 0.000 · all kernel laws held every step
```

Trust was **earned from evidence**, not declared: the explorer's four passing
simulations were retroactively devalued when formal refuted one of them.

### PoC-C: the real lowRISC `ibex_alu` (`voe_ibex/`)

The board scaled to a **byte-identical copy** of `corpus/ibex_rtl/ibex_alu.sv`
(`diff` empty) plus a mutant copy carrying one narrow defect. Five obligations:
`add`, `logic`, `shift`, `cmp`, and `logic[mut]`.

**Demonstrated:**

- Verilator built and simulated the real Ibex ALU; 20,000 random vectors per run
  matched an independent RV32I golden.
- SymbiYosys + Z3 **proved** all four clean properties — `ibex_alu.add`,
  `ibex_alu.logic`, `ibex_alu.shift`, `ibex_alu.cmp` — equivalent to the golden
  over the entire input space (`DONE (PASS)`), with `bug_logic` simultaneously
  returning `DONE (FAIL)` so the passes are known to be non-vacuous.
- The mutant returned a **real counterexample** (`DONE (FAIL)`, `trace.vcd`),
  refuting the explorer's four passing simulations.
- All kernel laws held at every step; `R` reached 0.000.

**`ibex_alu.shift` — a real reference-model bug, found by formal.** The board
reported a counterexample on the unmodified Ibex RTL. The trace pinned the exact
state:

```
op = 8 (ALU_SRA)   a = 0xFFFFFFFF   b[4:0] = 16
Ibex result_o = 0xFFFFFFFF   (correct: -1 arithmetic-shifted right is -1)
harness golden = 0x0000FFFF  (a LOGICAL shift — wrong)
```

Cause: a Verilog signedness rule. In a conditional (ternary) expression, **if any
operand is unsigned the whole expression is unsigned**, and that propagates into
the branches — silently demoting `$signed(a) >>> amt` to a logical shift. The
sibling branches (`a << amt`, `a >> amt`) are unsigned, so the arithmetic shift
was destroyed. Fixed by using separate `if/else` assignments, where each RHS
keeps its own signedness. **Ibex was correct; the reference model was wrong.**

Why this is the strongest result so far: the *simulation* testbench models the
same operation in a `case` statement with independent assignments, so its golden
was accidentally correct — 20,000 random vectors per run passed. **The two
reference models disagreed with each other, and only formal exposed it.** No
amount of random simulation against that testbench would have found this,
because the bug lived in the checker, not the stimulus. This is exactly the
Sem-1 formal-dominance the kernel encodes, on real silicon-proven RTL.

Throughout, the board **refused to sign the property off** — it stayed under
*bugs found*, never *signed off*.

---

## 5. The most valuable result: three caught vacuity incidents

While bringing up PoC-C, the board showed a **perfect green `R = 0.000` with all
five properties "proved" — and it was wrong — twice.** A deliberate
known-bad job (`bug_logic`, a mutant that *must* fail) caught both:

| # | Symptom | Root cause | Fix |
|---|---|---|---|
| 1 | All 5 "proved", mutant passed | **sv2v stripped every `assert`/`assume`** (it emits synthesizable Verilog). Proofs were vacuous. | Hybrid frontend: sv2v converts the **DUT only**; the assertion harness is plain Verilog read **directly** by yosys. |
| 2 | All 5 "proved" again, mutant passed | Assertions are clocked (`always @(posedge clk)`); at `depth 1` only step 0 exists, no edge occurs, nothing is evaluated. | `depth 2`, documented as required, not cosmetic. |
| 3 | Clean properties failed at the *induction* step | `mode prove` (k-induction) invents unreachable states for a **stateless** DUT. Base case passed. | `mode bmc` + explicit `combinational=True` completeness declaration. |

**A green board is not evidence. The negative control is what makes green
trustworthy.** This is the platform's own thesis (Wit-1: no unjustified
knowledge) validated against itself, and it is a stronger result than a clean
first run would have been.

---

## 6. Repository map (new work)

| Path | Contents |
|---|---|
| `docs/vsa_reference.py` | The frozen kernel: `Judgment`, `KnowledgeState`, `R`, `width`, `X`, `utility`, `check_laws`. |
| `docs/VSA_v1.0.md`, `VSA_ALGEBRA.md`, `VSA_ATTACK_SHEET.md`, `VSA_KERNEL_FREEZE_AND_ROADMAP.md` | Theory, algebra, falsification targets by dependency tier, freeze decision + phase plan. |
| `phase3/evidence_channels.py` | `FormalChannel` (sby, warrant-correct), `SimChannel` (Verilator, multi-file capable), sha256 witness stamping, `--mock`. |
| `phase3/engineer.py` | One autonomous engineer on the frozen kernel. |
| `voe/board.py · bus.py · reputation.py · workers.py · voe.py` | Task board + ledger, judgment bus, reputation, archetypes, scheduler. |
| `voe_ibex/rtl · formal · sim · run_voe_ibex.py` | Real Ibex ALU slice: vendored pkg, byte-identical DUT, mutant, harness, sby jobs, TB, board. |
| `install_toolchain.sh` | Reproducible Tier-A toolchain install (WSL2/Ubuntu). |

## 7. Honest status & open items

**Demonstrated:** real dual-channel evidence (sim + formal) driving a formal
epistemic kernel; warrant-correct risk accounting; multi-agent confluent merge;
evidence-derived reputation; law checking at every step; a real proof and a real
counterexample on the actual Ibex ALU; three self-caught vacuity failures.

Plus one **real reference-model bug found and fixed** (§4, PoC-C): a Verilog
signedness demotion that turned an arithmetic shift into a logical one, invisible
to 20,000 random simulation vectors because the simulation checker modelled it
differently and was accidentally correct.

**Open:**

1. **`n_eff` semantics** — currently a conservative +1 per seed. Coverage-weighted
   effective-N is the planned refinement (attack sheet 3.2). Interface is already
   correct, so this is a value change, not a redesign.
3. **Scale** — 5 properties on one combinational block. Sequential DUTs will
   exercise the `bounded_pass` path that is currently implemented but only
   unit-demonstrated.
4. **Hypothesis generation (`Γ`)** is heuristic/manifest-driven. LLM/GNN engines
   are Tier-C (roadmap Phase 4+), gated on a GPU; not required for what is built.

## 8. How to run everything

```bash
# one-time toolchain (WSL2 Ubuntu)
bash install_toolchain.sh
cd ~/tools && wget https://github.com/zachjs/sv2v/releases/latest/download/sv2v-Linux.zip \
  && unzip sv2v-Linux.zip && sudo cp sv2v-*/sv2v /usr/local/bin/

# PoC-A  one engineer, synthetic DUT
cd phase3    && python3 engineer.py --real

# PoC-B  two engineers, VOE
cd voe       && python3 run_voe.py --real

# PoC-C  real Ibex ALU
cd voe_ibex/formal && bash gen_verilog.sh
sby -f ibex_alu.sby bug_logic    # MUST be DONE (FAIL) — trustworthiness gate
sby -f ibex_alu.sby prove_add    # DONE (PASS)
cd ..        && python3 run_voe_ibex.py --real

# every demo also runs tool-free:  --mock
```

> **Always check `bug_logic` first.** If the known-bad job passes, the assertions
> are not binding and every green result on that board is vacuous. This ordering
> is the operational form of the platform's core rule: *evidence, not optimism.*

## 9. Roadmap position

Phase 1 (kernel) ✅ · Phase 2 (VOE) ✅ · Phase 3 (one engineer on real evidence) ✅
· **next:** resolve §7.1, then Phase 4 — specialised engineers owning real
property classes, scaling the board across Ibex and the wider corpus.
