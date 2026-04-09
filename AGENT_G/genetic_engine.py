"""
rv32im_testgen.genetic_engine
==============================
Genetic algorithm engine for adaptive test generation.

Architecture
------------
The GeneticEngine closes the AVA feedback loop between Agent G (test generator)
and Agents D (bug hunter) + F (coverage director):

    Agent F  →  coveragesummary.json  (cold_paths)
    Agent D  →  bugreport.json        (hypotheses)
                    │
                    ▼
              GeneticEngine.evolve(constraints)
                    │
          ┌─────────┴───────────┐
          │  Seed population     │  ← ALL_DIRECTED_TESTS (20 baseline)
          │  + biased random     │  ← generate_biased(80, constraints)
          └──────────┬──────────┘
                     │  fitness ranking  (DirectedTest.evaluate_fitness)
                     │  elitism         (keep top 20 %)
                     │  crossover       (splice asm_body sequences)
                     │  mutation        (replace / insert / delete instructions)
                     ▼
             evolved population → ELFs → Agents B/C/D/F

Constraint schema (union of Agent F and Agent D formats)
---------------------------------------------------------
Agent F cold-path entry::

    {
        "module":       "mul_overflow",   # opcode/module keyword
        "reachability": 0.05,             # 0.0 = never hit
        "line":         "rtl/mul.sv:142"  # optional source location
    }

Agent D hypothesis entry::

    {
        "module":      "div_zero",        # opcode/module keyword
        "hypothesis":  "div-by-zero path not exercised",
        "confidence":  0.8,               # 0.0–1.0
        "step":        1234               # simulation step where observed
    }

Both formats are unified in ``evolve()``'s ``constraints`` parameter.
"""

from __future__ import annotations

import logging
import random as _random_module
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .directed_tests import ALL_DIRECTED_TESTS, DirectedTest
from .random_gen import GeneratorConfig, RV32IMRandomGenerator
from .asm_builder import (
    build_directed_asm,
    build_evolved_asm,
    write_test,
    TOOLCHAIN_PREFIX,
)

logger = logging.getLogger(__name__)


# ─── Individual (test specimen) ───────────────────────────────────────────────

@dataclass
class Individual:
    """
    A single test specimen in the genetic population.

    Attributes
    ----------
    name
        Unique identifier for this specimen.
    asm_lines
        Assembly instruction lines (already indented, no prologue/epilogue).
    fitness
        Computed fitness score (higher = better coverage contribution).
    targets
        Cold-path / hypothesis module names this individual targets.
    generation
        Evolutionary generation in which this individual was created.
        Generation 0 = seed population (baseline directed + biased random).
    parents
        Names of parent individuals (empty for generation-0 seeds).
    crossover_point
        Index into ``asm_lines`` at which crossover was applied (−1 if none).
    """

    name: str
    asm_lines: List[str]
    fitness: float = 0.0
    targets: List[str] = field(default_factory=list)
    generation: int = 0
    parents: List[str] = field(default_factory=list)
    crossover_point: int = -1

    def to_evolution_meta(self) -> Dict[str, Any]:
        """Return a dict suitable for ``write_test(evolution_meta=…)``."""
        return {
            "fitness":            self.fitness,
            "targets":            self.targets,
            "evolved_generation": self.generation,
            "evolved_from":       self.parents,
            "crossover_point":    self.crossover_point if self.crossover_point >= 0 else None,
        }


# ─── Genetic engine ───────────────────────────────────────────────────────────

class GeneticEngine:
    """
    Genetic algorithm engine for AVA test-generation feedback loop.

    Parameters
    ----------
    seed
        Base random seed.  Same seed + same constraints → same evolved tests.
    population_size
        Total number of individuals maintained per generation.
    generations
        Number of evolutionary cycles to run.
    elite_fraction
        Fraction of top-fitness individuals carried forward unchanged.
    mutation_rate
        Per-instruction probability of mutation (replace/insert/delete).
    crossover_rate
        Fraction of new offspring produced by crossover vs. mutation-only.
    output_count
        Number of top individuals to return / write as ELFs.

    Example
    -------
    ::

        engine = GeneticEngine(seed=42, population_size=100, generations=10)
        elf_paths = engine.evolve(
            constraints=constraints,
            outdir=Path("rundir/outputs/test_binaries"),
            assemble=True,
        )
    """

    def __init__(
        self,
        seed: int = 42,
        population_size: int = 100,
        generations: int = 10,
        elite_fraction: float = 0.20,
        mutation_rate: float = 0.15,
        crossover_rate: float = 0.60,
        output_count: int = 50,
    ) -> None:
        if population_size < 4:
            raise ValueError(f"population_size must be ≥ 4, got {population_size}")
        if generations < 1:
            raise ValueError(f"generations must be ≥ 1, got {generations}")
        if not (0.0 < elite_fraction < 1.0):
            raise ValueError(f"elite_fraction must be in (0,1), got {elite_fraction}")
        if not (0.0 <= mutation_rate <= 1.0):
            raise ValueError(f"mutation_rate must be in [0,1], got {mutation_rate}")
        if not (0.0 <= crossover_rate <= 1.0):
            raise ValueError(f"crossover_rate must be in [0,1], got {crossover_rate}")
        if output_count < 1:
            raise ValueError(f"output_count must be ≥ 1, got {output_count}")

        self.seed           = seed
        self.population_size = population_size
        self.generations    = generations
        self.elite_fraction = elite_fraction
        self.mutation_rate  = mutation_rate
        self.crossover_rate = crossover_rate
        self.output_count   = output_count

        self._rng  = _random_module.Random(seed)
        self._rgen = RV32IMRandomGenerator(
            GeneratorConfig(seed=seed, length=50, mem_size=256)
        )

        # Monotonic counter for unique individual names within this engine instance
        self._name_counter: int = 0

        # Statistics collected across generations
        self.generation_stats: List[Dict[str, Any]] = []

    # ── Public API ────────────────────────────────────────────────────────────

    def evolve(
        self,
        constraints: List[Dict],
        outdir: Optional[Path] = None,
        assemble: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Run the full evolutionary cycle and return evolved test metadata.

        Steps
        -----
        1. Build generation-0 seed population from directed baseline +
           constraint-biased random sequences.
        2. For each generation: rank by fitness, select elite, breed via
           crossover and mutation to refill the population.
        3. Write the top ``output_count`` individuals to *outdir* as ``.S``
           (and ``.elf`` if toolchain available).
        4. Return a list of result dicts (one per individual written).

        Parameters
        ----------
        constraints
            Unified Agent F + Agent D constraint list.
        outdir
            Directory for output files.  If ``None``, files are not written
            (fitness ranking and stats are still computed).
        assemble
            Passed through to ``write_test()``.

        Returns
        -------
        List[Dict]
            One dict per written test containing:
            ``name``, ``fitness``, ``targets``, ``generation``,
            ``artifacts`` (S/meta/elf paths), ``asm_line_count``.
        """
        logger.info(
            "GeneticEngine: seed=%d pop=%d gen=%d constraints=%d",
            self.seed, self.population_size, self.generations, len(constraints),
        )

        # ── Generation 0: seed population ────────────────────────────────────
        population = self._build_seed_population(constraints)
        population = self._rank(population, constraints)

        # ── Evolutionary loop ─────────────────────────────────────────────────
        for gen in range(1, self.generations + 1):
            n_elite  = max(1, int(len(population) * self.elite_fraction))
            elite    = population[:n_elite]
            children = self._breed(elite, self.population_size - n_elite, gen, constraints)
            population = self._rank(elite + children, constraints)

            best   = population[0].fitness if population else 0.0
            mean   = (sum(i.fitness for i in population) / max(len(population), 1))
            logger.info(
                "  gen %02d/%02d  pop=%d  best=%.2f  mean=%.2f",
                gen, self.generations, len(population), best, mean,
            )
            self.generation_stats.append({
                "generation": gen,
                "best_fitness": round(best, 3),
                "mean_fitness": round(mean, 3),
                "population":   len(population),
            })

        # ── Write top-N to disk ───────────────────────────────────────────────
        top_n  = population[: self.output_count]
        results: List[Dict[str, Any]] = []

        for ind in top_n:
            asm_src = build_evolved_asm(
                name=ind.name,
                body_lines=ind.asm_lines,
                generation=ind.generation,
                targets=ind.targets,
                fitness=ind.fitness,
            )
            meta: Dict[str, Any] = {
                "type":       "evolved",
                "name":       ind.name,
                "fitness":    ind.fitness,
                "targets":    ind.targets,
                "generation": ind.generation,
                "parents":    ind.parents,
            }

            artifacts: Dict[str, Optional[str]] = {"S": None, "meta": None, "elf": None}
            if outdir is not None:
                artifacts = write_test(
                    name=ind.name,
                    asm_src=asm_src,
                    metadata=meta,
                    outdir=Path(outdir),
                    assemble=assemble,
                    evolution_meta=ind.to_evolution_meta(),
                )

            results.append({
                "name":           ind.name,
                "fitness":        ind.fitness,
                "targets":        ind.targets,
                "generation":     ind.generation,
                "parents":        ind.parents,
                "asm_line_count": len(ind.asm_lines),
                "artifacts":      artifacts,
            })

        avg_fitness = (
            sum(r["fitness"] for r in results) / max(len(results), 1)
        )
        logger.info(
            "GeneticEngine complete: %d tests written, avg_fitness=%.3f",
            len(results), avg_fitness,
        )
        return results

    # ── Population seeding ────────────────────────────────────────────────────

    def _build_seed_population(
        self,
        constraints: List[Dict],
    ) -> List[Individual]:
        """
        Build generation-0 population.

        Composition:
        * All 20 baseline directed tests (converted to Individuals).
        * ``population_size − 20`` constraint-biased random sequences.
        """
        population: List[Individual] = []

        # Convert baseline directed tests → Individuals
        for test in ALL_DIRECTED_TESTS:
            ind = Individual(
                name=self._next_name("dir"),
                asm_lines=list(test.asm_body),
                generation=0,
                targets=list(test.targets),
            )
            population.append(ind)

        # Fill remainder with biased random sequences
        n_random = max(0, self.population_size - len(population))
        if n_random > 0 and constraints:
            biased = self._rgen.generate_biased(
                population=n_random,
                constraints=constraints,
                bias_ratio=0.70,
            )
            for b in biased:
                ind = Individual(
                    name=self._next_name("rnd"),
                    asm_lines=b["sequence"],
                    generation=0,
                    targets=b["targets"],
                )
                population.append(ind)
        elif n_random > 0:
            # No constraints → pure random fill
            for _ in range(n_random):
                seq, _ = self._rgen.generate()
                ind = Individual(
                    name=self._next_name("rnd"),
                    asm_lines=seq,
                    generation=0,
                )
                population.append(ind)

        return population

    # ── Fitness ranking ───────────────────────────────────────────────────────

    def _rank(
        self,
        population: List[Individual],
        constraints: List[Dict],
    ) -> List[Individual]:
        """
        Score every individual against *constraints* and return sorted
        population (highest fitness first).

        Fitness formula (per individual):
        * +1.0 per constraint whose ``module`` substring appears in the
          individual's assembly lines or target list.
        * +0.5 bonus for cold paths (reachability < 0.2).
        * +0.2 per unique M-extension opcode present in asm_lines
          (encourages opcode diversity within the M-extension family).
        * −0.1 per duplicate line (penalises trivially repeated sequences).
        """
        for ind in population:
            ind.fitness = self._score(ind, constraints)
        population.sort(key=lambda i: i.fitness, reverse=True)
        return population

    def _score(
        self,
        ind: Individual,
        constraints: List[Dict],
    ) -> float:
        """Compute fitness score for a single individual."""
        score = 0.0
        lines_text = " ".join(ind.asm_lines).lower()
        target_text = " ".join(ind.targets).lower()

        # Constraint coverage
        for c in constraints:
            module = str(c.get("module", "")).lower()
            if module and (module in lines_text or module in target_text):
                score += 1.0
                if float(c.get("reachability", 1.0)) < 0.2:
                    score += 0.5   # cold-path bonus

        # M-extension opcode diversity bonus
        from .random_gen import _R_TYPE_M
        m_ops_present = {
            op for op in _R_TYPE_M
            if f"    {op}" in " ".join(ind.asm_lines)
        }
        score += 0.2 * len(m_ops_present)

        # Repetition penalty
        if len(ind.asm_lines) > 1:
            n_unique = len(set(ind.asm_lines))
            duplication_ratio = 1.0 - (n_unique / len(ind.asm_lines))
            score -= 0.1 * duplication_ratio * len(ind.asm_lines)

        return max(score, 0.0)

    # ── Breeding: crossover + mutation ────────────────────────────────────────

    def _breed(
        self,
        elite: List[Individual],
        n_children: int,
        generation: int,
        constraints: List[Dict],
    ) -> List[Individual]:
        """
        Produce *n_children* new individuals from the elite pool.

        Strategy:
        * ``crossover_rate`` of offspring produced by 1-point crossover of
          two randomly selected elite parents.
        * Remaining offspring produced by cloning one elite parent and
          applying mutation.
        * All offspring receive random mutation at ``mutation_rate``.
        """
        children: List[Individual] = []

        for _ in range(n_children):
            if (
                self._rng.random() < self.crossover_rate
                and len(elite) >= 2
            ):
                p1 = self._rng.choice(elite)
                p2 = self._rng.choice([e for e in elite if e.name != p1.name] or elite)
                child = self._crossover(p1, p2, generation)
            else:
                parent = self._rng.choice(elite)
                child = Individual(
                    name=self._next_name("mut"),
                    asm_lines=list(parent.asm_lines),
                    generation=generation,
                    targets=list(parent.targets),
                    parents=[parent.name],
                )

            child = self._mutate(child, constraints)
            children.append(child)

        return children

    def _crossover(
        self,
        p1: Individual,
        p2: Individual,
        generation: int,
    ) -> Individual:
        """
        Single-point crossover: take a prefix from p1 and a suffix from p2.

        Crossover point is chosen uniformly at random within the shorter
        parent's body; a minimum of 1 line is taken from each parent.
        """
        lines1 = p1.asm_lines
        lines2 = p2.asm_lines
        max_point = min(len(lines1), len(lines2)) - 1
        if max_point < 1:
            # One parent too short to split — clone the longer one
            child_lines = list(lines1 if len(lines1) >= len(lines2) else lines2)
            cx_point    = -1
        else:
            cx_point    = self._rng.randint(1, max_point)
            child_lines = lines1[:cx_point] + lines2[cx_point:]

        return Individual(
            name=self._next_name("cx"),
            asm_lines=child_lines,
            generation=generation,
            targets=list(set(p1.targets + p2.targets)),
            parents=[p1.name, p2.name],
            crossover_point=cx_point,
        )

    def _mutate(
        self,
        ind: Individual,
        constraints: List[Dict],
    ) -> Individual:
        """
        Apply random mutations to *ind*'s asm_lines.

        Three mutation operators (chosen with equal probability per slot):
        * **Replace** — swap one instruction for a new constraint-biased one.
        * **Insert**  — add a new instruction at a random position.
        * **Delete**  — remove one instruction (minimum 1 line kept).

        The number of mutation sites is Poisson-distributed with mean
        ``mutation_rate × len(asm_lines)``.
        """
        lines = list(ind.asm_lines)
        if not lines:
            return ind

        # Compute number of mutations (at least 1 if mutation_rate > 0)
        import math
        expected = max(1.0, self.mutation_rate * len(lines))
        # Draw from a simple geometric distribution: mean = expected
        n_mutations = 0
        p = 1.0 / (1.0 + expected)
        while self._rng.random() > p and n_mutations < len(lines):
            n_mutations += 1
        n_mutations = max(1, n_mutations)

        # Extract module names from constraints for targeted replacement
        modules = [
            str(c.get("module", "")).lower()
            for c in constraints
            if c.get("module")
        ]

        for _ in range(n_mutations):
            if not lines:
                break
            op = self._rng.choice(["replace", "insert", "delete"])

            if op == "replace" or len(lines) <= 1:
                idx = self._rng.randint(0, len(lines) - 1)
                new_line = self._random_instruction(modules)
                lines[idx] = new_line

            elif op == "insert":
                idx = self._rng.randint(0, len(lines))
                new_line = self._random_instruction(modules)
                lines.insert(idx, new_line)

            else:   # delete
                if len(lines) > 1:
                    idx = self._rng.randint(0, len(lines) - 1)
                    lines.pop(idx)

        return Individual(
            name=ind.name,
            asm_lines=lines,
            generation=ind.generation,
            targets=ind.targets,
            parents=ind.parents,
            crossover_point=ind.crossover_point,
        )

    # ── Instruction helpers ───────────────────────────────────────────────────

    def _random_instruction(self, modules: List[str]) -> str:
        """
        Return a single random instruction line, biased toward *modules*.

        If modules are available, 70 % of calls produce an M-extension or
        module-targeted instruction; 30 % are purely random.
        """
        from .random_gen import (
            _R_TYPE_M, _R_TYPE_INT, _I_TYPE_ALU,
            _MODULE_OPS, _SRC_REGS, _DEST_REGS,
        )

        dest_pool = list(_DEST_REGS)
        rd  = self._rng.choice(dest_pool)
        rs1 = self._rng.choice(list(_SRC_REGS))
        rs2 = self._rng.choice(list(_SRC_REGS))

        if modules and self._rng.random() < 0.70:
            # Targeted: choose opcode from a relevant module
            m = self._rng.choice(modules)
            ops = _MODULE_OPS.get(m, [])
            if not ops:
                for key in sorted(_MODULE_OPS.keys(), key=len, reverse=True):
                    if key in m:
                        ops = _MODULE_OPS[key]
                        break
            if not ops:
                ops = list(_R_TYPE_M)
            op = self._rng.choice(ops)
        else:
            op = self._rng.choice(list(_R_TYPE_M) + list(_R_TYPE_INT))

        return f"    {op:<8} {rd}, {rs1}, {rs2}"

    # ── Utility ───────────────────────────────────────────────────────────────

    def _next_name(self, prefix: str) -> str:
        self._name_counter += 1
        return f"evo_{prefix}_{self._name_counter:05d}"

    # ── Stats ─────────────────────────────────────────────────────────────────

    def summary(self) -> Dict[str, Any]:
        """Return a JSON-serialisable summary of the evolutionary run."""
        if not self.generation_stats:
            return {}
        best  = max(s["best_fitness"] for s in self.generation_stats)
        final = self.generation_stats[-1]["mean_fitness"]
        return {
            "generations_run":    len(self.generation_stats),
            "peak_best_fitness":  best,
            "final_mean_fitness": final,
            "per_generation":     self.generation_stats,
        }
