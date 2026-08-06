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

### PoC-D: a specialised organisation (`voe_ibex/run_voe_specialists.py`)

Phase 4. Four domain specialists replace the generalist pair, each **owning a
region of the failure space** plus that domain's schemas (`M_s`), crossed with
cognitive archetypes:

| Specialist | Owns | Archetype | Semantic memory highlights |
|---|---|---|---|
| `arith` | `.add` | explorer | carry/borrow, signed overflow — cheap to sample |
| `bitwise` | `.logic`, `.logic[mut]` | skeptic | operand-negate mux; narrow input-specific corruption |
| `shift` | `.shift` | skeptic | **the signedness-demotion bug found on this DUT**; formal-first |
| `compare` | `.cmp` | explorer | signed/unsigned GTE boundary; equality via adder |

Result on the real Ibex ALU: same four proofs and the same mutant catch, closed
in **28 budget units instead of 40** (−30%). The gain is attributable: the shift
specialist's memory says checker bugs in this domain are invisible to sampling,
so it went straight to proof — one step at cost 4, where the generalists spent 8
sampling first. **Expertise showed up as measurably cheaper risk reduction.**

Two invariants are enforced, not assumed:

- **Ownership.** A specialist never bids outside its class and *raises* if asked
  to execute outside it. Any property nobody owns is reported as a
  `** COVERAGE GAP` before work starts and again in the final report — and it
  stays in residual risk, so `R` cannot reach zero while an obligation is
  unowned. (Verified by adding an `ibex_alu.mul` task nobody owns.)
- **Memory cannot certify** (attack sheet 2.3). `M_s` changes *what is tried* —
  method, ordering, effort — and has **no path to `R`**. A specialist with fifty
  recorded failure modes and `difficulty = 0.0` computes exactly the same
  residual risk as one that knows nothing. Domain experience makes an engineer
  faster, never more certain without evidence.

### PoC-E: the board derived from the RTL (`voe_ibex/run_voe_generated.py`)

Every earlier board was hand-written — a human chose the properties, so whatever
the human forgot was invisible. This one reads `ibex_alu.sv` with the
corpus-hardened parser and enumerates what the *design* implies: one obligation
per output port, plus structural checks.

```
module ibex_alu (15 ports, 7 outputs, 0 FSMs, 0 RTL assertions)
generated 8 obligations — 2/7 outputs have a checker, 5 have NONE

final R = 16.000   (NOT zero — and that is the correct answer)
signed off : struct.comb_loops · out.comparison_result_o · out.is_equal_result_o
residual   : out.result_o · out.adder_result_o · out.adder_result_ext_o
             out.imd_val_d_o · out.imd_val_we_o
```

**The finding.** PoC-C/D showed a green board — four proofs, `R = 0.000` — which
reads as "the Ibex ALU is verified". The generated board shows that claim was
narrower than it looked: **five of seven real outputs have no checker at all**,
and `result_o` is proved only for 4 op classes out of ~60 opcodes, so it is
deliberately left unbound rather than counted as covered. Nothing regressed —
what changed is that the gap is now *stated* instead of being absent from the
board.

This is the design rule that makes it work: **naming a property is not checking
it.** Unbound obligations are emitted as *declared but unverifiable*, keep
contributing residual risk, and are listed explicitly. Generating only the
properties we can already check would have produced a board that is complete by
construction — the exact failure mode this platform exists to prevent.

**Closing the gap it found.** Four new formal classes were then written against
exactly the outputs the board named, using semantics read off the RTL:

| Output | Property proved |
|---|---|
| `result_o` | correct for **all 16 RV32I opcodes** (supersedes the four per-op-class proofs) |
| `adder_result_o` | `a−b` for SUB/compare ops, `a+b` otherwise |
| `adder_result_ext_o` | the raw 34-bit sum `{a,1} + (negate ? ~{b,0} : {b,0})` |
| `imd_val_d_o`, `imd_val_we_o` | **tied off to zero for every opcode** under `RV32BNone` |

Result: 7/7 outputs now have a checker and `R` falls 16.000 → 3.000. The
remainder is deliberate — `result_o` is bound with an explicit **scope** ("all 16
RV32I opcodes"), so the generator emits a declared-only remainder obligation for
the RV32B opcode space this build does not implement. A checker covering part of
an output can never be mistaken for one covering all of it.

**A fourth soundness bug, caught by the same discipline.** The first run of the
closed board showed `plumbing` earning simulation passes for `imd_val_d_o` — but
`tb_ibex_alu` compares `result_o` and checks nothing else. Formal is
property-specific by construction (one sby task per assertion set); a single
shared testbench is not, so its pass was being credited to properties it never
examined. `SimChannel` now declares a **coverage scope**, the planner will not
choose simulation outside it, and `execute` refuses outright. Budget fell 44 → 28
once the meaningless runs stopped.

**A third evidence channel, no kernel change.** `StaticChannel` runs real
structural analysis via `AGENT_H.rtl_graph` (89 signals, 145 assigns on the real
ALU) and proves combinational-loop freedom, emitting the same typed, stamped,
hash-verified judgments as simulation and formal. Its warrant is deductive *with
respect to the parsed structure* — a boundary recorded in the witness report
itself, since it inherits parser fidelity and says nothing about post-synthesis
silicon. Adding an entirely new *kind* of evidence required no kernel
modification, which is the freeze claim exercised a third time.

### PoC-F: the first STATEFUL DUT (`voe_fifo/`) — bounded vs proved

Real `cv32e40p_fifo` (byte-identical, `DEPTH=4`). Everything before this was
combinational, where a bounded check with free inputs is already exhaustive. A
FIFO has state, so the same tool result means something weaker. Same properties,
same solver, two modes:

```
bounded   (mode bmc, depth 12)   R = 15.000 -> 14.250   proved = []      nothing closed
unbounded (mode prove, k-induct) R = 15.000 ->  0.000   proved = all 3
```

The property carrying the point is `cnt_o <= DEPTH` — an inductive invariant
(true after reset, preserved because a push is blocked while `full_o` holds).
Twelve cycles of silence lowered risk but discharged nothing; only k-induction
closed the obligations. This is the `bounded_pass` warrant, implemented during
the first hardening pass on the argument that it *would* matter once we left
combinational logic, finally exercised on real sequential RTL.

**The gate failed CLOSED — the best behaviour observed so far.** On the first
attempt `prove_cnt` was a genuine k-induction proof (it passed when run
directly), yet the VOE refused to record it three times: the negative control
returned `UNKNOWN`, so the gate could not confirm the assertions bind and
withheld certification. A **false negative** — the system rejected a true claim.
Every earlier failure mode produced false *positives* (green boards that meant
nothing); this is the first time the machinery erred, and it erred toward
refusing to certify. Root cause was ours: `depth 6` while the mutant's overflow
first appears at step 7, so the base case never reached it. Fixed by depth 12,
plus a distinct **`inconclusive`** status ("base case clean, induction step
failed — may need a strengthening invariant") that no longer hides inside a
generic error.

### PoC-G: organisational foresight (`voe/impact.py`)

Until this point the organisation was purely **reactive** — it could say what it
knew, not what would stop being true if something moved. `ImpactGraph` records
what every claim rests on (source files, hash-pinned; and named assumptions,
which may be other properties) and propagates invalidation transitively.

```
event 1  someone edits ibex_alu.sv
         -> 3 proofs retracted        risk 0.000 -> 15.000
         laws under 'commit': hold          <- rising risk is LEGAL here
         same rise labelled 'update': REJECTED
event 2  the FIFO counter bound is withdrawn
         -> subsystem.no_data_loss (direct)
         -> soc.stream_integrity  (TRANSITIVE, two hops, never mentions a FIFO)
            risk 15.000 -> 32.000
```

**This is not a new primitive — it discharges a debt the foundation already
recorded.** Attack-sheet 3.1 states the requirement ("cross-commit safety ⟺ COI
soundness: show a sound COI that still lets stale evidence certify a post-commit
bug"), and Sem-2′ already reserved exactly one event class in which residual risk
may legitimately *rise*: `commit`. Retraction is that event. The tests verify the
claim rather than assert it: the same risk increase is accepted under `commit`
and **rejected** under `update`, and the kernel exposes nothing new.

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

### The gate is now a system invariant, not a habit

All three incidents were caught because a human wrote a known-bad job and chose
to check it first. That does not scale. The gate is now enforced in code:

- `FormalChannel(..., negative_control="bug_logic")` names a **known-bad task
  that must FAIL**. It is run once, cached, and required before any deductive
  warrant is issued.
- If the control passes, errors, or is not declared, a formal PASS is downgraded
  to **`gate_failed`** — the run is reported, the budget is charged, and
  **nothing is believed**. Risk is not discharged.
- The VOE prints `vacuity gate: ARMED / NOT ARMED` with the reason in every
  report, so a board can never look green without stating why it is credible.

This closes the specific failure mode that a misspelled class define
(`CLASS_SHFT`) yields a harness with zero assertions and a board that "proves"
everything.

### Provenance is verified, not merely stamped

`verify_witness()` re-hashes each cited artifact and compares it to the recorded
`#sha256:` stamp; `audit_knowledge()` runs it across the whole knowledge state
and the VOE reports the result. Missing, altered, unstamped and mock witnesses
are all reported as **unverified** rather than silently accepted — a stamp that
nothing checks is decoration.

### Regression suite

`tests/test_voe_kernel.py` — 23 pure-Python tests (no EDA tools) covering kernel
law enforcement, warrant typing (bounded vs proved vs combinational-complete),
the vacuity gate in all four states, witness tampering detection, bus confluence
and warrant precedence, miscalibration penalties, ledger budgeting, and worker
integration. Full repository: **727 passed, 1 skipped**.

Writing it immediately caught a real orchestrator bug: a gate-blocked action
returns no judgment, and the VOE crashed publishing `None`. Now it charges for
the work, reports `NOT CERTIFIED`, and continues.

---

## 6. Repository map (new work)

| Path | Contents |
|---|---|
| `docs/vsa_reference.py` | The frozen kernel: `Judgment`, `KnowledgeState`, `R`, `width`, `X`, `utility`, `check_laws`. |
| `docs/VSA_v1.0.md`, `VSA_ALGEBRA.md`, `VSA_ATTACK_SHEET.md`, `VSA_KERNEL_FREEZE_AND_ROADMAP.md` | Theory, algebra, falsification targets by dependency tier, freeze decision + phase plan. |
| `phase3/evidence_channels.py` | `FormalChannel` (sby, warrant-correct, **vacuity gate**), `SimChannel` (Verilator, multi-file capable), witness stamping + `verify_witness`/`audit_knowledge`, `--mock`. |
| `tests/test_voe_kernel.py` | 23 regression tests for the kernel-adjacent layers (laws, gate, provenance, bus, reputation, ledger). |
| `phase3/engineer.py` | One autonomous engineer on the frozen kernel. |
| `voe/board.py · bus.py · reputation.py · workers.py · voe.py` | Task board + ledger, judgment bus, reputation, archetypes, scheduler. |
| `voe/specialists.py` | Phase 4: `PropertyClass` (owned failure region), `SemanticMemory` (`M_s`), `Specialist`, `unowned_properties`. |
| `voe/obligations.py` | Phase 4b: derives obligations from real RTL; binds checkers; marks the rest declared-only. |
| `voe_ibex/rtl · formal · sim · run_voe_ibex.py` | Real Ibex ALU slice: vendored pkg, byte-identical DUT, mutant, harness, sby jobs, TB, board. |
| `install_toolchain.sh` | Reproducible Tier-A toolchain install (WSL2/Ubuntu). |

## 7. Honest status & open items

**Demonstrated:** three real evidence channels (simulation, formal, static)
driving a formal epistemic kernel; warrant-correct risk accounting; multi-agent
confluent merge; evidence-derived reputation; law checking at every step;
specialisation as state ownership; obligations derived from the RTL itself; and
**every output of the real lowRISC `ibex_alu` formally proved** — `result_o`
across all 16 RV32I opcodes, both adder outputs, both comparison outputs, the
`imd_val_*` tie-offs, and combinational-loop freedom — with the vacuity gate
armed and all witnesses hash-verified. Four soundness failures were caught by the
system's own negative controls rather than by inspection.

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

# PoC-D  specialised organisation (Phase 4)
cd voe_ibex  && python3 run_voe_specialists.py --real

# PoC-E  board derived from the RTL itself (Phase 4b)
cd voe_ibex  && python3 run_voe_generated.py --real

# regression suite (no EDA tools needed)
pytest tests/test_voe_kernel.py --import-mode=importlib -q

# every demo also runs tool-free:  --mock
```

> **Always check `bug_logic` first.** If the known-bad job passes, the assertions
> are not binding and every green result on that board is vacuous. This ordering
> is the operational form of the platform's core rule: *evidence, not optimism.*

## 9. Roadmap position

Phase 1 (kernel) ✅ · Phase 2 (VOE) ✅ · Phase 3 (one engineer on real evidence) ✅
· Phase 4 (specialised engineers owning real property classes) ✅ — all on the
unchanged VSA v1.0 kernel; the freeze has held through every layer.

Phase 4b (obligations derived from RTL + a third evidence channel) ✅.

**Next — the gap the generated board just exposed:** five real Ibex outputs have
no checker. Closing them means auto-*generating harnesses*, not just obligations
(`AGENT_B/testbench_generator` already emits compilable environments from a
parsed module). That converts declared-only obligations into dischargeable ones
and is now the highest-value work, because the platform can finally see what it
is missing. **Phase 5** (leads, review/sign-off authority via the witness gate,
cross-specialist planning, conflict resolution by kernel merge) follows, and is
worth more once the board is large.
