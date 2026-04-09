#!/usr/bin/env python3
"""
Agent G — RV32IM Adaptive Test Generator CLI
=============================================
Generates reproducible directed and random RISC-V M-extension tests, and
optionally runs the genetic evolution loop against Agent F/D feedback.

Modes of operation
------------------
1. **Baseline mode** (default): generate 20 directed + 50 random tests.
2. **Manifest mode** (``--manifest``): read Agent F cold paths + Agent D
   hypotheses from the AVA manifest → run GeneticEngine → write evolved ELFs
   to ``rundir/outputs/test_binaries/`` → update manifest phases.

Usage examples
--------------
  # Baseline (20 directed + 50 random):
  python -m rv32im_testgen.generate_tests

  # Custom seed, explicit counts:
  python -m rv32im_testgen.generate_tests --seed 0xDEADBEEF --random 50

  # AVA manifest mode (reads Agent F/D output, evolves population):
  python -m rv32im_testgen.generate_tests --manifest /path/to/manifest.json

  # Extended directed set (~48 tests) with trap injection:
  python -m rv32im_testgen.generate_tests --extended-directed --trap-rate 0.05

  # Source only (no GCC required):
  python -m rv32im_testgen.generate_tests --no-assemble

  # Verify ELFs with Spike:
  python -m rv32im_testgen.generate_tests --verify-spike

Exit codes
----------
  0  — all tests generated successfully
  1  — fatal error (bad arguments, import failure, I/O error)
  2  — one or more Spike verification failures (only with --verify-spike)
  3  — manifest update failed (manifest mode only)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ── Allow running as script or as package module ───────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rv32im_testgen.directed_tests import (
    ALL_DIRECTED_TESTS,
    ALL_DIRECTED_TESTS_EXTENDED,
    DirectedTest,
)
from rv32im_testgen.random_gen import (
    GeneratorConfig,
    DEFAULT_WEIGHTS,
    InstructionMix,
)
from rv32im_testgen.asm_builder import (
    build_directed_asm,
    build_random_asm,
    write_test,
    run_spike,
    TOOLCHAIN_PREFIX,
)
from rv32im_testgen.genetic_engine import GeneticEngine

# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-7s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _derive_seed(base_seed: int, index: int) -> int:
    """Deterministically derive a per-test seed from base_seed and index."""
    h = (index + 1) * 0x9E3779B9
    h = (h ^ (h >> 16)) & 0xFFFF_FFFF
    return (base_seed ^ h) & 0xFFFF_FFFF


def _make_result(
    name: str,
    category: str,
    artifacts: Dict[str, Optional[str]],
    metadata: Dict[str, Any],
    spike_exit: Optional[int] = None,
) -> Dict[str, Any]:
    return {
        "name":       name,
        "category":   category,
        "artifacts":  artifacts,
        "metadata":   metadata,
        "spike_exit": spike_exit,
    }


# ─── Directed test runner ─────────────────────────────────────────────────────

def run_directed(
    tests: List[DirectedTest],
    outdir: Path,
    assemble: bool,
    verify_spike: bool,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    results: List[Dict[str, Any]] = []
    errors:  List[str]            = []
    outdir = outdir / "directed"

    logger.info("Generating %d directed tests → %s", len(tests), outdir)

    for i, test in enumerate(tests):
        idx_str = f"[{i+1:02d}/{len(tests)}]"
        try:
            asm_src = build_directed_asm(test)
            metadata: Dict[str, Any] = {
                "type":           "directed",
                "name":           test.name,
                "description":    test.description,
                "category":       test.category,
                "spec_ref":       test.spec_ref,
                "fitness":        test.fitness,
                "targets":        list(test.targets),
                "expected_regs":  {k: f"0x{v:08X}" for k, v in test.expected_regs.items()},
                "asm_body_lines": len(test.asm_body),
                "index":          i,
                "generated_at":   _utcnow_iso(),
            }
            artifacts = write_test(
                name=test.name,
                asm_src=asm_src,
                metadata=metadata,
                outdir=outdir,
                assemble=assemble,
            )
            spike_exit: Optional[int] = None
            if verify_spike and artifacts["elf"]:
                spike_exit = run_spike(Path(artifacts["elf"]))

            elf_tag = "elf+asm" if artifacts["elf"] else "asm_only"
            logger.info("  %s %-42s [%s]", idx_str, test.name, elf_tag)
            results.append(_make_result(test.name, test.category, artifacts, metadata, spike_exit))

        except Exception as exc:
            msg = f"Directed test {test.name!r} failed: {exc}"
            logger.error("  %s ERROR: %s", idx_str, exc)
            errors.append(msg)

    return results, errors


# ─── Random test runner ───────────────────────────────────────────────────────

def run_random(
    n: int,
    base_seed: int,
    length: int,
    outdir: Path,
    assemble: bool,
    verify_spike: bool,
    trap_rate: float,
    weights: Dict[str, float],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    results: List[Dict[str, Any]] = []
    errors:  List[str]            = []
    outdir = outdir / "random"

    logger.info(
        "Generating %d random tests (base_seed=0x%08X, length=%d) → %s",
        n, base_seed, length, outdir,
    )

    for i in range(n):
        idx_str = f"[{i+1:02d}/{n}]"
        seed = _derive_seed(base_seed, i)
        name = f"rand_s{seed:08X}_l{length}"
        try:
            cfg = GeneratorConfig(
                seed=seed,
                length=length,
                trap_injection_rate=trap_rate,
                weights=weights,
            )
            asm_src, mix = build_random_asm(cfg)
            metadata: Dict[str, Any] = {
                "type":            "random",
                "seed":            f"0x{seed:08X}",
                "base_seed":       f"0x{base_seed:08X}",
                "index":           i,
                "length":          length,
                "trap_rate":       trap_rate,
                "weights":         weights,
                "instruction_mix": mix.to_dict(),
                "generated_at":    _utcnow_iso(),
            }
            artifacts = write_test(
                name=name,
                asm_src=asm_src,
                metadata=metadata,
                outdir=outdir,
                assemble=assemble,
            )
            spike_exit: Optional[int] = None
            if verify_spike and artifacts["elf"]:
                spike_exit = run_spike(Path(artifacts["elf"]))

            m_pct   = mix.to_dict()["alu_m_pct"]
            elf_tag = "elf+asm" if artifacts["elf"] else "asm_only"
            logger.info(
                "  %s seed=0x%08X  M-ext=%d/%d (%.0f%%)  [%s]",
                idx_str, seed, mix.alu_m, mix.total, m_pct, elf_tag,
            )
            results.append(_make_result(name, "random", artifacts, metadata, spike_exit))

        except Exception as exc:
            msg = f"Random test index {i} (seed=0x{seed:08X}) failed: {exc}"
            logger.error("  %s ERROR: %s", idx_str, exc)
            errors.append(msg)

    return results, errors


# ─── Manifest mode (AVA feedback loop) ───────────────────────────────────────

def generate_from_manifest(manifest_path: Path) -> int:
    """
    AVA contract: read Agent F cold paths + Agent D hypotheses from
    *manifest_path*, evolve a population of tests, write ELFs to
    ``rundir/outputs/test_binaries/``, then update the manifest.

    Expected manifest schema (subset)::

        {
            "rundir": "/path/to/run/directory",
            "phases": {
                "coverage": {"status": "completed"},
                "bugfinder": {"status": "completed"}
            }
        }

    Output files at ``rundir/outputs/test_binaries/``::

        evo_cx_00001.S
        evo_cx_00001.meta.json
        evo_cx_00001.elf          ← if toolchain available
        ...
        evolution_summary.json

    Manifest ``phases.generator`` is updated on success::

        "generator": {
            "status": "completed",
            "evolved_population": 50,
            "constraints_targeted": 20,
            "avg_fitness": 3.24,
            "seed_tests": 70,
            "output_dir": "..."
        }

    Returns
    -------
    int
        Exit code: 0 = success, 3 = manifest/IO error.
    """
    logger.info("Manifest mode: %s", manifest_path)

    # ── Load manifest ─────────────────────────────────────────────────────────
    try:
        manifest_raw = manifest_path.read_text(encoding="utf-8")
        manifest: Dict[str, Any] = json.loads(manifest_raw)
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("Cannot read manifest %s: %s", manifest_path, exc)
        return 3

    rundir_raw = manifest.get("rundir")
    if not rundir_raw:
        logger.error("Manifest missing 'rundir' key")
        return 3
    rundir = Path(rundir_raw)

    # ── Load Agent F coverage summary ────────────────────────────────────────
    cov_path = rundir / "coveragesummary.json"
    cold_paths: List[Dict] = []
    if cov_path.exists():
        try:
            cov_data    = json.loads(cov_path.read_text(encoding="utf-8"))
            cold_paths  = cov_data.get("cold_paths", [])
            logger.info("  Agent F cold paths: %d", len(cold_paths))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Cannot read %s: %s — proceeding without Agent F data", cov_path, exc)
    else:
        logger.warning("  coveragesummary.json not found at %s", cov_path)

    # ── Load Agent D bug hypotheses ───────────────────────────────────────────
    bug_path = rundir / "bugreport.json"
    hypotheses: List[Dict] = []
    if bug_path.exists():
        try:
            bug_data   = json.loads(bug_path.read_text(encoding="utf-8"))
            hypotheses = bug_data.get("hypotheses", [])
            logger.info("  Agent D hypotheses:  %d", len(hypotheses))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Cannot read %s: %s — proceeding without Agent D data", bug_path, exc)
    else:
        logger.warning("  bugreport.json not found at %s", bug_path)

    # ── Unify constraints ─────────────────────────────────────────────────────
    constraints = cold_paths + hypotheses
    logger.info("  Total constraints: %d", len(constraints))

    # ── Prepare output directory ──────────────────────────────────────────────
    binaries_dir = rundir / "outputs" / "test_binaries"
    binaries_dir.mkdir(parents=True, exist_ok=True)

    # ── Run genetic engine ────────────────────────────────────────────────────
    engine = GeneticEngine(
        seed=manifest.get("seed", 42),
        population_size=100,
        generations=10,
        elite_fraction=0.20,
        mutation_rate=0.15,
        crossover_rate=0.60,
        output_count=50,
    )

    elf_assemble = bool(TOOLCHAIN_PREFIX)
    results = engine.evolve(
        constraints=constraints,
        outdir=binaries_dir,
        assemble=elf_assemble,
    )

    # ── Write evolution summary ───────────────────────────────────────────────
    avg_fitness = (
        sum(r["fitness"] for r in results) / max(len(results), 1)
    )
    summary_path = binaries_dir / "evolution_summary.json"
    summary: Dict[str, Any] = {
        "generated_at":        _utcnow_iso(),
        "constraints_used":    len(constraints),
        "cold_paths_used":     len(cold_paths),
        "hypotheses_used":     len(hypotheses),
        "population_size":     engine.population_size,
        "generations":         engine.generations,
        "evolved_population":  len(results),
        "avg_fitness":         round(avg_fitness, 3),
        "toolchain_found":     bool(TOOLCHAIN_PREFIX),
        "generation_stats":    engine.generation_stats,
        "test_binaries": [
            {
                "path":       r["artifacts"].get("elf") or r["artifacts"].get("S"),
                "fitness":    round(r["fitness"], 3),
                "targets":    r["targets"],
                "generation": r["generation"],
            }
            for r in results
        ],
    }
    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    logger.info("  Evolution summary: %s", summary_path)

    # ── Update manifest ───────────────────────────────────────────────────────
    phases = manifest.setdefault("phases", {})
    phases["generator"] = {
        "status":               "completed",
        "evolved_population":   len(results),
        "constraints_targeted": len(constraints),
        "avg_fitness":          round(avg_fitness, 3),
        "seed_tests":           70,   # 20 directed + 50 random baseline
        "output_dir":           str(binaries_dir),
        "completed_at":         _utcnow_iso(),
    }

    try:
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )
        logger.info("  Manifest updated: %s", manifest_path)
    except OSError as exc:
        logger.error("Failed to update manifest: %s", exc)
        return 3

    # ── Summary banner ────────────────────────────────────────────────────────
    SEP = "=" * 62
    print(f"\n{SEP}")
    print("  AGENT G — MANIFEST MODE COMPLETE")
    print(SEP)
    print(f"  Constraints read   : {len(constraints)}")
    print(f"  Evolved tests      : {len(results)}")
    print(f"  Avg fitness        : {avg_fitness:.3f}")
    print(f"  ELFs produced      : {sum(1 for r in results if r['artifacts'].get('elf'))}")
    print(f"  Output dir         : {binaries_dir}")
    print(f"  Evolution summary  : {summary_path}")
    print(SEP)

    return 0


# ─── Manifest writer ──────────────────────────────────────────────────────────

def write_manifest(
    outdir: Path,
    directed_results: List[Dict[str, Any]],
    random_results: List[Dict[str, Any]],
    args: argparse.Namespace,
    wall_seconds: float,
    all_errors: List[str],
) -> Path:
    elf_produced = bool(TOOLCHAIN_PREFIX) and args.assemble
    manifest: Dict[str, Any] = {
        "agent":          "G — RV32IM Test Generator",
        "generated_at":   _utcnow_iso(),
        "base_seed":      f"0x{args.seed:08X}",
        "toolchain":      TOOLCHAIN_PREFIX or "none",
        "elf_produced":   elf_produced,
        "directed_count": len(directed_results),
        "random_count":   len(random_results),
        "random_length":  args.length,
        "trap_rate":      args.trap_rate,
        "wall_seconds":   round(wall_seconds, 2),
        "errors":         all_errors,
        "generator": {
            "evolved_population":   0,
            "constraints_targeted": 0,
            "avg_fitness":          0.0,
            "seed_tests":           len(directed_results) + len(random_results),
        },
        "directed": [
            {
                "name":     r["name"],
                "category": r["category"],
                "fitness":  r["metadata"].get("fitness", 0.0),
                "targets":  r["metadata"].get("targets", []),
                "asm":      r["artifacts"]["S"],
                "elf":      r["artifacts"]["elf"],
                "meta":     r["artifacts"]["meta"],
                "spike":    r["spike_exit"],
            }
            for r in directed_results
        ],
        "random": [
            {
                "name":  r["name"],
                "seed":  r["metadata"]["seed"],
                "asm":   r["artifacts"]["S"],
                "elf":   r["artifacts"]["elf"],
                "meta":  r["artifacts"]["meta"],
                "mix":   r["metadata"]["instruction_mix"],
                "spike": r["spike_exit"],
            }
            for r in random_results
        ],
    }
    mp = outdir / "manifest.json"
    mp.write_text(
        json.dumps(manifest, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    return mp


# ─── Summary printer ──────────────────────────────────────────────────────────

def _print_summary(
    directed_results: List[Dict[str, Any]],
    random_results: List[Dict[str, Any]],
    all_errors: List[str],
    manifest_path: Path,
    wall_seconds: float,
) -> None:
    n_elf_d = sum(1 for r in directed_results if r["artifacts"]["elf"])
    n_elf_r = sum(1 for r in random_results   if r["artifacts"]["elf"])

    spike_d      = [r for r in directed_results if r["spike_exit"] is not None]
    spike_r      = [r for r in random_results   if r["spike_exit"] is not None]
    spike_fail_d = sum(1 for r in spike_d if r["spike_exit"] != 0)
    spike_fail_r = sum(1 for r in spike_r if r["spike_exit"] != 0)

    m_counts = [
        r["metadata"]["instruction_mix"].get("alu_m", 0)
        for r in random_results
        if "instruction_mix" in r["metadata"]
    ]
    avg_m  = sum(m_counts) / len(m_counts) if m_counts else 0.0
    length = random_results[0]["metadata"]["length"] if random_results else 0

    SEP = "=" * 62
    print(f"\n{SEP}")
    print("  AGENT G — GENERATION COMPLETE")
    print(SEP)
    print(f"  Wall time      : {wall_seconds:.2f}s")
    print(f"  Directed tests : {len(directed_results):3d}  ({n_elf_d} ELFs)")
    print(f"  Random tests   : {len(random_results):3d}  ({n_elf_r} ELFs)")
    if length:
        print(f"  Avg M-ext      : {avg_m:.1f}/{length} ({100*avg_m/max(length,1):.1f}%)")
    if spike_d or spike_r:
        print(f"  Spike directed : {len(spike_d)-spike_fail_d}/{len(spike_d)} PASS"
              + (f"  ({spike_fail_d} FAIL)" if spike_fail_d else ""))
        print(f"  Spike random   : {len(spike_r)-spike_fail_r}/{len(spike_r)} PASS"
              + (f"  ({spike_fail_r} FAIL)" if spike_fail_r else ""))
    print(f"  Manifest       : {manifest_path}")
    if all_errors:
        print(f"\n  ⚠  {len(all_errors)} non-fatal error(s):")
        for e in all_errors:
            print(f"     • {e}")
    if not TOOLCHAIN_PREFIX:
        print("\n  ⚠  No RISC-V toolchain found.  Assembly sources (.S) only.")
        print("     Install riscv32-unknown-elf-gcc and re-run to produce ELFs.")
    print(SEP)


# ─── CLI parser ───────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="generate_tests",
        description=__doc__.strip(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--manifest", type=Path, metavar="FILE",
        help="AVA manifest path.  Enables manifest mode: reads Agent F/D feedback "
             "and evolves a new test population.  All other flags are ignored.",
    )
    p.add_argument(
        "--seed", type=lambda x: int(x, 0), default=0, metavar="SEED",
        help="Base seed for random tests (hex or decimal). Default: 0",
    )
    p.add_argument(
        "--random", type=int, default=50, metavar="N",
        help="Number of random tests to generate. Default: 50",
    )
    p.add_argument(
        "--directed", type=int, default=20, metavar="N",
        help="Number of directed tests to select from the pool. Default: 20",
    )
    p.add_argument(
        "--length", type=int, default=200, metavar="INSTRS",
        help="Instruction groups per random test. Default: 200",
    )
    p.add_argument(
        "--outdir", type=Path, default=Path("tests_out"), metavar="DIR",
        help="Output directory. Default: tests_out/",
    )
    p.add_argument(
        "--trap-rate", type=float, default=0.0, metavar="RATE",
        help="Fraction of random instruction slots replaced by trap-inducing "
             "instructions. Default: 0.0",
    )
    p.add_argument(
        "--m-weight", type=float, default=0.30, metavar="W",
        help="Relative weight of M-extension instructions in random tests. Default: 0.30",
    )
    p.add_argument(
        "--extended-directed", action="store_true",
        help="Use the full extended directed test pool (~48 tests) instead of core 20.",
    )
    p.add_argument(
        "--no-assemble", dest="assemble", action="store_false",
        help="Skip ELF assembly; produce .S source files only.",
    )
    p.add_argument(
        "--verify-spike", action="store_true",
        help="Run each assembled ELF through Spike and record PASS/FAIL.",
    )
    p.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable DEBUG-level logging.",
    )
    return p


# ─── Weight builder ───────────────────────────────────────────────────────────

def _build_weights(m_weight: float) -> Dict[str, float]:
    if not (0.0 < m_weight < 1.0):
        raise ValueError(f"--m-weight must be in (0, 1), got {m_weight}")
    base = dict(DEFAULT_WEIGHTS)
    other_sum = sum(v for k, v in base.items() if k != "alu_m")
    if other_sum <= 0.0:
        raise ValueError("DEFAULT_WEIGHTS has no non-M-ext entries")
    scale = (1.0 - m_weight) / other_sum
    return {
        k: (m_weight if k == "alu_m" else v * scale)
        for k, v in base.items()
    }


# ─── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    args = _build_parser().parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # ── Manifest mode: delegate entirely ────────────────────────────────────
    if args.manifest:
        if not args.manifest.exists():
            logger.error("Manifest file not found: %s", args.manifest)
            return 1
        return generate_from_manifest(args.manifest)

    # ── Baseline mode ─────────────────────────────────────────────────────────
    if args.verify_spike:
        args.assemble = True

    pool      = ALL_DIRECTED_TESTS_EXTENDED if args.extended_directed else ALL_DIRECTED_TESTS
    n_dir     = min(args.directed, len(pool))
    if n_dir < args.directed:
        logger.warning(
            "Pool has only %d tests; requested %d → using %d.",
            len(pool), args.directed, n_dir,
        )
    directed_subset = pool[:n_dir]

    try:
        weights = _build_weights(args.m_weight)
    except ValueError as exc:
        logger.error("Invalid --m-weight: %s", exc)
        return 1

    if args.random < 0:
        logger.error("--random must be ≥ 0"); return 1
    if args.length < 1:
        logger.error("--length must be ≥ 1"); return 1
    if not (0.0 <= args.trap_rate <= 1.0):
        logger.error("--trap-rate must be in [0.0, 1.0]"); return 1

    SEP = "=" * 62
    logger.info(SEP)
    logger.info("  Agent G — RV32IM Test Generator")
    logger.info(SEP)
    logger.info("  Base seed  : 0x%08X", args.seed)
    logger.info("  Directed   : %d  (pool: %d)", n_dir, len(pool))
    logger.info("  Random     : %d  (length=%d)", args.random, args.length)
    logger.info("  Trap rate  : %.1f%%", args.trap_rate * 100)
    logger.info("  M-ext wt   : %.0f%%", args.m_weight * 100)
    logger.info("  Toolchain  : %s", TOOLCHAIN_PREFIX or "NOT FOUND (source only)")
    logger.info("  Verify     : %s", "Spike" if args.verify_spike else "disabled")
    logger.info("  Output     : %s", args.outdir)
    logger.info(SEP)

    t0         = time.monotonic()
    all_errors: List[str] = []

    directed_results, d_errors = run_directed(
        tests=directed_subset, outdir=args.outdir,
        assemble=args.assemble, verify_spike=args.verify_spike,
    )
    all_errors.extend(d_errors)

    random_results, r_errors = run_random(
        n=args.random, base_seed=args.seed, length=args.length,
        outdir=args.outdir, assemble=args.assemble,
        verify_spike=args.verify_spike, trap_rate=args.trap_rate,
        weights=weights,
    )
    all_errors.extend(r_errors)

    wall_seconds = time.monotonic() - t0

    try:
        manifest_path = write_manifest(
            outdir=args.outdir,
            directed_results=directed_results,
            random_results=random_results,
            args=args,
            wall_seconds=wall_seconds,
            all_errors=all_errors,
        )
    except Exception as exc:
        logger.error("Failed to write manifest: %s", exc)
        return 1

    _print_summary(
        directed_results, random_results, all_errors, manifest_path, wall_seconds
    )

    spike_fails = sum(
        1 for r in directed_results + random_results
        if r["spike_exit"] is not None and r["spike_exit"] != 0
    )
    if spike_fails and args.verify_spike:
        logger.error("%d Spike verification failure(s)", spike_fails)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
