# Path to the Verification Civilization — Build Requirements

The goal is unchanged: the full VOE / autonomous-verification-organization stack
on the frozen VSA kernel. This lists **everything needed to get there**, mapped
to phases so you provision incrementally. Nothing here changes the goal; it is
the shortest honest path *to* it.

Legend: **have** ✓ · **install** (open-source, free) · **generate** (produced by
running tools you have) · **provide** (needs a decision/resource from you).

---

## The one principle that shapes all requirements

Agents reason, but they can only *know* what real evidence establishes. So the
whole civilization rests on **evidence channels**: simulation, formal, coverage.
Everything else (agents, world models, reputation, society) is computation *on
top of* evidence. Provision the evidence channels first; they are cheap and
open-source.

---

## Tier A — Evidence channels (unblocks Phase 3: one real engineer)

The minimum to make a single autonomous engineer verify **real** Ibex RTL. All
open-source, CPU-only, **no GPU, no LLM, no hardware required.**

| Need | Tool | Status |
|---|---|---|
| Simulation | **Verilator** | ✓ have |
| Python test harness | **cocotb** (`pip install cocotb`) | install |
| Alt SV simulator (small/plain-SV) | **Icarus Verilog** (`iverilog`) | install |
| Formal proof engine | **Yosys + SymbiYosys** + a solver (**Z3** / Boolector / Yices2) | install |
| Coverage | Verilator `--coverage` (line/toggle) | ✓ (in Verilator) |
| Golden ISS (tandem reference) | **Spike** (`riscv-isa-sim`) | install |
| Compile test programs | **riscv-gnu-toolchain** (`riscv64-unknown-elf-gcc`) | install |
| Stimulus generation | **riscv-dv** (pyflow mode) — already in corpus — or AVA's `stimulus_generator` | ✓/install |
| DUT | **Ibex** (+ cv32e40p) | ✓ in `corpus/` |
| Python libs | numpy, scipy, networkx, pandas | install |

With Tier A, the reference kernel (`docs/vsa_reference.py`) swaps its *simulated*
DUT for real Verilator+formal evidence, and one engineer runs for real. Its
hypothesis generation (`Γ`) can start **heuristic/template-based** — no LLM yet.

## Tier B — Learning & data (Phase 3→4: the engineer improves)

| Need | Source | Status |
|---|---|---|
| `(state, action, outcome)` tuples for priors/likelihood | **generate** by running Tier A at scale | generate (CPU-hours) |
| Per-module defect priors (empirical Bayes for `π`) | corpus **git history** (`git log --numstat`, fix-commits) | ✓ generate |
| Coverage time-series (forecasting) | regression runs | generate |
| Compute for generation | **many CPU cores** (simulation throughput) | provide |

No GPU needed yet — this is all simulation + statistics.

## Tier C — Reasoning engines (Phase 4+: smarter, heterogeneous agents)

This is where GPU / LLM enter. Each is *one implementation of a kernel morphism*.

| Engine | Requirement | Status |
|---|---|---|
| **LLM** (hypothesis gen `Γ`, spec reasoning) | **API key** (cloud) *or* **local open model + GPU ≥24 GB** (Qwen2.5-Coder / DeepSeek / Llama via Ollama/vLLM) | provide |
| **GNN** (structural world model) | **GPU** + PyTorch + PyTorch-Geometric; RTL graphs already produced by `rtl_graph` | provide (GPU) |
| **RL planner** | modest **GPU** + the Tier-B data | provide (GPU) |
| **Retrieval / RAG** (specs, docs) | embedding model (`sentence-transformers`, CPU-ok) + **FAISS** | install |
| **Learned behavioral world model** (Δ) | **GPU** + generated simulation-dynamics data | provide (GPU) |
| **Symbolic / SAT** | covered by Tier-A formal tools | ✓ |

## Tier D — Multimodal perception (Phase 6+: the most speculative)

Grounding waveforms, coverage heatmaps, netlists, timing diagrams, PDFs into a
unified latent. **Honest status: the hardware-specific encoders do not exist
off-the-shelf and must be trained** (GPU + labelled data, much self-generated).
Spec/image/PDF can use existing vision-language models (API or local GPU).
Defer until agents do real work — this is not a Phase-3 dependency.

| Need | Requirement | Status |
|---|---|---|
| Waveform / netlist → latent encoders | **train** (GPU + generated data) | later |
| Spec / diagram / PDF understanding | vision-language model (API or GPU) | provide, later |

## Tier E — Silicon / FPGA twin (optional, latest; NOT required for the goal)

Only for hardware-in-the-loop correlation (Phase 7-ish). **Your Arduino/ESP32 do
not apply** — they are MCUs, not FPGAs.

| Need | Requirement |
|---|---|
| Host the RISC-V soft core | **Digilent Arty A7-35T/100T** (Xilinx Artix-7) — the Ibex Demo System's target |
| Bitstream flow | **Vivado** (free WebPACK edition) |
| Debug | **OpenOCD + GDB** over USB (Ibex Demo System supports this) |

---

## Compute summary (what actually matters)

- **CPU cores + RAM + disk** — the workhorse for Phases 1–5 (simulation is CPU).
  More cores = more evidence per hour. A workstation or a cloud CPU instance.
- **One GPU (≥24 GB ideal)** — needed only from Tier C (learned engines / local
  LLM). A single RTX 3090/4090 prototypes; an A100/H100 (cloud) is for scale.
- **Cloud** — optional, for parallel evidence generation and training at scale.

## What is genuinely *external* (everything else is open-source + CPU)

1. **An LLM** for the reasoning agents — a cloud API key *or* a GPU for a local
   model. (A first engineer can run heuristic, no LLM, so this is not blocking
   for Phase 3.)
2. **A GPU** — for the learned world models / GNN / RL / multimodal (Tier C+).
3. **CPU-hours** — for evidence generation (you have the tool: Verilator).

No proprietary EDA tool is required anywhere in Tiers A–D. Formal, simulation,
synthesis, ISS, stimulus, coverage — all open-source.

---

## Build order (the path, respecting your phase plan)

1. **Now, with Verilator + Tier-A installs:** wire real evidence into the kernel;
   run **one engineer** (heuristic `Γ`) on real Ibex. *Gate: Tier A.*
2. **Then:** generate Tier-B data; learn priors; the engineer improves.
3. **Then (needs GPU/LLM):** add reasoning engines → heterogeneous archetypes →
   the VOE (scheduler, resource ledger, judgment bus, reputation).
4. **Then:** specialised engineers, organisation, self-evolution.
5. **Later (GPU + training):** multimodal perception, learned world models.
6. **Optional, last (FPGA):** silicon twin.

Every step plugs into the **frozen kernel**; if any step needs a kernel change,
it is rejected or re-levelled (per `VSA_KERNEL_FREEZE_AND_ROADMAP.md`).
