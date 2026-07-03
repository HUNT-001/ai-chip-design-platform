# Design & Build Plan — T43 Coverage-Directed Stimulus Generator

**Status:** Implemented & tested (AVA v2.22.0, 2026-06-30)
**Module:** `AGENT_H/stimulus_generator.py`
**Owner agent:** AGENT_H (Extended Verification Research Tier)

---

## 1. Why this — it completes the loop

The self-evolving story had three of four pieces:

- **T40 `self_evolving_engine`** — decides *which* holes to chase (RL bandit).
- **T42 `coverage_collector`** — measures *what's covered*, emits holes.
- **missing** — turn a hole back into an actual *test*.
- back to T42 to re-measure.

This module is the missing generation half. It converts a coverage hole /
constraint into a concrete RISC-V instruction **seed**, closing the loop
end-to-end:

```
holes ─▶ self_evolving_engine ─▶ constraints ─▶ StimulusGenerator ─▶ seeds
  ▲                                                                    │
  └───────────── coverage_collector ◀── (run seeds) ◀─────────────────┘
```

## 2. Templates — constraint → directed stimulus

Each bin *kind* has a template that emits assembly aimed at that bin:

| Bin kind | Stimulus |
|---|---|
| `reg:x{n}` | `addi x{n},x0,K` — writes the target register |
| `valclass:{c}` | `li x5,V` with `V` a representative of class `c` |
| `branch:{taken/not_taken}` | set two regs equal/unequal, then `beq`; the emitted next-PC reflects the direction |
| `priv:{M,S,U}` | a record executing in that privilege mode |
| `instr:{mnem}` | the named instruction with safe operands |
| *(unknown)* | random register write (still valid stimulus) |

Constraints come straight from `self_evolving_engine.constraint_for`, so the
generator plugs into the planner with no glue.

## 3. Self-validating by construction

This is the key property. Every template emits **both** the assembly and the
golden commit-log records the seed is expected to produce. `predicted_coverage()`
runs those records through the *real* `CoverageCollector`, and `covers_target()`
asserts the target bin is in the result. So the generator can prove each seed
actually hits its bin — it checks its own work, and the tests assert it for
every template (all six value classes, both branch directions, all three
privilege modes, register and instruction targets).

Because the effect model is the same collector used everywhere else, "the seed
covers bin X" means exactly what it means in the rest of the platform — no
divergent oracle.

## 4. Real self-evolving plugins + end-to-end closure

`make_env()` returns a `(generate, evaluate)` pair:

- `generate(strategy, constraints)` — `directed`/`genetic`/`adversarial` aim a
  seed at each targeted hole; `random` emits arbitrary writes.
- `evaluate(seeds)` — unions `predicted_coverage` over the seeds and returns
  `{covered, bugs, cost}`.

`close_coverage()` wires this into `SelfEvolvingEngine` and runs the loop for
real. The end-to-end test shows generated stimulus driving coverage to **≥95%**
(≥99% for the full finite universe), and — because directed generation earns
more reward than random — the bandit **learns to prefer directed** generation.
That's the directed-random hybrid / coverage-guided / feedback-driven
stimulus-generation ideas, demonstrated rather than asserted.

## 5. Offline / pipeline

`generate_from_holes(holes)` emits a directed seed per hole.
`run_from_manifest` reads the run's `coverage_summary.json` (produced by T42),
generates stimulus for every open hole, self-validates each seed, and writes
`stimulus.json` (seeds + a `seeds_self_validated` count). Wired into
`_run_extended_pipeline` after the self-evolving planner, so a run emits directed
tests for its own open holes. Exported from `AGENT_H/__init__.py`. Standalone:
`python AGENT_H/stimulus_generator.py --manifest run_manifest.json`.

## 6. Test coverage

`tests/test_agents.py::TestStimulusGenerator` — 14 cases: per-template
self-validation (register, all value classes, both branch directions, all
privilege modes, instruction), seed shape, batch, random validity, env plugins,
`generate_from_holes`, manifest round-trip (with self-validation count), and the
**end-to-end `close_coverage` test** (≥95% coverage via generated stimulus +
bandit prefers directed). All pass (validated standalone: 14 in the isolated
harness, wired against the real `coverage_collector` and `self_evolving_engine`).

> Build note: stdlib-only apart from the two sibling AGENT_H modules it composes
> (imported with a package-or-standalone fallback so it tests in isolation). The
> workspace mount truncates recently-grown files, so the full in-repo suite runs
> against the real repo. Additive change (new module + lazy pipeline hook + new
> test class), existing agents unaffected.

## 7. Limitations / next steps

- **Real ISS execution** — seeds are validated against the golden effect model,
  not a live Spike/RTL run; wiring `evaluate` to real tandem-sim coverage is the
  production step (the interface is already the right shape).
- **Richer templates** — cross-coverage (opcode × operand-class), memory-access
  patterns, CSR sequences, and vector (`vsetvli` + element) stimulus.
- **Constraint solving** — for holes needing a specific architectural
  precondition (e.g. a particular trap), add a small constraint solver over the
  register/CSR state instead of fixed templates.
- **Seed minimization / dedup** — fold in `minimizer.py` so emitted stimulus is
  minimal and non-redundant.
