# What each remaining capability needs (data / hardware) — checklist

**Purpose:** the honest input list for the batch-4 items that can't be built from
nothing. Written so you can gather things later and we pick straight up.

Legend: 🟢 **RTL repo is enough** · 🟡 **repo + generated data** · 🔴 **needs
hardware or an LLM**

---

## 1. 🟢 Works immediately from an RTL repo alone

Send **Ibex** or **BlackParrot** (or CVA6 / PULP / OpenTitan — any real SV/V
codebase) and these become buildable straight away, because they are
**structural / unsupervised** — no labels needed:

| Capability | What it needs | Notes |
|---|---|---|
| **RTL graph construction** (AST, dataflow, control-flow) | `.sv` / `.v` source files | The whole GNN foundation. Ibex ≈ 50 modules is plenty to build and validate the graph builder |
| **Module similarity / clone detection** | same | Graph edit distance + embedding cosine. Works with zero labels |
| **Circuit graph embeddings** | same | Structural features: fan-in/out, depth, cone size, operator mix |
| **Cone-of-influence on real designs** | same | Already built — just needs real netlists to run on |
| **Testbench generation (AGENT_B)** | module port lists + parameters | Generate cocotb / UVM / Verilator harness, monitors, scoreboards, register model |
| **Assertion → formal pipeline** | module interfaces | Feed mined/generated properties into the BMC engine we just built |
| **Security / structural analysis** | same | Unconnected resets, missing default cases, latch inference |

**What to send:** the repo as-is. Most useful subfolders:
- Ibex: `rtl/`, `dv/` (testbench), `examples/`
- BlackParrot: `bp_be/`, `bp_fe/`, `bp_top/`, `bp_common/`

---

## 2. 🟡 Needs the repo **plus** generated data

These are the *supervised* items. The repo gives inputs; we still need labels.
All of the labels below can be produced with **open-source tools**, no licences.

### 2a. Area / timing / power prediction
- **Need:** per-module synthesis results — the labels.
- **How to generate:** Yosys (`stat` → cell count / area), OpenSTA (WNS / TNS /
  critical path), OpenROAD (power). Sweep parameters to multiply data points
  (same module at different widths/depths → different area).
- **Format:** one CSV/JSON row per (module, parameter-set):
  `module, params, area_um2, cell_count, wns_ns, tns_ns, power_mw, tech_node`
- **Volume needed — honest:** a few hundred rows gives a usable *feature-based
  regressor* (gradient boosting on graph features). A true **GNN needs
  thousands+**. With Ibex-scale data I would build the feature regressor and say
  so, rather than ship an under-trained GNN and call it deep learning.

### 2b. Bug localization on real code (Ochiai — engine already built)
- **Need:** test→coverage map + pass/fail results.
- **How to generate:** `verilator --coverage` (or the repo's own DV flow) →
  per-test coverage `.dat`; run the regression to get pass/fail.
- **Format:** `{test_name: [covered_files_or_modules]}` + `{test_name: passed}`.
- Ibex's `dv/` flow can produce this.

### 2c. Assertion mining on real designs
- **Need:** signal traces.
- **How to generate:** simulate with Verilator/Icarus dumping **VCD or FST**;
  we convert to the trace format `mine_assertions()` already accepts.
- **Format:** VCD/FST, or JSONL of `{signal: bool/int}` per cycle.

### 2d. Bug prediction / reopen prediction / lifetime
- **Need:** project history — this is the label source.
- **How to generate:** it already exists in the repo:
  - `git log --numstat` → file churn, commit frequency, author count
  - bug-fix commits (messages matching `fix|bug|issue #`) → defect labels
  - GitHub issues export (JSON) → open/close dates (lifetime), reopen events
- **Format:** git history is enough to start; issues JSON improves it a lot.

### 2e. Coverage forecasting / tape-out readiness (AI digital twin)
- **Need:** a **time series** of past regression runs, not a single snapshot.
- **Format:** per-run JSONL: `{run_id, timestamp, coverage_pct, tests_run,
  bugs_found, cpu_hours}` — 20+ runs makes the curve fit meaningful.

---

## 2f. AGENT_B testbench generation — status & the interface-port gap

**Built and validated** (v2.59.0): AGENT_B synthesises a full UVM + cocotb +
smoke-SV verification environment from a parsed flat port list. It works today
on Ibex, cv32e40p and any core whose top-level ports are flat signals.

**The one real gap — SystemVerilog interface ports.** Modules that carry a bus
as an `interface` port or a packed `struct` (many CVA6 AXI modules,
`AXI_BUS.Slave` style) parse fine, but the protocol is not auto-classified from
the flat name list because the signals live *inside* the interface type. To
close this we would need to also parse the **interface/struct definitions**
(`typedef struct`, `interface AXI_BUS`) and expand a port of that type into its
member signals. That is a bounded, doable extension — it needs the interface
`.sv` files (already in the corpus) — just not yet done. Until then, such
modules generate a correct env without bus-specific stimulus/SVA.

## 3. 🔴 Genuinely needs hardware or an LLM — cannot be faked

| Capability | Blocker | What would unblock it |
|---|---|---|
| **FPGA prototype sync** | Physical board | Any FPGA dev board + bitstream flow; we'd add a JTAG/UART bridge |
| **Emulator sync** | Emulator (Palladium/Veloce/Z1) | Access + its API |
| **Post-silicon correlation** | Actual silicon + ATE data | Test-program logs from the tester |
| **Hardware/software co-validation** | Board or emulator running real SW | Same as FPGA |
| **LLM-driven verification planning** | A runtime LLM | An API key/endpoint the platform may call |
| **RAG verification assistant** | LLM + embedding model | Same, plus a doc corpus to index |
| **Neuro-symbolic / graph transformers** | Training data + compute | The 2a dataset at scale |

For these, the honest build is: **real interfaces + adapters, clearly marked as
requiring the missing input** — never synthetic numbers dressed up as results.

---

## 4. Recommended order (highest value per unit of effort)

1. **Send Ibex and/or BlackParrot now** → I build the RTL graph layer, module
   similarity, clone detection, and AGENT_B testbench generation. No labels
   needed; these are real deliverables today.
2. **Run Verilator coverage + the repo's regression** → unlocks Ochiai bug
   localization and assertion mining on real code.
3. **Run a Yosys/OpenSTA/OpenROAD sweep** → unlocks the area/timing/power
   regressor (feature-based first; GNN only if the dataset gets large).
4. **Export git history / issues** → unlocks bug lifetime and reopen prediction.
5. Hardware items last, if and when a board/emulator exists.

---

## 5. A note on model honesty

Where the data is thin, the right answer is a **simpler model that is defensible**
(linear / gradient-boosted regression over graph features, with reported
confidence intervals) rather than a deep model that looks impressive and
generalises badly. Any predictor shipped here will state its training set size,
its validation method, and its error bars — and will return "insufficient data"
instead of a number when that is the truthful answer.
