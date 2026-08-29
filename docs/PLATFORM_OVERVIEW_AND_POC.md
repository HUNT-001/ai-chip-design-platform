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

### PoC-H: policies as data, and the first held-out verdict

Human archetypes (`explorer`/`skeptic`) were removed as *architecture* and
replaced by `Policy` — a parameter set (ordering, method bias, explore budget,
adaptivity) that can be enumerated, mutated and selected on measured evidence.
Human practice survives as one point in the space (`D-human-org`), the incumbent
to beat. `evaluation.py` measures `E = ΔR / cost` with secondary signals (proof
yield, bugs, steps-to-first-discharge, cost sunk into obligations that never
closed).

**On the development design (`ibex_alu`), the adaptive policy beat the
hand-designed organisation by 90%** — and the system refused to promote it,
because `promote()` rejects any candidate evaluated on a design it was developed
against.

**On a held-out design the margin vanished entirely.** Running the same five
policies against the real pulp `lfsr_8bit` (an 8-way cache-replacement LFSR no
policy had seen):

```
A-random  B-cheapest  C-engineer  D-human-org  E-adaptive
E=1.333   E=1.333     E=1.333     E=1.333      E=1.333
REJECTED: +0.0% vs incumbent (needs > 5%)
```

The 90% advantage did not transfer. It came from choosing formal over simulation
on a board where that choice existed; the LFSR board has no testbench, so every
policy is forced down the same path and all strategies collapse to identical
behaviour. **Held-out evaluation caught a non-generalising result on its first
use** — which is the entire reason for the rule.

**Then the benchmark was fixed, and it promoted RANDOM.** Giving the LFSR a
Verilator testbench made simulation a genuine option, so policies finally faced
an allocation decision. On a single campaign the winner was `A-random` at
E=1.231 (+53.8%), and `promote()` accepted it. Promoting a random ordering policy
is obviously not a discovery — it is the protocol failing exactly where a single
sample cannot separate luck from skill.

**Repeats fixed it.** Seven campaigns per policy, with the accept rule
strengthened so the candidate's *worst* run must still beat the incumbent's mean:

```
E-adaptive   mean 1.231 +/-0.000  worst 1.231   deterministic, consistently ahead
A-random     mean 1.130 +/-0.199  worst 0.842   luck — variance exposed
D-human-org  mean 0.800 +/-0.000                incumbent
PROMOTED: E-adaptive +53.8%, worst run still ahead
```

The random policy was demoted by its own variance, and the first legitimate
promotion is an adaptive policy that re-weights channel choice from realised
risk-per-cost — beating the hand-designed human organisation by 54% on a design
it had never seen.

**Confirmed on real tools** (35 campaigns, Verilator + SymbiYosys + Z3), with the
real run reproducing the mock figures exactly: `E-adaptive` mean E=1.231
(std 0.000) vs incumbent 0.800, vacuity gate armed throughout. This is the first
time the organisation improved itself and the improvement survived a rule built
to reject it.

### Two failures the second design exposed (both caught, both fixed)

Adding `mv_filter` broke the experiment in two informative ways.

**The mutant was ineffective, and the gate caught it.** `bug_sticky` returned
"successful proof by k-induction" — the mutation (`d = q` → `d = 1'b0`) does not
break stickiness, because once the counter reaches THRESHOLD it stops
incrementing and holds, so the flag re-asserts every cycle regardless. The
negative control was therefore vacuous, the gate refused to arm, and **nothing
was promoted on that design**. Correct behaviour on a badly designed experiment.
Replaced with a mutation that genuinely breaks the property: a low sample also
clears the filter, so `q_o` can fall with `clear_i` never asserted.

**`E = ΔR/cost` rewarded proving nothing.** With formal refused on that board,
every policy closed ZERO obligations — yet `B-cheapest` scored **E=5.000** and
ranked first, because simulation lowers residual risk by accumulating effective
samples without settling anything. A policy could nibble indefinitely and
outrank one that proves. Two fixes:

- the primary measure now counts only **risk removed by CLOSING obligations**
  (proof or counterexample); inductive shaving is reported separately as
  `shave` so a large gap between them is visible rather than rewarded;
- promotion additionally **refuses any candidate that closed nothing in any
  campaign**, however cheaply it moved the risk number.

This is the gaming surface flagged earlier — "E is only as honest as the
obligation set" — showing up as a concrete number rather than a worry.

**Then a third failure, and the most serious: a broken testbench REFUTED correct
RTL.** With the metric fixed, `mv_filter` still showed `E=5.000` with
`proofs=0` — because the obligations had been closed by *counterexamples*. The
testbench compared against the previous iteration's `clear_i` while the DUT
responds to the one presented at the current edge: an off-by-one that reported
failures on correct RTL. The kernel recorded them faithfully, because under
Sem-1 a counterexample settles an obligation.

This exposed a genuine asymmetry in the design. **The vacuity gate protected
proofs; nothing protected against false refutations.** A checker that can never
fail cannot issue proofs — but a checker that fails spuriously could close any
property it liked. `SimChannel` now carries a **positive control**, the mirror of
the gate: the testbench must PASS on the known-good variant, and until it does,
its counterexamples are downgraded to `control_failed` and settle nothing. The
VOE reports `sim control : OK / FAILED` beside the vacuity gate.

The general rule, now applied in both directions: **evidence that cannot fail
proves nothing, and evidence that always fails refutes nothing.**

**And the win REPLICATED on a second held-out design.** `mv_filter` (real pulp
majority-vote filter: a sticky output flag and a clear path, both inductive) was
added as a second unseen design, with its own mutant, harness and testbench:

```
                 lfsr_8bit   mv_filter   pooled
E-adaptive         1.231       1.111      1.171   PROMOTED on both
A-random           1.130       1.010      1.070   variance-rejected on both
D-human-org        0.800       0.625      0.712   incumbent
                  (+53.8%)    (+77.8%)
```

The promotion rule is applied **per design and accepted only if it holds on every
one** — a policy that wins on one block and loses on another has fitted that
block, not improved verification. `E-adaptive` won on both.

**Confirmed on real tools only after all four measurement defects were fixed**
(ineffective mutant, gameable metric, insufficient proof depth, false-refuting
testbench). Both designs now show real proofs (3 and 2), the vacuity gate armed,
the simulation positive control OK, and the metric counting only closed
obligations. Every earlier version of this table was wrong in a way that
flattered somebody — which is the point of having reported them.

Honest limitations that remain:

- **Two designs is a replication, not a generalisation claim.** It rules out the
  weakest explanation (the policy suited one block) and nothing stronger.
  `scan_corpus.py` identified 91 further self-contained candidates.
- **Both held-out designs are small sequential control blocks.** They are more
  similar to each other than either is to, say, a cache or a bus. Replication
  across a narrow family is weak evidence for a broad claim.
- **Repeats are near-theatrical for deterministic policies.** `E-adaptive` has
  std = 0 because nothing in it is stochastic; the variance test only bites for
  policies like `A-random`. Real variance requires varying the *environment*
  (tool timing, budgets, obligation order), not just the policy seed.
- **The accept rule is a stated convention, not a statistical protocol.**
  "Worst run beats incumbent mean" is strong enough to reject a lucky winner and
  nothing more; effect size, significance and multiple-comparison correction
  across designs remain open.
- **`E = ΔR/C` is only as honest as the obligation set.** A policy can raise `E`
  by working easy obligations, and the organisation influences what lands on the
  board. RTL-derived obligations (§PoC-E) partly close this, but "who decides
  what counts as an obligation" is the remaining gaming surface and deserves the
  same negative-control treatment proofs received.

### Experiment 2 — a formal-HOSTILE regime (`voe_hostile/`)

Every earlier board rewarded going straight to proof, so the adaptive policy's
win was only meaningful if it was CONTINGENT. This board inverts the economics:
a 32x32 signed multiply (`corpus/rsd` `Multiplier`) checked against a
structurally different shift-and-add reference — true, but out of the solver's
reach (`DONE (TIMEOUT)`), so formal costs 4 and returns nothing — beside a dense
defect (1 vector in 16) that simulation closes for 1.

```
policy         mean E    sim share here   sim share on friendly boards
E-adaptive     1.000        33%                   ~0%
D-human-org    0.667        56%                    —   (incumbent)
B-cheapest     0.462        69%
```

**Contingency is supported, directionally.** The adaptive policy moved from
essentially all-formal on the friendly boards to a third of its budget in
simulation here. The allocation tracked the regime rather than repeating a
habit — which is what the experiment was built to test, and it could have failed.

**But the adaptation is coarse, and the honest headline is how far from optimal
every policy is.** The optimal play on this board is one simulation run: close
`mul.bug` (weight 6) for cost 1 and never touch `mul.equiv`, which nothing can
close. That is `E = 6.0`. The best policy scored `1.000`. All five are within a
factor of two of each other and all are ~6x off optimal.

**Root cause, and it points at the next experiment.** `E-adaptive` learns a
single realised rate PER CHANNEL. On this board formal is cheap for one
obligation (it refutes `bug_equiv` in seconds) and useless for the other (it
times out on `mul.equiv`) — a distinction a per-channel average cannot express.
The policy can adapt BETWEEN designs but not WITHIN one. Conditioning has to
move to per-property/per-context features (structure, prior tool outcomes on
similar obligations), which is the concrete form of the "verification regime
descriptor" idea rather than a design-level label.

**Three broken instruments preceded this valid trial**, and both safeguards
fired: an unsigned reference model made the property false so formal refuted it
in seconds instead of timing out (board not hostile at all), and the same error
in the testbench was caught by the simulation positive control, which refused
every refutation from a checker that disagreed with the known-good DUT. Without
that control, sim-only policies would have "found" bugs in a correct multiplier
across 21 campaigns and posted excellent numbers.

### Experiment 3 — obligation-level planning against an oracle (`voe_hetero/`)

Experiment 2 showed allocation tracks the regime BETWEEN designs but not WITHIN
one. So this board is heterogeneous in a single campaign: six obligations drawn
from three real designs at once, each routed to its own evidence channels —
lfsr and mv_filter invariants (formal closes cheaply), a 32x32 multiplier
equivalence (formal TIMES OUT, nothing closes it), and a dense multiplier defect
(simulation closes for 1).

`F-obligation` conditions on `Omega_i` = (property kind, arithmetic, sequential),
with the structural half **probed from the RTL** by `obligation_state.py`, not
declared. Learning is keyed on the signature, so it transfers to an unseen
obligation with similar structure rather than to a name.

```
human heuristic     E=0.683   (41.5% of oracle)
design-adaptive     E=1.120   (68.0% of oracle)
obligation-adaptive E=1.273   (77.3% of oracle)
ORACLE              E=1.647   formal x4, sim for the defect, SKIP the unprovable
```

The predicted progression appears, on real tools. Transfer is genuine and
cross-design: one signature covers four invariants spanning *two* designs, so
what is learned on the LFSR informs the mv_filter obligations.

**Two limits, one of them irreducible.**

The signature `('equivalence', arith, comb)` covers BOTH multiplier
obligations — and formal times out on one while refuting the other in seconds.
The representation groups two obligations with opposite tool behaviour, which is
where much of the remaining 23% goes.

But refining features cannot close that gap, and this is the important part:
the outcomes differ because one property is TRUE (needs an expensive proof) and
the other is FALSE (needs a cheap counterexample) — and **which of those holds is
the very question the campaign exists to answer.** The oracle is allowed to know
it retrospectively; no policy can know it in advance. So a fraction of the gap to
oracle is not a modelling failure but the cost of not yet knowing the answer,
and any future claim of "approaching the oracle" has to net it out.

### Experiment 4A — the realisable ceiling (`voe_hetero/run_experiment4a.py`)

Experiment 3 measured against a RETROSPECTIVE oracle that knows each property's
truth value in advance. That is not a valid ceiling for a pre-action planner:
`mul.equiv` is expensive because it is TRUE and `mul.bug` is cheap because it is
FALSE, and which is which is exactly what a campaign exists to discover. So a
**realisable oracle** was built — it sees only the obligation signature and the
population statistics of that class, and must pick one fixed action per class.

```
retrospective oracle  E=1.647   knows each truth value in advance
REALISABLE oracle     E=1.556   class statistics only
value of information nobody has yet: 0.092
```

**This corrects the previous write-up.** The claim that a meaningful fraction of
the gap was irreducible was too generous to the learner: the unreachable part is
**0.092 of a 0.374 gap — under a quarter**. The rest was model error. Measured
against the ceiling that can actually be reached:

```
D-human-org     E=0.683    43.9% of realisable
E-adaptive      E=1.120    72.0%
F-obligation    E=1.273    81.8%
G-diagnostic    E=1.287    82.8%
```

The realisable oracle also chooses differently from the retrospective one: it
picks `sim` for the whole equivalence/arithmetic class, because half that class
closes cheaply by counterexample and half does not close at all, so sampling has
better expected yield than paying 4.0 for a coin-flip. That is a strategy a real
planner could adopt; "skip the unprovable one" is not.

**Diagnostic actions: mechanism demonstrated, value unmeasured.** `G-diagnostic`
may spend a 0.25 probe — a very short simulation — where a class has behaved BOTH
ways, to learn which action deserves the 4.0. It reaches 1.287 against
`F-obligation`'s 1.273: about 1%. The mechanism runs, but **this board contains
exactly one ambiguous class**, so there is almost nothing to buy. A 1% edge over
a single ambiguity is evidence the code works, not evidence that diagnosis
matters.

Testing it properly needs a board with many classes whose members genuinely
differ — several true-and-expensive properties interleaved with false-and-cheap
ones sharing structure. That is the concrete requirement the next scale step has
to satisfy, and it is a requirement about *causal diversity*, not about adding
more designs.

### Experiment 5 — the diverse-regime benchmark (`voe_bench/`)

Experiment 4A could not test diagnostic planning: its board had one ambiguous
class. The requirement was not "more designs" but **causal diversity** — classes
whose members genuinely differ. That was satisfiable from what already existed,
because every design here has a mutant. 20 obligations, 6 designs, 6 regimes, and
**every class mixes true properties with mutant-refuted false ones**:

```
('equivalence', False, False)   6 obligations (4 true, 2 false)
('equivalence', True,  False)   2 obligations (1 true, 1 false)
('invariant',   False, True)   11 obligations (8 true, 3 false)
```

Structure cannot separate those members; only evidence can. Results (real tools):

```
G-diagnostic   E=1.256   94.9% of realisable      <- probes when a class is mixed
E-adaptive     E=1.210   91.4%                    <- design-level, one rate per channel
F-obligation   E=1.140   86.0%                    <- obligation-level, no probing
D-human-org    E=0.925   69.8%                    <- incumbent
realisable     E=1.324   |  retrospective E=1.400
```

**Diagnosis pays: +10.3% over the same policy without it**, and reaches 94.9% of
the reachable ceiling. Spending 0.25 to learn which action deserves 4.0 beats
committing blind — the behaviour Experiment 4A predicted but could not measure.

**And a reversal that inverts Experiment 3's headline.** Obligation-level
conditioning is now WORSE than design-level (86.0% vs 91.4%). The 11-member
invariant class holds 8 true and 3 false obligations, so its per-class average
blends two populations that want opposite actions, and acting on that average is
worse than a global rate. **Finer conditioning bought false confidence.** Only
adding the probe recovers it. So conditioning and diagnosis are not independent
improvements: on a genuinely mixed board, conditioning WITHOUT diagnosis is
actively harmful.

**A process failure worth recording.** Experiment 3 was reported without checking
the feature extractor, and it was wrong twice — the arithmetic regex matched `*`
inside `/* */` comments, and `always_comb` was counted as sequential — so
`ibex_alu` was labelled arithmetic and stateful when it is neither. Correcting it
needed a third distinction: `i*4` and `x[2*N*(seg+1)-1:0]` are index arithmetic
that elaborates away, not the datapath multiply that builds the solver's
bit-blast wall. All six designs now verify against expectation
(`test_comments_do_not_create_arithmetic`, `test_index_arithmetic_is_not_a_datapath_multiply`,
`test_always_comb_is_not_sequential`).

**Follow-up: Experiments 3 and 4A were RE-RUN under the corrected extractor and
are byte-identical** — same signatures, same E values (0.683 / 1.120 / 1.273 /
1.287), same conclusions. The caveat that they "should be treated as unverified"
was over-cautious and is withdrawn.

The bug never touched them: it required either a `*` inside a comment or an
`always_comb` block, and Experiment 3's three designs have neither — `lfsr` and
`mv_filter` are genuinely clocked (real `always_ff`), and the multiplier's
`srcA * srcB` is a genuine datapath multiply. Only `toy_alu` and `ibex_alu` were
mislabelled, and neither appears in Experiment 3. Flagging the risk was right;
asserting the result was tainted without checking was not, and the check was
two commands.

That is the sixth instrument defect in this sequence. The kernel and the RTL have
been right every time; the measuring apparatus has been wrong repeatedly.

### Experiment 6 — uncertainty-aware conditioning (`voe_bench/run_controlled.py`)

The benchmark's regression (obligation-level conditioning WORSE than
design-level) had an obvious wrong reading — "conditioning was a mistake" — and a
right one: **uncertainty-blind conditioning was the mistake**. A signature is not
a regime; it is an observation consistent with several. So `voe/regime.py` keeps
a posterior over "does this action close an obligation like this one", pooled
`global <- class <- obligation`, and derives:

    P(a is best)   sampled, not read off a mean
    ambiguity      1 - max_a P(a is best)
    VoD(d)         E_o[max_a E[U|o]] - max_a E[U] - c(d)

`H-uncertainty` probes only when **VoD > 0** — when the probe pays, not merely
when the policy feels unsure.

Results on REAL tools (mock figures in brackets — the divergence matters):

```
arm             E      %realisable   wrong commits   premature loss   probe
D-human-org   0.942      71.2%           1.0              4.0          0.00
F-obligation  1.195      90.2%  [86.0]   1.0              4.0          0.00
G-diagnostic  1.256      94.9%           1.0              4.0          1.00
H-uncertainty 1.244      94.0%  [99.0]   1.0              4.0          2.75
realisable    1.324     100.0%
```

**The principle holds, but this implementation of it does not earn its
complexity.** Diagnosis clearly helps: both diagnosing arms beat both blind
ones. But the honest comparison is not H against F — that is the arm H was
designed to beat. It is **H against G**, the simplest thing that also diagnoses:

```
G-diagnostic   E=1.256   probes on a one-line heuristic ("has this class
                         behaved both ways?"), probe cost 1.00
H-uncertainty  E=1.244   posteriors, sampled P(a is best), priced VoD,
                         probe cost 2.75
H over G: -1.0%
```

H spends 2.75x more on probing and comes out slightly behind. So:

- *never increase specialisation without increasing diagnosability* — **supported**
- *the Bayesian machinery is the right way to do it* — **not supported here**

**Mock overstated the effect** (99.0% vs 94.0% of ceiling for H). The modelled
timeouts and bug densities made the board more favourable to the sophisticated
arm than the real tools do. Every conclusion in this section rests on the real
numbers.

**Two measurement defects had to be fixed before any of these numbers meant
anything, and the second invalidated a conclusion already drawn.**

*Not reproducible.* Two identical real invocations gave `F-obligation` 1.195 and
1.140. `Worker._seed` was `abs(hash(name)) % 1000`, and Python randomises
`hash()` per interpreter, so every process drew different simulation seeds.
Fixed with a stable `zlib.crc32` seed, pinned by a test.

*Repeats that did not repeat anything.* With the seed stabilised, `_seed` still
depended only on the worker NAME — so all five repeats simulated identically and
`std = 0.000` was true by construction. The statistic was not measuring variance;
it was **incapable** of measuring it. The repeat seed now reaches the simulation
seed.

The cost of that second defect was a wrong conclusion, not just a wrong number.
With one seed, H scored 1.244 and LOST to G by 1.0%; with another it scored 1.311
and WON by 4.4%. A comparison was reported as settled in both directions before
the experiment could distinguish them.

FINAL results, repeats varying the simulation seed:

```
arm               E    +/- std   worst   %realisable
D-human-org     0.946 +/-0.012  0.925      71.4%
E-adaptive      1.210 +/-0.000  1.210      91.4%
F-obligation    1.151 +/-0.025  1.140      86.9%
G-diagnostic    1.256 +/-0.000  1.256      94.9%
H-uncertainty   1.284 +/-0.036  1.244      97.0%
realisable      1.324             |  retrospective 1.400
```

- **diagnosis helps — SUPPORTED.** H over F is +11.6%, far outside the spread.
- **H over G is +2.2% against a spread of 0.036 — UNDECIDED.** It does not clear
  twice the noise, and it has already flipped sign once. The posterior/VoD
  machinery's case rests on auditability (per-decision confidence and ambiguity),
  which is real but must be argued on its own terms rather than on an efficiency
  claim the data does not carry.

Every verdict in the script is now gated on clearing the measured spread. An
earlier version announced that the machinery "earns its complexity" in the same
report that declared the comparison undecided — two sections disagreeing because
only one consulted the variance.

**Three bugs had to be fixed first, and the last one is the interesting one.**

1. *No obligation-local level.* The hierarchy was implemented as global <- class
   only, so repeated failures on ONE obligation barely moved a class-wide mean
   and the policy thrashed.
2. *A uniform 0.5 prior over both channels.* Combined with cost-normalised
   utility (`w*p/cost`), that handed the cheap channel a permanent 4x advantage:
   simulation was chosen forever and formal was never sampled, so its posterior
   never left the prior. The fix is not a tuned constant but the kernel's own
   warrant asymmetry — simulation is inductive and closes only by counterexample;
   formal is deductive and closes by proof OR counterexample.
3. *`closed = (gain > 0)`.* **The same inductive-shaving-vs-discharge conflation
   already fixed in the efficiency metric, reintroduced one layer up.** A
   simulation pass lowers `R` via `n_eff` while settling nothing, so the belief
   recorded it as a closure and simulation looked successful every time. The
   policy ran **162 sim passes in a single campaign**. Closure is now read from
   the kernel (`proven or disproven`), never from risk movement.

That third one is worth stating plainly: a distinction the platform had already
identified, documented and tested still recurred in new code. Conceptual clarity
did not prevent the same error being re-made in a different layer — only a test
that watched the mechanism would have.

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

## 8b. The Diagnosability Principle, and the roadmap it implies

The sequence of experiments produced one result worth more than any of the
percentages:

> **Diagnosability Principle.** A refinement of the decision representation may
> improve policy resolution only if the system also gains the ability to detect
> and resolve the additional decision uncertainty that refinement exposes, at
> acceptable cost.

Evidence: obligation-level conditioning made things WORSE than design-level
(86.9% vs 91.4% of the realisable ceiling) until diagnosis was added, at which
point it became the best arm (97.0%). The finer representation was not wrong; it
was under-supported.

**Three uncertainties, kept separate** (collapsing them is what produced the
regression):

| | what it is | what reduces it |
|---|---|---|
| `U_world` | is this property actually true? | verification itself |
| `U_strategy` | which action is best here? | a cheap diagnostic probe |
| `U_model` | how good is my predictor? | learning across obligations |

Diagnosis attacks `U_strategy` only. It does not reveal truth, and the
realisable-vs-retrospective ceiling gap (0.076) is the part of `U_world` no
pre-action policy can price.

**Pre-registration** (`voe/preregistration.py`). Every measurement defect in this
project was caught AFTER a number was reported. The criteria for the one
undecided comparison are now written to disk and hashed BEFORE any campaign runs;
the analysis re-reads them, verifies the hash, and applies exactly that rule.
Committing twice does not overwrite; editing the file marks the result INVALID.
Three outcomes are distinguished — MET, NOT MET, and **UNDERPOWERED**, the last
of which must not be read as evidence against the treatment.

**RESULT (real tools, 12 seeds, 6 design families):**

```
G-diagnostic    E = 1.256 +/- 0.000   probe cost 1.00
H-uncertainty   E = 1.291 +/- 0.038   probe cost 2.75
+2.7% relative against a committed 5% threshold  ->  NOT MET
(H's worst run, 1.229, falls below G's mean)
```

**The Bayesian belief + Value-of-Diagnosis layer does not earn its complexity.**
Nearly 3x the probing cost for a gain that does not clear the bar. The
obligation committed before the data was to SIMPLIFY, and it was honoured:
`policy.RECOMMENDED = DIAGNOSTIC`, and `voe/regime.py` now carries a header
recording that it failed its own test and is not on the default path.

What survives and what does not:

- **diagnosis before commitment** — established (+11.6%, far outside the spread)
- **the Diagnosability Principle** — supported
- **posteriors, sampled P(a is best), priced VoD** — NOT justified on efficiency.
  Retained only for per-decision auditability ("best=formal, confidence=0.51,
  ambiguity=0.49"), which is real but is a different argument.

**This also weakens the case for Phase 5.** The world model's motivating gap was
"the planner needs a better model of which probe is worth running" — and the
measurement found that a one-line heuristic already selects probes as well as a
priced VoD does. Building a predictive layer on top of a component that failed
its own test would compound an unjustified assumption. Phase 5 needs a new
justification, or a different foundation.

The mock run of the same comparison gave +4.6% — also NOT MET, but close enough
to the threshold that a post-hoc rule would have been tempting. That is precisely
what committing the threshold first prevents.

### Institutional memory, and Experiment 6 — finding the heuristic's boundary

**The rejection was recorded, not just applied** (`voe/institutional_memory.py`,
`voe_bench/capability_ledger.json`). An organisation that remembers only its
successes relearns its failures, so the record carries the hypothesis, the
evidence, the decision, **and the conditions under which to reconsider**:
long-horizon boards, coupled obligations, non-stationary environments, large
action spaces, multi-step diagnosis. That list is simultaneously a research
programme — each entry names a regime where the simple policy should break.

Promotion is now priced: `benefit > threshold + uncertainty + complexity`. For
H that bar was **11.5%** against a measured +2.8% — the rejection is clearer once
~3x probing spend is charged rather than ignored.

**Experiment 6 tests the first revisit condition.** Assume-guarantee structure:
a LEMMA (`fifo.cnt_bound`, weight 1, proved by a real `sby` run) unlocks eight
dependents worth 48, which cannot be discharged until it closes. Eight
independently closable distractors of weight 5 give a one-step rule something
more attractive to chase.

```
G-diagnostic (one-step)   E=1.175   closed 47 of 89   cost 40
I-lookahead  (values what an action unlocks)
                          E=1.350   closed 54 of 89   cost 40
                                                      +14.9%
```

**The first regime found where the simple policy fails materially.** A one-step
expected-value rule ranks the lemma by its own weight of 1 and never reaches the
48 behind it.

*The first version of this board found nothing* — it blocked everything except
the lemma, so the greedy policy proved it by elimination rather than foresight.
A horizon test requires the greedy policy to have attractive alternatives; that
correction is what made the experiment able to answer its question.

**Boundary of the claim:** the dependency structure is DECLARED, as an
assume-guarantee contract is, so a planner may legitimately use it. The lemma is
discharged by a real proof, but the dependents' unlocking is modelled. This is a
PLANNER experiment on a partly modelled environment — not a measurement of tool
behaviour.

This is what a derived justification looks like: **the failure came first, the
mechanism second.** Multi-step planning is now motivated by a demonstrated
deficiency rather than chosen because it is the next interesting thing to build.

**Revised roadmap**, reflecting what was actually learned rather than what was
planned:

```
Phase 1  VSA kernel                          complete
Phase 2  Verification environment            complete
Phase 3  Single engineer on real evidence    complete
Phase 4  Adaptive verification strategy      CURRENT
         4a cross-design adaptation          done
         4b regime contingency               done
         4c obligation conditioning          done
         4d diagnosis before commitment      done
         4e belief vs heuristic              RESOLVED: negative (NOT MET)
Phase 5  Predictive world model              justification WEAKENED by 4e —
                                             needs a new one, or a different
                                             foundation than the belief layer
Phase 6  Emergent specialisation             specialists earned, not declared
Phase 7  Multi-agent organisation
Phase 8  Self-improvement
Phase 9  Autonomous verification research
```

Multi-agent work moved DOWN, deliberately. A single engineer that knows what it
does not know is worth more than ten that coordinate confidently.

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
