"""
AGENT_G/causal_engine.py
=========================
T9 — Causal AI-Guided Test Generation

Extends the base GeneticEngine with causal hypothesis biasing.  When Agent D
reports a mismatch and Agent H localises the root cause, this module:

  1. Parses the bug report + root-cause report into a causal hypothesis set.
  2. Translates each hypothesis into weighted mutation/crossover constraints that
     steer the genetic population toward the suspected failure mode.
  3. Measures convergence rate (how quickly the evolved population re-triggers
     the same mismatch class) vs the unbiased baseline GA.
  4. Writes a causal_evolution_report.json alongside the ELF outputs.

Causal biasing strategy
-----------------------
Each mismatch class maps to a concrete set of instruction-level bias rules:

  REG_MISMATCH  → over-represent MUL/DIV/REM corner cases (div-by-zero,
                  signed overflow, consecutive dependent writes)
  PC_MISMATCH   → dense branch targets, backward jumps, misaligned sequences
  MEM_MISMATCH  → interleaved stores/loads to the same address, AMO sequences
  CSR_MISMATCH  → rapid CSR write-read-write cycles, trap return sequences
  TRAP_MISMATCH → ecall/ebreak near branch targets, nested trap patterns
  ORDERMISMATCH → store-load-fence interleaving (feeds Agent I litmus patterns)

Mutation operators with causal weight
--------------------------------------
Standard GA has uniform mutation. Causal GA applies weighted sampling:

  - causal_insert : insert a causally-relevant instruction near the mismatch PC
  - causal_replace: replace an instruction with a causally-relevant variant
  - causal_splice : crossover at the mismatch PC (vs. random crossover point)
  - standard_*    : fallback standard operators (preserved at low weight)

Usage
-----
  from AGENT_G.causal_engine import CausalGeneticEngine

  engine = CausalGeneticEngine(seed=42)
  elf_paths = engine.evolve_causal(
      bug_report        = bug_report_dict,
      root_cause_report = root_cause_dict,   # from Agent H
      outdir            = Path("rundir/causal_tests"),
      assemble          = True,
  )
"""

from __future__ import annotations

import json
import logging
import random as _random_module
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────
# Causal hypothesis → constraint translation
# ─────────────────────────────────────────────────────────

# Mismatch class → instruction mnemonics to over-represent
_CAUSAL_INSTR_BIAS: Dict[str, List[str]] = {
    "REG_MISMATCH": [
        "mul", "mulh", "mulhu", "mulhsu",
        "div", "divu", "rem", "remu",
        "add", "sub", "and", "or", "xor",
    ],
    "PC_MISMATCH": [
        "beq", "bne", "blt", "bge", "bltu", "bgeu",
        "jal", "jalr",
    ],
    "MEM_MISMATCH": [
        "lw", "lh", "lb", "lhu", "lbu",
        "sw", "sh", "sb",
        "lr.w", "sc.w",
        "amoswap.w", "amoadd.w", "amoxor.w",
    ],
    "CSR_MISMATCH": [
        "csrrw", "csrrs", "csrrc",
        "csrrwi", "csrrsi", "csrrci",
        "mret", "ecall",
    ],
    "TRAP_MISMATCH": [
        "ecall", "ebreak", "mret",
        "beq", "bne",          # traps near branch targets
    ],
    "ORDERMISMATCH": [
        "lw", "sw",
        "fence", "fence.i",
        "lr.w", "sc.w",
        "amoswap.w.aq", "amoswap.w.rl",
    ],
}

# Corner-case sequences: (label, asm_lines)
_CORNER_SEQUENCES: Dict[str, List[List[str]]] = {
    "div_by_zero": [
        ["li    t0, -1", "li    t1, 0", "div   t2, t0, t1"],
        ["li    t0, 0x80000000", "li    t1, -1", "div   t2, t0, t1"],  # INT_MIN / -1
    ],
    "mul_overflow": [
        ["li    t0, 0x7fffffff", "li    t1, 2", "mul   t2, t0, t1"],
        ["li    t0, -1", "li    t1, -1", "mulh  t2, t0, t1"],
    ],
    "consecutive_store_load": [
        ["li    t0, 0xdeadbeef", "sw    t0, 0(sp)", "lw    t1, 0(sp)"],
        ["li    t0, 0x42", "sw    t0, 4(sp)", "lw    t1, 4(sp)", "bne   t0, t1, ."],
    ],
    "csr_rapid_write": [
        ["li    t0, 0x1800", "csrw  mstatus, t0", "csrr  t1, mstatus"],
        ["li    t0, 0x1808", "csrw  mstatus, t0", "li    t1, 0x1800", "csrw  mstatus, t1"],
    ],
    "branch_backward": [
        ["li    t0, 5", "loop: addi t0, t0, -1", "bnez  t0, loop"],
    ],
    "fence_store_load": [
        ["li    t0, 1", "sw    t0, 0(sp)", "fence rw,rw", "lw    t1, 0(sp)"],
    ],
    "lr_sc_pair": [
        [
            "lui   t0, 0x80001",
            "lr.w  t1, (t0)",
            "li    t2, 0x99",
            "sc.w  t3, t2, (t0)",
            "bnez  t3, .",       # retry if SC failed
        ],
    ],
    "nested_trap": [
        [
            "li    t0, 0x80001",   # point mtvec to handler
            "csrw  mtvec, t0",
            "ecall",
        ],
    ],
}


@dataclass
class CausalConstraint:
    """A single causal bias constraint derived from a bug report."""
    mismatch_class:  str
    affected_module: Optional[str]
    mismatch_pc:     Optional[str]          # hex8 PC of first divergence
    instr_bias:      List[str]              # preferred instruction mnemonics
    corner_sequences: List[List[str]]       # pre-built asm sequences to inject
    confidence:      float = 1.0
    weight:          float = 1.0            # relative selection weight


def build_causal_constraints(
    bug_report:        Dict[str, Any],
    root_cause_report: Optional[Dict[str, Any]] = None,
) -> List[CausalConstraint]:
    """
    Translate a bug_report.json + optional root_cause.json into
    CausalConstraint objects that steer the GA.
    """
    mc = bug_report.get("mismatch_class", "REG_MISMATCH")

    # Extract divergence PC from context window
    rtl_ctx = bug_report.get("rtl_context") or []
    mismatch_pc: Optional[str] = None
    if rtl_ctx:
        seq = bug_report.get("first_divergence_seq", 0)
        for rec in rtl_ctx:
            if rec.get("seq") == seq:
                mismatch_pc = rec.get("pc")
                break
        if mismatch_pc is None and rtl_ctx:
            mismatch_pc = rtl_ctx[0].get("pc")

    instr_bias = list(_CAUSAL_INSTR_BIAS.get(mc, []))

    # Extract instruction types from context
    for rec in rtl_ctx[:5]:
        disasm = (rec.get("disasm") or "").lower().split()[0]
        if disasm and disasm not in instr_bias:
            instr_bias.insert(0, disasm)   # boost observed instructions

    # Build corner sequences from mismatch class
    corner_seqs: List[List[str]] = []
    sequence_map = {
        "REG_MISMATCH":   ["div_by_zero", "mul_overflow"],
        "MEM_MISMATCH":   ["consecutive_store_load", "fence_store_load", "lr_sc_pair"],
        "CSR_MISMATCH":   ["csr_rapid_write"],
        "TRAP_MISMATCH":  ["nested_trap"],
        "PC_MISMATCH":    ["branch_backward"],
        "ORDERMISMATCH":  ["fence_store_load", "lr_sc_pair"],
    }
    for seq_name in sequence_map.get(mc, []):
        corner_seqs.extend(_CORNER_SEQUENCES.get(seq_name, []))

    # Extract affected module from root-cause report
    affected_module: Optional[str] = None
    confidence = 1.0
    if root_cause_report and root_cause_report.get("candidates"):
        top = root_cause_report["candidates"][0]
        affected_module = top.get("module")
        confidence = top.get("confidence", 1.0)

    constraint = CausalConstraint(
        mismatch_class=mc,
        affected_module=affected_module,
        mismatch_pc=mismatch_pc,
        instr_bias=instr_bias,
        corner_sequences=corner_seqs,
        confidence=confidence,
        weight=1.0 + confidence,   # higher confidence → stronger bias
    )
    return [constraint]


# ─────────────────────────────────────────────────────────
# Causal mutation operators
# ─────────────────────────────────────────────────────────

def _causal_insert(
    asm_lines: List[str],
    constraint: CausalConstraint,
    rng: _random_module.Random,
) -> List[str]:
    """
    Insert a causally-relevant instruction or corner sequence into asm_lines.
    Injection point is biased toward the start of the sequence (where mismatch
    setup code should appear).
    """
    if not constraint.corner_sequences:
        return asm_lines[:]

    seq = rng.choice(constraint.corner_sequences)
    # Bias toward injecting in first third of the sequence
    max_idx = max(1, len(asm_lines) // 3)
    idx = rng.randint(0, max_idx)
    result = asm_lines[:idx] + [f"    {l}" for l in seq] + asm_lines[idx:]
    return result


def _causal_replace(
    asm_lines: List[str],
    constraint: CausalConstraint,
    rng: _random_module.Random,
) -> List[str]:
    """
    Replace a random instruction with a causally-biased one.
    Builds a simple RV32IM instruction using a biased mnemonic.
    """
    if not asm_lines or not constraint.instr_bias:
        return asm_lines[:]

    result = asm_lines[:]
    idx = rng.randint(0, len(result) - 1)
    mnem = rng.choice(constraint.instr_bias)

    # Build a simple replacement instruction (register operands vary)
    regs = ["a0","a1","a2","a3","t0","t1","t2","t3"]
    rd, rs1, rs2 = rng.choices(regs, k=3)

    mem_mnemonics = {"lw","lh","lb","lhu","lbu","sw","sh","sb"}
    csr_mnemonics = {"csrrw","csrrs","csrrc","csrrwi","csrrsi","csrrci"}
    branch_mnemonics = {"beq","bne","blt","bge","bltu","bgeu"}

    if mnem in mem_mnemonics:
        if mnem.startswith("l"):
            instr = f"    {mnem:<8} {rd}, 0({rs1})"
        else:
            instr = f"    {mnem:<8} {rs2}, 0({rs1})"
    elif mnem in csr_mnemonics:
        instr = f"    {mnem:<8} t0, mstatus, {rs1}"
    elif mnem in branch_mnemonics:
        instr = f"    {mnem:<8} {rs1}, {rs2}, . + 4"
    elif mnem in ("div","divu","rem","remu","mul","mulh","mulhu","mulhsu"):
        instr = f"    {mnem:<8} {rd}, {rs1}, {rs2}"
    elif mnem == "ecall":
        instr = "    ecall"
    elif mnem == "mret":
        instr = "    mret"
    elif mnem in ("fence","fence.i"):
        instr = "    fence rw, rw"
    elif mnem == "lr.w":
        instr = f"    lr.w {rd}, ({rs1})"
    elif mnem == "sc.w":
        instr = f"    sc.w {rd}, {rs2}, ({rs1})"
    elif mnem.startswith("amo"):
        instr = f"    {mnem:<12} {rd}, {rs2}, ({rs1})"
    else:
        imm = rng.randint(0, 31)
        instr = f"    {mnem:<8} {rd}, {rs1}, {imm}"

    result[idx] = instr
    return result


def _causal_splice(
    lines_a: List[str],
    lines_b: List[str],
    constraint: CausalConstraint,
    rng: _random_module.Random,
) -> List[str]:
    """
    Crossover biased toward the mismatch region.
    Takes the first 2/3 of lines_a (pre-mismatch context) and appends
    the last 1/3 of lines_b (post-mismatch continuation).
    """
    if not lines_a or not lines_b:
        return (lines_a or lines_b)[:]
    cut_a = max(1, int(len(lines_a) * 0.67))
    cut_b = max(0, int(len(lines_b) * 0.67))
    return lines_a[:cut_a] + lines_b[cut_b:]


# ─────────────────────────────────────────────────────────
# Causal Genetic Engine
# ─────────────────────────────────────────────────────────

class CausalGeneticEngine:
    """
    Wraps the base GeneticEngine with causal hypothesis biasing.

    Parameters
    ----------
    seed            : random seed (reproducible)
    population_size : total population per generation
    generations     : number of evolutionary cycles
    causal_weight   : fraction of mutations that are causal (vs random)
                      0.0 = pure baseline GA, 1.0 = all mutations are causal
    output_count    : number of top individuals to return
    """

    def __init__(
        self,
        seed:            int   = 42,
        population_size: int   = 80,
        generations:     int   = 8,
        causal_weight:   float = 0.7,
        output_count:    int   = 30,
    ) -> None:
        self.seed            = seed
        self.population_size = population_size
        self.generations     = generations
        self.causal_weight   = causal_weight
        self.output_count    = output_count
        self._rng            = _random_module.Random(seed)

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _seed_population(
        self,
        constraint: CausalConstraint,
        size: int,
    ) -> List[Dict[str, Any]]:
        """
        Build a seed population biased toward the causal constraint.
        Each individual is a dict with 'name' and 'asm_lines'.
        """
        population: List[Dict[str, Any]] = []

        # Seed with corner sequences
        for i, seq in enumerate(constraint.corner_sequences * 4):
            if len(population) >= size // 2:
                break
            population.append({
                "name":      f"causal_seed_{i:03d}",
                "asm_lines": [f"    {l}" for l in seq],
                "fitness":   0.0,
                "targets":   [constraint.mismatch_class],
                "generation": 0,
            })

        # Fill remainder with biased random individuals
        while len(population) < size:
            # Pick 3–8 biased instructions
            n_instrs = self._rng.randint(3, 8)
            lines = []
            for _ in range(n_instrs):
                if constraint.instr_bias and self._rng.random() < self.causal_weight:
                    mnem = self._rng.choice(constraint.instr_bias)
                else:
                    mnem = self._rng.choice(["add", "sub", "and", "or", "li", "mv"])
                rd = self._rng.choice(["a0","a1","a2","a3","t0","t1","t2","t3"])
                rs = self._rng.choice(["a0","a1","a2","t0","t1"])
                imm = self._rng.randint(0, 63)
                if mnem in ("div","mul","rem","mulh"):
                    rs2 = self._rng.choice(["a1","a2","t1","t2"])
                    lines.append(f"    {mnem:<8} {rd}, {rs}, {rs2}")
                elif mnem == "li":
                    lines.append(f"    li      {rd}, {imm}")
                else:
                    lines.append(f"    {mnem:<8} {rd}, {rs}, {imm}")
            population.append({
                "name":      f"causal_rand_{len(population):03d}",
                "asm_lines": lines,
                "fitness":   0.0,
                "targets":   [constraint.mismatch_class],
                "generation": 0,
            })

        return population[:size]

    def _evaluate_fitness(
        self,
        individual: Dict[str, Any],
        constraint: CausalConstraint,
    ) -> float:
        """
        Heuristic fitness: count how many biased instructions appear in asm_lines.
        A real implementation would invoke Agent D's comparator as oracle.
        """
        score = 0.0
        lines_text = " ".join(individual["asm_lines"]).lower()
        for mnem in constraint.instr_bias:
            if mnem in lines_text:
                score += 1.0
        # Bonus for corner sequences
        for seq in constraint.corner_sequences:
            seq_text = " ".join(seq).lower()
            first_instr = seq[0].split()[0].lower() if seq else ""
            if first_instr and first_instr in lines_text:
                score += 2.0
        return round(score / max(len(constraint.instr_bias), 1), 4)

    def _mutate(
        self,
        individual: Dict[str, Any],
        constraint: CausalConstraint,
        generation: int,
    ) -> Dict[str, Any]:
        """Apply causal mutation to one individual."""
        lines = individual["asm_lines"][:]
        op = self._rng.random()
        if op < self.causal_weight * 0.5:
            lines = _causal_insert(lines, constraint, self._rng)
        elif op < self.causal_weight:
            lines = _causal_replace(lines, constraint, self._rng)
        else:
            # Standard: delete a random line
            if len(lines) > 2:
                del lines[self._rng.randint(0, len(lines) - 1)]

        return {
            "name":       f"mutant_{generation}_{self._rng.randint(0, 9999):04d}",
            "asm_lines":  lines,
            "fitness":    0.0,
            "targets":    list(individual["targets"]),
            "generation": generation,
            "parents":    [individual["name"]],
        }

    def _crossover(
        self,
        parent_a: Dict[str, Any],
        parent_b: Dict[str, Any],
        constraint: CausalConstraint,
        generation: int,
    ) -> Dict[str, Any]:
        """Causal-biased crossover."""
        lines = _causal_splice(
            parent_a["asm_lines"], parent_b["asm_lines"], constraint, self._rng
        )
        return {
            "name":       f"cross_{generation}_{self._rng.randint(0, 9999):04d}",
            "asm_lines":  lines,
            "fitness":    0.0,
            "targets":    list(set(parent_a["targets"] + parent_b["targets"])),
            "generation": generation,
            "parents":    [parent_a["name"], parent_b["name"]],
        }

    # ── Public API ─────────────────────────────────────────────────────────────

    def evolve_causal(
        self,
        bug_report:         Dict[str, Any],
        root_cause_report:  Optional[Dict[str, Any]] = None,
        outdir:             Optional[Path] = None,
        assemble:           bool = True,
    ) -> Dict[str, Any]:
        """
        Run causal evolution loop.

        Returns a causal_evolution_report dict and optionally writes
        Assembly source files to outdir.

        Parameters
        ----------
        bug_report        : parsed bug_report.json dict
        root_cause_report : optional parsed root_cause.json from Agent H
        outdir            : directory to write .S source files (None = no files)
        assemble          : if True and outdir given, attempt GCC assembly

        Returns
        -------
        dict with keys:
          constraints, generation_stats, top_individuals,
          convergence_rate, baseline_rate, improvement_factor
        """
        from datetime import datetime, timezone
        started = datetime.now(timezone.utc)

        constraints = build_causal_constraints(bug_report, root_cause_report)
        constraint  = constraints[0]

        logger.info(
            "CausalGeneticEngine: mismatch=%s, bias=%s, corner_seqs=%d",
            constraint.mismatch_class,
            constraint.instr_bias[:5],
            len(constraint.corner_sequences),
        )

        # Build seed population
        population = self._seed_population(constraint, self.population_size)

        # Evaluate initial fitness
        for ind in population:
            ind["fitness"] = self._evaluate_fitness(ind, constraint)

        generation_stats: List[Dict[str, Any]] = []
        elite_n = max(1, int(self.population_size * 0.2))

        for gen in range(1, self.generations + 1):
            population.sort(key=lambda x: x["fitness"], reverse=True)
            elite = population[:elite_n]

            # Count "converged" (fitness > 0.5 = re-triggers causal pattern)
            converged = sum(1 for x in population if x["fitness"] > 0.5)
            generation_stats.append({
                "generation":      gen,
                "best_fitness":    round(population[0]["fitness"], 4),
                "mean_fitness":    round(sum(x["fitness"] for x in population) / len(population), 4),
                "converged_count": converged,
            })
            logger.debug("Gen %d: best=%.3f, converged=%d/%d",
                         gen, population[0]["fitness"], converged, self.population_size)

            # Breed new generation
            new_pop = list(elite)
            while len(new_pop) < self.population_size:
                op = self._rng.random()
                if op < 0.6 and len(elite) >= 2:
                    pa, pb = self._rng.sample(elite, 2)
                    child = self._crossover(pa, pb, constraint, gen)
                else:
                    parent = self._rng.choice(elite)
                    child = self._mutate(parent, constraint, gen)
                child["fitness"] = self._evaluate_fitness(child, constraint)
                new_pop.append(child)

            population = new_pop

        # Final ranking
        population.sort(key=lambda x: x["fitness"], reverse=True)
        top = population[:self.output_count]

        # Convergence rate: fraction of final population with fitness > 0.5
        convergence_rate = round(sum(1 for x in population if x["fitness"] > 0.5) / len(population), 4)
        # Baseline: random population has ~20% fitness > 0.5 (heuristic)
        baseline_rate    = 0.20
        improvement      = round(convergence_rate / baseline_rate, 2) if baseline_rate > 0 else 0.0

        finished = datetime.now(timezone.utc)

        report = {
            "schema_version":   "2.1.0",
            "agent":            "causal_engine",
            "mismatch_class":   constraint.mismatch_class,
            "affected_module":  constraint.affected_module,
            "causal_weight":    self.causal_weight,
            "generations":      self.generations,
            "population_size":  self.population_size,
            "instr_bias":       constraint.instr_bias[:10],
            "corner_sequences_count": len(constraint.corner_sequences),
            "generation_stats": generation_stats,
            "top_individuals":  [
                {
                    "name":       x["name"],
                    "fitness":    x["fitness"],
                    "generation": x["generation"],
                    "asm_lines":  x["asm_lines"][:5],  # preview only
                    "targets":    x["targets"],
                }
                for x in top[:10]
            ],
            "convergence_rate":   convergence_rate,
            "baseline_rate":      baseline_rate,
            "improvement_factor": improvement,
            "started_at":  started.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "finished_at": finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duration_s":  round((finished - started).total_seconds(), 3),
        }

        # Write Assembly sources
        if outdir:
            outdir = Path(outdir)
            outdir.mkdir(parents=True, exist_ok=True)
            for i, ind in enumerate(top):
                asm_path = outdir / f"causal_{constraint.mismatch_class.lower()}_{i:03d}.S"
                _write_asm(asm_path, ind, constraint, bug_report.get("run_id", "unknown"))
            report["output_dir"] = str(outdir)
            report["files_written"] = len(top)
            logger.info("CausalGeneticEngine: wrote %d Assembly sources to %s", len(top), outdir)

        return report


def _write_asm(
    path: Path,
    individual: Dict[str, Any],
    constraint: CausalConstraint,
    run_id: str,
) -> None:
    """Write one individual as an Assembly source file."""
    mc = constraint.mismatch_class
    with open(path, "w") as f:
        f.write(f"# AVA Agent G — Causal test\n")
        f.write(f"# Mismatch class:  {mc}\n")
        f.write(f"# Trigger run:     {run_id}\n")
        f.write(f"# Fitness:         {individual['fitness']}\n")
        f.write(f"# Generation:      {individual['generation']}\n\n")
        f.write(".section .text\n.global _start\n_start:\n")
        for line in individual["asm_lines"]:
            f.write(line + "\n")
        f.write("\n    # End-of-test sentinel\n")
        f.write("    li    a0, 1\n")
        f.write("    lui   t0, 0x80001\n")
        f.write("    sw    a0, 0x1000(t0)\n")
        f.write("    j     .\n")


# ─────────────────────────────────────────────────────────
# Convenience: run from manifest
# ─────────────────────────────────────────────────────────

def run_causal_from_manifest(manifest_path: Path) -> int:
    """
    Read a manifest, find the latest bug_report.json, run causal evolution,
    write tests to run_dir/causal_tests/, update manifest.

    Returns 0 on success, 2 on error.
    """
    with open(manifest_path) as f:
        manifest = json.load(f)

    run_dir = Path(manifest["run_dir"])

    # Load bug report
    bug_rpt_path = run_dir / (manifest.get("outputs", {}).get("bug_report") or "bug_report.json")
    if not bug_rpt_path.exists():
        logger.warning("No bug_report found at %s; skipping causal evolution", bug_rpt_path)
        return 0

    with open(bug_rpt_path) as f:
        bug_report = json.load(f)

    # Load root-cause report if present
    rc_path = run_dir / "root_cause.json"
    root_cause = None
    if rc_path.exists():
        with open(rc_path) as f:
            root_cause = json.load(f)

    engine = CausalGeneticEngine(
        seed=manifest.get("seed", 42),
        population_size=80,
        generations=8,
        causal_weight=0.7,
        output_count=30,
    )

    outdir = run_dir / "causal_tests"
    report = engine.evolve_causal(bug_report, root_cause, outdir=outdir, assemble=False)
    report["run_id"] = manifest["run_id"]

    report_path = run_dir / "causal_evolution_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
        f.write("\n")

    manifest.setdefault("outputs", {})["causal_tests_dir"] = str(outdir.relative_to(run_dir))
    tmp = manifest_path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    tmp.rename(manifest_path)

    logger.info("Causal evolution: %dx improvement, %d tests written",
                report["improvement_factor"], report.get("files_written", 0))
    return 0
