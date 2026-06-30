# Design & Build Plan — T35 Fault-Injection Campaign Engine

**Status:** Implemented & tested (AVA v2.13.0, 2026-06-30)
**Module:** `AGENT_H/fault_injector.py`
**Owner agent:** AGENT_H (Extended Verification Research Tier)

---

## 1. Why this

**Level 12**, **priority #8** (★★★★★ research impact). Every other agent verifies
a DUT; this one verifies the **verification suite itself**. It injects hardware
fault models into a known-good commit log, re-runs the AVA detector panel, and
measures what fraction the panel catches. That is mutation testing applied to
the verification environment — the "who-watches-the-watchers" check — and it is
exactly what Level-12 asks for: *inject faults; measure detection rate and fault
coverage.*

The most valuable output is not the headline number but the **undetected
faults**: each one is a concrete, reproducible blind spot in the current
verification panel, directly pointing at the next agent to build.

---

## 2. Fault models

| Model | Effect |
|---|---|
| `bit_flip` | flip one bit of a register / memory / PC value |
| `stuck_at_0` | force one bit to 0 |
| `stuck_at_1` | force one bit to 1 |
| `register_corruption` | replace a committed register value with a wrong one |
| `memory_corruption` | corrupt a memory read/write value |
| `pc_corruption` | corrupt a program-counter value |

`inject_fault(log, fault)` applies a fault to a **deep copy** (the golden log is
never mutated) and records the ground-truth `old`→`new` values on the `Fault`
descriptor.

---

## 3. The campaign

`FaultCampaign(golden_log, detectors, models, seed)` runs a reproducible
campaign:

1. sample a random fault (model + target chosen from the log's actual
   registers / memory accesses / PCs);
2. inject it into a copy of the golden log;
3. run the detector panel; the fault is **detected** if any detector flags the
   mutated log;
4. aggregate `detection_rate` / `fault_coverage`, a **per-model** breakdown, and
   the list of **undetected** faults.

The default detector panel is the golden-ALU `PipelineVerifier` plus
`CSRVerifier` and `AtomicsVerifier`; any callable `log → bool` can be supplied,
so the panel grows as more agents are added. Detectors are individually
exception-guarded — a crashing detector can never abort the campaign.

The whole campaign is seeded, so results are bit-for-bit reproducible.

---

## 4. What it reveals (example)

On a clean ALU trace the engine reports, per model:

```
bit_flip            1.0     register_corruption  1.0
stuck_at_1          1.0     pc_corruption        0.0   ← blind spot
```

The 1.0s confirm the golden in-order ALU catches every datapath fault; the
`pc_corruption` 0.0 honestly surfaces that the panel has no control-flow checker
for plain ALU PCs — a precise, actionable gap (a branch/PC-stride checker would
close it). This is the engine working as intended: quantifying coverage and
naming the holes rather than masking them.

---

## 5. Report & integration

`run(n)` returns a schema v2.1.0 report with `detection_rate`, `fault_coverage`,
`per_model`, `undetected`, and a coverage `band` (VERIFIED ≥0.9 … CRITICAL
<0.3). It is a **measurement**, so `pass` is always true — it never fails the
DUT. Wired into `_run_extended_pipeline` (`_faultinj` import,
`EXTENDED_AGENTS_AVAILABLE`, writes `fault_report.json`, records
`reports["fault_injection"]`) so every run also self-measures the panel's
detection coverage on its own log. Standalone:
`python AGENT_H/fault_injector.py --rtl rtl_commit.jsonl -n 100 --seed 1`.

---

## 6. Test coverage

`tests/test_agents.py::TestFaultInjector` — 10 cases: register-corruption and
bit-flip injection (with original-untouched and exact bit-flip math), detection
of a register fault by the panel, **100 % coverage** for register faults,
**blind-spot reporting** for PC faults, campaign **determinism** under a fixed
seed, malformed-input robustness, report schema, manifest round-trip.

**Full suite: 295 passed, 1 skipped**, `compileall` clean, orchestrator
self-test passes.

---

## 7. Limitations / next steps

- **Recovery-time / fault-resilience** metrics (Level 12) need traces from
  fault-tolerant cores (lockstep/ECC) that *recover*; the descriptor already
  carries enough to time a recovery window.
- **Broader panels close blind spots** — adding the FP, bit-manip, cache and
  bus verifiers (when their event fields are present) raises memory/PC coverage;
  the panel is intentionally pluggable.
- **Guided injection** — coupling the campaign to the coverage model
  (`AGENT_F`) and the causal engine (`AGENT_G`) turns random injection into a
  reinforcement-learning loop that targets the suite's weakest spots (the
  Self-Evolving-Verification research idea).
- **Multi-bit / temporal faults** (transient pulses across cycles) extend the
  single-value model once cycle-level traces are available.
