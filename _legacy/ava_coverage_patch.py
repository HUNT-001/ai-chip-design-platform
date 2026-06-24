"""
ava_coverage_patch.py — Agent F: AVA Coverage Integration Patch v2.0
=====================================================================
Apply targeted edits to ava.py to replace all placeholder coverage code
with real Verilator extraction.

Features
--------
  * Idempotency — safe to run multiple times (detects already-applied hunks)
  * Automatic timestamped backup before any modification
  * Dry-run mode (--dry-run / --status)
  * Syntax validation of patched file via py_compile
  * Clear per-hunk pass/skip/fail report

Usage
-----
    python ava_coverage_patch.py ava.py            # apply
    python ava_coverage_patch.py ava.py --dry-run  # preview only
    python ava_coverage_patch.py ava.py --status   # show applied state

Exit codes:  0 = all applied/already-applied,  1 = failure,  2 = usage error
"""
from __future__ import annotations

import argparse
import py_compile
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List


@dataclass
class Hunk:
    hunk_id:     str
    description: str
    search:      str
    replace:     str
    applied:     bool = False
    skipped:     bool = False
    error:       str  = ""


# ── Hunk definitions ──────────────────────────────────────────────────────────

HUNKS: List[Hunk] = [

Hunk(
    "H1",
    "Add coverage_pipeline import with graceful fallback",
    search='''\
# Optional imports with fallbacks
try:
    import ollama
    OLLAMA_AVAILABLE = True
except ImportError:
    OLLAMA_AVAILABLE = False
    logging.warning("Ollama not available - LLM features will be disabled")''',
    replace='''\
# Optional imports with fallbacks
try:
    import ollama
    OLLAMA_AVAILABLE = True
except ImportError:
    OLLAMA_AVAILABLE = False
    logging.warning("Ollama not available - LLM features will be disabled")

# Agent F: real Verilator coverage backend (coverage_pipeline.py)
try:
    from coverage_pipeline import (
        VerilatorCoverageBackend,
        CoverageDatabase,
        ParseError,
        parse_spike_commit_log,
        parse_dut_commit_log,
        count_cycles_instrets,
        _parse_commit_log,
        _count_cycles_instrs,
    )
    COVERAGE_PIPELINE_AVAILABLE = True
except ImportError:
    COVERAGE_PIPELINE_AVAILABLE = False
    logging.warning(
        "coverage_pipeline.py not found — coverage will be placeholder values. "
        "Place coverage_pipeline.py in the same directory as ava.py."
    )''',
),

Hunk(
    "H2",
    "SpikeISS.__init__: add run_dir / spike_binary / VerilatorCoverageBackend",
    search='''\
class SpikeISS:
    """Spike Instruction Set Simulator integration"""
    
    def __init__(self, timeout: int = 3600):
        self.timeout = timeout
        self.simulation_count = 0
        logger.info("SpikeISS initialized")''',
    replace='''\
class SpikeISS:
    """Spike Instruction Set Simulator integration"""

    def __init__(
        self,
        timeout: int = 3600,
        run_dir: str = "sim_runs/default",
        spike_binary: str = "spike",
        isa: str = "rv32im",
    ):
        self.timeout       = timeout
        self.isa           = isa
        self.spike_binary  = spike_binary
        self.simulation_count = 0
        self._run_dir = Path(run_dir)
        self._run_dir.mkdir(parents=True, exist_ok=True)

        # Agent F: real Verilator coverage backend
        if COVERAGE_PIPELINE_AVAILABLE:
            self._cov_backend: Optional[VerilatorCoverageBackend] = (
                VerilatorCoverageBackend(
                    run_dir=self._run_dir,
                    dat_filename="coverage.dat",
                    fallback_on_missing=True,
                )
            )
        else:
            self._cov_backend = None

        logger.info(
            "SpikeISS initialized | run_dir=%s | coverage_pipeline=%s",
            self._run_dir, COVERAGE_PIPELINE_AVAILABLE,
        )''',
),

Hunk(
    "H3",
    "SpikeISS._simulate_rtl: real Verilator subprocess with asyncio (stub-safe)",
    search='''\
    async def _simulate_rtl(
        self,
        tb_suite: Dict[str, Any],
        semantic_map: SemanticMap
    ) -> Dict[str, Any]:
        """Simulate RTL with testbench"""
        await asyncio.sleep(0.1)  # Placeholder for actual simulation
        
        return {
            "cycles": 10000,
            "instructions": 8500,
            "coverage_data": {
                "lines_hit": 9620,
                "total_lines": 10000,
                "branches_hit": 1876,
                "total_branches": 2000,
                "toggles": 91200
            },
            "performance": {
                "ipc": 1.82,
                "branch_predictions": 1850,
                "branch_correct": 1708
            },
            "state_snapshots": []
        }''',
    replace='''\
    async def _simulate_rtl(
        self,
        tb_suite: Dict[str, Any],
        semantic_map: SemanticMap,
    ) -> Dict[str, Any]:
        """
        Real RTL simulation via Verilator-compiled DUT binary.

        tb_suite expected keys:
          sim_binary  : path to Verilator simulation executable
          elf_path    : ELF test binary
          seed        : integer RNG seed
          extra_args  : list of additional +arg strings (optional)

        Returns a clearly-labelled stub dict when binary is absent.
        """
        run_dir  = self._run_dir
        run_dir.mkdir(parents=True, exist_ok=True)

        sim_bin  = tb_suite.get(
            "sim_binary",
            str(run_dir / "obj_dir" / f"V{semantic_map.dut_module}"),
        )
        elf_path = str(tb_suite.get("elf_path", run_dir / "test.elf"))
        seed     = int(tb_suite.get("seed", 1))
        cov_dat  = str(run_dir / "coverage.dat")
        extra    = list(tb_suite.get("extra_args", []))

        if self._cov_backend is not None:
            self._cov_backend.update_run_dir(run_dir)

        if not Path(sim_bin).exists():
            logger.warning(
                "RTL binary not found: %s — returning stub (build DUT first).", sim_bin
            )
            return {
                "cycles": 0, "instructions": 0, "coverage_data": {},
                "performance": {"ipc": 0.0, "branch_predictions": 0, "branch_correct": 0},
                "commit_log": [], "state_snapshots": [], "seed": seed, "stub": True,
            }

        cmd = [sim_bin, f"+seed={seed}", f"+elf={elf_path}",
               f"+coverage_file={cov_dat}"] + extra
        logger.info("RTL sim: %s", " ".join(cmd))

        try:
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(run_dir),
                ),
                timeout=self.timeout,
            )
            stdout_b, stderr_b = await proc.communicate()
        except asyncio.TimeoutError:
            raise SimulationError(f"RTL simulation timed out after {self.timeout}s")
        except Exception as exc:
            raise SimulationError(f"Failed to launch RTL sim: {exc}") from exc

        stdout = stdout_b.decode(errors="replace")
        stderr = stderr_b.decode(errors="replace")

        if proc.returncode not in (0, None):
            logger.error("RTL sim rc=%d\\nSTDERR: %s", proc.returncode, stderr[:2000])

        if COVERAGE_PIPELINE_AVAILABLE:
            commit_log = parse_dut_commit_log(stdout)
            cycles, instrets = count_cycles_instrets(stdout)
        else:
            commit_log = []
            cycles, instrets = 0, 0

        return {
            "cycles":       cycles,
            "instructions": instrets or len(commit_log),
            "coverage_data": {},
            "performance": {
                "ipc": round(instrets / max(cycles, 1), 4),
                "branch_predictions": 0,
                "branch_correct": 0,
            },
            "commit_log":      commit_log,
            "state_snapshots": commit_log,
            "seed":            seed,
            "returncode":      proc.returncode,
        }''',
),

Hunk(
    "H4",
    "SpikeISS._simulate_iss: real Spike subprocess (stub-safe)",
    search='''\
    async def _simulate_iss(
        self,
        semantic_map: SemanticMap,
        stimulus: Optional[List[Dict]]
    ) -> Dict[str, Any]:
        """Simulate using ISS golden model"""
        await asyncio.sleep(0.05)
        
        return {
            "instructions": 8500,
            "state_snapshots": [],
            "exceptions": []
        }''',
    replace='''\
    async def _simulate_iss(
        self,
        semantic_map: SemanticMap,
        stimulus: Optional[List[Dict]],
        tb_suite: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Run Spike ISS as golden reference model.
        Requires Spike on PATH with --log-commits support.
        Returns a stub if Spike is not found or ELF is missing.
        """
        import shutil as _shutil

        tb_suite   = tb_suite or {}
        spike_bin  = getattr(self, "spike_binary", "spike")
        elf_path   = str(tb_suite.get("elf_path", self._run_dir / "test.elf"))

        if not _shutil.which(spike_bin) and not Path(spike_bin).exists():
            logger.warning(
                "Spike binary '%s' not found — returning ISS stub. "
                "Install Spike: https://github.com/riscv-software-src/riscv-isa-sim",
                spike_bin,
            )
            return {"instructions": 0, "commit_log": [],
                    "state_snapshots": [], "exceptions": [], "stub": True}

        if not Path(elf_path).exists():
            logger.warning("ELF not found: %s — ISS run skipped", elf_path)
            return {"instructions": 0, "commit_log": [],
                    "state_snapshots": [], "exceptions": [], "stub": True}

        isa = getattr(self, "isa", "rv32im")
        cmd = [spike_bin, f"--isa={isa}", "--log-commits", "-l", elf_path]
        logger.info("Spike ISS: %s", " ".join(cmd))

        try:
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(self._run_dir),
                ),
                timeout=self.timeout,
            )
            stdout_b, stderr_b = await proc.communicate()
        except asyncio.TimeoutError:
            raise SimulationError(f"Spike ISS timed out after {self.timeout}s")
        except Exception as exc:
            raise SimulationError(f"Failed to launch Spike: {exc}") from exc

        text = stderr_b.decode(errors="replace") + "\\n" + stdout_b.decode(errors="replace")
        commit_log = (
            parse_spike_commit_log(text)
            if COVERAGE_PIPELINE_AVAILABLE
            else []
        )
        return {
            "instructions":    len(commit_log),
            "commit_log":      commit_log,
            "state_snapshots": commit_log,
            "exceptions":      [],
        }''',
),

Hunk(
    "H5",
    "SpikeISS._compare_results: real CommitLog differential (PC + register)",
    search='''\
    async def _compare_results(
        self,
        rtl_results: Dict[str, Any],
        iss_results: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Compare RTL vs ISS results"""
        mismatches = []
        
        # Check instruction count match
        if rtl_results.get("instructions") != iss_results.get("instructions"):
            mismatches.append({
                "type": "instruction_count_mismatch",
                "severity": "high",
                "rtl_count": rtl_results.get("instructions"),
                "iss_count": iss_results.get("instructions")
            })
        
        return {
            "mismatches": mismatches,
            "match_percentage": 100.0 - (len(mismatches) * 5.0)
        }''',
    replace='''\
    async def _compare_results(
        self,
        rtl_results: Dict[str, Any],
        iss_results: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Differential commit-log comparison: PC-by-PC, register-by-register.
        Falls back to instruction-count check when logs are stubs/empty.
        """
        rtl_log  = rtl_results.get("commit_log", [])
        iss_log  = iss_results.get("commit_log", [])
        bugs:     List[Dict[str, Any]] = []
        warnings: List[str]            = []

        if rtl_results.get("stub") or not rtl_log:
            warnings.append("RTL commit log empty — differential comparison skipped.")
        if iss_results.get("stub") or not iss_log:
            warnings.append("ISS commit log empty — differential comparison skipped.")

        if rtl_log and iss_log:
            MAX_MISMATCHES = 50
            rtl_i = iss_i = 0
            while rtl_i < len(rtl_log) and iss_i < len(iss_log) and len(bugs) < MAX_MISMATCHES:
                re_ = rtl_log[rtl_i]
                ie_ = iss_log[iss_i]
                # PC check
                if re_.get("pc") != ie_.get("pc"):
                    ctx = rtl_log[max(0, rtl_i-5):rtl_i+1]
                    bugs.append({
                        "type": "pc_mismatch", "severity": "critical",
                        "pc": re_.get("pc"), "instr": re_.get("instr"),
                        "rtl_value": re_.get("pc"), "iss_value": ie_.get("pc"),
                        "description": (
                            f"PC divergence at RTL[{rtl_i}]: "
                            f"RTL={re_.get('pc')} ISS={ie_.get('pc')}"
                        ),
                        "repro": ctx,
                    })
                    rtl_i += 1; iss_i += 1
                    continue
                # Register check (skip x0)
                rd = re_.get("rd", "")
                rv = re_.get("rd_val", "")
                iv = ie_.get("rd_val", "")
                if rd and rd != "x0" and rv and iv and rv != iv:
                    bugs.append({
                        "type": "register_mismatch", "severity": "high",
                        "pc": re_.get("pc"), "instr": re_.get("instr"),
                        "register": rd, "rtl_value": rv, "iss_value": iv,
                        "description": (
                            f"Reg {rd} mismatch at {re_.get('pc')}: "
                            f"RTL={rv} ISS={iv}"
                        ),
                    })
                rtl_i += 1; iss_i += 1

        # Instruction count sanity
        n_rtl = rtl_results.get("instructions", 0)
        n_iss = iss_results.get("instructions", 0)
        if n_rtl and n_iss and abs(n_rtl - n_iss) > max(n_rtl, n_iss) * 0.02:
            warnings.append(f"Instruction count delta: RTL={n_rtl} ISS={n_iss}")

        total  = max(len(rtl_log), len(iss_log), 1)
        return {
            "bugs":      bugs,
            "mismatches":bugs,   # backward-compat key
            "warnings":  warnings,
            "match_pct": round(100.0 * max(0, total - len(bugs)) / total, 2),
        }''',
),

Hunk(
    "H6",
    "SpikeISS._calculate_coverage: delegate to VerilatorCoverageBackend",
    search='''\
    def _calculate_coverage(self, rtl_results: Dict[str, Any]) -> Dict[str, float]:
        """Calculate coverage metrics"""
        cov_data = rtl_results.get("coverage_data", {})
        
        line_cov = (cov_data.get("lines_hit", 0) / max(cov_data.get("total_lines", 1), 1)) * 100
        branch_cov = (cov_data.get("branches_hit", 0) / max(cov_data.get("total_branches", 1), 1)) * 100
        toggle_cov = 91.2  # Placeholder
        
        return {
            "line": min(line_cov, 100.0),
            "branch": min(branch_cov, 100.0),
            "toggle": toggle_cov,
            "functional": min((line_cov + branch_cov) / 2, 100.0)
        }''',
    replace='''\
    def _calculate_coverage(self, rtl_results: Dict[str, Any]) -> Dict[str, float]:
        """
        Real coverage extraction via VerilatorCoverageBackend (Agent F).

        Priority:
          1. Verilator coverage.dat in self._run_dir  (real hardware data)
          2. coverage_data dict in rtl_results         (legacy / manual stub)
          3. Zeros + explicit ERROR log                (last resort)

        Saves coverage_report.json to self._run_dir after each run.
        """
        if self._cov_backend is not None:
            return self._cov_backend.get_coverage(rtl_results)

        # No coverage_pipeline — legacy fallback
        cov_data = rtl_results.get("coverage_data") or {}
        lines_hit      = int(cov_data.get("lines_hit", 0))
        total_lines    = max(int(cov_data.get("total_lines", 1)), 1)
        branches_hit   = int(cov_data.get("branches_hit", 0))
        total_branches = max(int(cov_data.get("total_branches", 1)), 1)
        line_pct   = round(100.0 * lines_hit / total_lines, 2)
        branch_pct = round(100.0 * branches_hit / total_branches, 2)
        logger.warning(
            "coverage_pipeline unavailable — using legacy fallback "
            "(toggle=0, expression=0)"
        )
        return {
            "line":       min(line_pct, 100.0),
            "branch":     min(branch_pct, 100.0),
            "toggle":     0.0,
            "expression": 0.0,
            "functional": min((line_pct + branch_pct) / 2, 100.0),
        }''',
),

Hunk(
    "H7",
    "CoverageDirector.adapt_cold_paths: accept cold_path_detail + UCB1 hint",
    search='''\
    def adapt_cold_paths(
        self,
        current_coverage: Dict[str, float],
        semantic_map: Optional[SemanticMap] = None
    ) -> List[Dict[str, Any]]:
        """Generate adaptive stimulus for uncovered paths"""
        try:
            logger.info("Generating adaptive stimulus for cold paths...")
            
            # Identify coverage gaps
            gaps = self._identify_gaps(current_coverage)
            
            if not gaps:
                logger.info("Target coverage achieved - no cold paths")
                return []
            
            # Generate targeted stimulus
            adaptive_stimulus = []
            for gap in gaps[:self.max_iterations]:
                stimulus = self._generate_gap_stimulus(gap, semantic_map)
                if stimulus:
                    adaptive_stimulus.append(stimulus)
            
            logger.info(f"Generated {len(adaptive_stimulus)} adaptive test cases")
            
            # Record coverage history
            self.coverage_history.append(current_coverage.copy())
            
            return adaptive_stimulus
            
        except Exception as e:
            logger.error(f"Adaptive stimulus generation failed: {e}")
            return []''',
    replace='''\
    def adapt_cold_paths(
        self,
        current_coverage: Dict[str, float],
        semantic_map: Optional[SemanticMap] = None,
        cold_path_detail: Optional[Dict[str, List]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Generate adaptive stimulus for uncovered paths.

        Args
        ----
        current_coverage  : dict with keys line/branch/toggle/functional (real %)
        semantic_map      : optional SemanticMap for module-aware targeting
        cold_path_detail  : optional dict from VerilatorCoverageBackend.cold_paths
                            {lines:[{file,line,hier,comment},...],
                             branches:[...], toggles:[...]}
                            Each stimulus entry will include up to 20 specific
                            uncovered locations for the test generator.
        """
        try:
            logger.info("Generating adaptive stimulus for cold paths...")
            self.coverage_history.append(current_coverage.copy())

            gaps = self._identify_gaps(current_coverage)
            if not gaps:
                logger.info("All coverage targets met — no cold paths")
                return []

            _key_map = {"line": "lines", "branch": "branches",
                        "toggle": "toggles", "expression": "expressions"}
            adaptive_stimulus = []

            for gap in gaps[:self.max_iterations]:
                stimulus = self._generate_gap_stimulus(gap, semantic_map)
                if stimulus is None:
                    continue

                # Enrich with exact cold locations
                if cold_path_detail:
                    ckey     = _key_map.get(gap["metric"], "")
                    targets  = cold_path_detail.get(ckey, [])[:20]
                    stimulus["specific_targets"] = targets
                    if targets:
                        stimulus["description"] += (
                            f" — {len(targets)} cold points attached"
                        )
                else:
                    stimulus["specific_targets"] = []

                adaptive_stimulus.append(stimulus)

            logger.info("Generated %d adaptive test cases", len(adaptive_stimulus))
            return adaptive_stimulus

        except Exception as exc:
            logger.error("Adaptive stimulus generation failed: %s", exc)
            return []''',
),

Hunk(
    "H8",
    "AVA._tandem_simulation: pass cold_path_detail from backend to CoverageDirector",
    search='''\
            # 5. COVERAGE DIRECTOR: RL for cold blocks
            logger.info("\\n[5/5] Coverage Adaptation - RL-Directed Stimulus...")
            current_phase = VerificationPhase.COVERAGE_ADAPTATION
            adaptive_stimulus = self.coverage_director.adapt_cold_paths(
                results.coverage,
                semantic_map
            )''',
    replace='''\
            # 5. COVERAGE DIRECTOR: RL for cold blocks
            logger.info("\\n[5/5] Coverage Adaptation - RL-Directed Stimulus...")
            current_phase = VerificationPhase.COVERAGE_ADAPTATION

            # Agent F: pull exact cold-path locations from the coverage backend
            cold_detail: Optional[Dict[str, List]] = None
            if (COVERAGE_PIPELINE_AVAILABLE
                    and hasattr(self.spike_iss, "_cov_backend")
                    and self.spike_iss._cov_backend is not None):
                cold_detail = self.spike_iss._cov_backend.cold_paths

            adaptive_stimulus = self.coverage_director.adapt_cold_paths(
                results.coverage,
                semantic_map,
                cold_path_detail=cold_detail,
            )''',
),

]  # end HUNKS


# ═══════════════════════════════════════════════════════════════════════════════
# Patch engine
# ═══════════════════════════════════════════════════════════════════════════════

def _apply_hunks(src: str, hunks: List[Hunk], dry_run: bool = False) -> Tuple[str, List[Hunk]]:
    """Apply all hunks to src. Returns (patched_src, updated_hunks)."""
    result = src
    for hunk in hunks:
        if hunk.search not in src:
            # Check if it's already been applied (idempotent)
            if hunk.replace in result:
                hunk.skipped = True
            else:
                hunk.error = "search text not found in source"
            continue
        if not dry_run:
            result = result.replace(hunk.search, hunk.replace, 1)
        hunk.applied = True
    return result, hunks


def _validate_syntax(patched_src: str) -> Optional[str]:
    """Return error string if patched source has syntax errors, else None."""
    import ast
    try:
        ast.parse(patched_src)
        return None
    except SyntaxError as exc:
        return f"SyntaxError at line {exc.lineno}: {exc.msg}"


def _print_report(hunks: List[Hunk]) -> None:
    print("\n── Hunk Application Report " + "─" * 43)
    for h in hunks:
        if h.skipped:
            status = "\033[93m SKIP\033[0m  (already applied)"
        elif h.applied:
            status = "\033[92m PASS\033[0m"
        else:
            status = f"\033[91m FAIL\033[0m  {h.error}"
        print(f"  [{h.hunk_id}] {h.description[:55]:<55} {status}")
    print("─" * 70)

    n_applied = sum(1 for h in hunks if h.applied)
    n_skipped = sum(1 for h in hunks if h.skipped)
    n_failed  = sum(1 for h in hunks if not h.applied and not h.skipped)
    print(f"  Applied: {n_applied}  Skipped: {n_skipped}  Failed: {n_failed}")
    print()


# ── Typing shim ──────────────────────────────────────────────────────────────
from typing import Dict, List, Optional, Tuple


def main() -> int:
    parser = argparse.ArgumentParser(
        description="AVA Coverage Integration Patch v2.0",
    )
    parser.add_argument("ava_py", help="Path to ava.py to patch")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview without writing")
    parser.add_argument("--status", action="store_true",
                        help="Show which hunks are already applied")
    parser.add_argument("--backup-dir", default=".",
                        help="Directory for .bak file (default: same as ava.py)")
    parser.add_argument("--output",
                        help="Write patched file here instead of <ava_py>_patched.py")
    args = parser.parse_args()

    ava_path = Path(args.ava_py)
    if not ava_path.exists():
        print(f"ERROR: {ava_path} not found", file=sys.stderr)
        return 2

    src = ava_path.read_text(encoding="utf-8", errors="replace")

    if args.status:
        print(f"\nStatus for {ava_path}:")
        for h in HUNKS:
            applied  = h.replace in src
            searched = h.search in src
            print(f"  [{h.hunk_id}] {h.description[:55]:<55} "
                  f"{'✔ applied' if applied else ('✘ pending' if searched else '? not found')}")
        print()
        return 0

    patched, updated_hunks = _apply_hunks(src, HUNKS, dry_run=args.dry_run)
    _print_report(updated_hunks)

    failed = [h for h in updated_hunks if not h.applied and not h.skipped]
    if failed:
        print(f"\033[91mERROR: {len(failed)} hunk(s) failed — patched file NOT written.\033[0m\n")
        return 1

    if args.dry_run:
        print("Dry-run complete — no files written.")
        return 0

    # Syntax check
    syntax_err = _validate_syntax(patched)
    if syntax_err:
        print(f"\033[91mSyntax error in patched file: {syntax_err}\033[0m\n")
        return 1

    # Backup original
    ts    = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    bak   = Path(args.backup_dir) / f"{ava_path.name}.bak_{ts}"
    shutil.copy2(ava_path, bak)
    print(f"Backup: {bak}")

    # Write patched file
    out_path = Path(args.output) if args.output else Path(str(ava_path).replace(".py", "_patched.py"))
    out_path.write_text(patched, encoding="utf-8")
    print(f"Patched: {out_path}")
    print(f"\nTo activate: mv {out_path} {ava_path}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
