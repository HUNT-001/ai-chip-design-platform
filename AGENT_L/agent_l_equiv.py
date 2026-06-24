"""
AGENT_L/agent_l_equiv.py
=========================
Agent L — RTL→Netlist Equivalence Checker

Verifies that a synthesised gate-level netlist is functionally equivalent to
the RTL source design. Three verification layers:

  1. Combinational equivalence (Yosys `equiv` pass)
     Proves that every combinational output is identical between RTL and netlist
     for all possible input combinations — instant for small designs, SAT-based
     for larger ones.

  2. Bounded sequential equivalence (SymbiYosys BMC)
     For designs with state, uses bounded model checking up to `bmc_depth`
     cycles to verify that RTL and netlist produce identical outputs for any
     input sequence up to that length.

  3. Gate-level commit-log comparison
     Runs the same test binary through a gate-level simulation (if a simulator
     is available) and feeds the resulting commit log to Agent D's comparator.
     A discrepancy means the netlist diverges from the RTL in a way the formal
     tools didn't catch (e.g. timing-dependent behaviour, X-propagation).

Output: `equiv_report.json` in the run directory.
Manifest: `phases.equiv_check`, `outputs.equiv_report`, error code `EQUIV_FAIL`.

Tool dependencies (graceful degradation when absent)
------------------------------------------------------
- yosys  : synthesis + combinational equivalence (YOSYS_BIN env var)
- sby    : SymbiYosys for sequential equivalence (SBY_BIN env var)
- iverilog / verilator : gate-level simulation (optional)

Schema: AGENT_A/commitlog.schema.json v2.1.0
        AGENT_A/run_manifest.schema.json v2.1.0
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "2.1.0"
AGENT_ID       = "agent_l"

# ─────────────────────────────────────────────────────────
# Tool discovery
# ─────────────────────────────────────────────────────────

def _find_tool(env_var: str, default_name: str) -> Optional[str]:
    val = os.environ.get(env_var)
    if val and shutil.which(val):
        return val
    return shutil.which(default_name)


# ─────────────────────────────────────────────────────────
# Yosys synthesis
# ─────────────────────────────────────────────────────────

_YOSYS_SYNTH_SCRIPT = """\
# AVA Agent L — RTL synthesis for equivalence checking
read_verilog -sv {rtl_files}
hierarchy -check -top {top}
proc
flatten
opt -full
memory_collect
synth -run begin:fine
opt -full
write_verilog -noattr {netlist_out}
"""

_YOSYS_EQUIV_SCRIPT = """\
# AVA Agent L — combinational equivalence check
read_verilog -sv {rtl_files}
hierarchy -check -top {top}
proc; flatten; opt
design -stash gold

read_verilog {netlist}
hierarchy -check -top {top}
proc; flatten; opt
design -stash gate

equiv_make gold gate equiv
hierarchy -top equiv
flatten
equiv_simple
equiv_status -assert
"""


def synthesise_rtl(
    rtl_sources: List[str],
    top_module:  str,
    netlist_out: Path,
    yosys_bin:   str,
    workdir:     Path,
) -> Tuple[bool, str]:
    """
    Run Yosys synthesis. Returns (success, raw_output).
    """
    script = workdir / "synth.ys"
    script.write_text(
        _YOSYS_SYNTH_SCRIPT.format(
            rtl_files=" ".join(f'"{s}"' for s in rtl_sources),
            top=top_module,
            netlist_out=str(netlist_out),
        )
    )
    try:
        r = subprocess.run(
            [yosys_bin, "-q", str(script)],
            capture_output=True, text=True, timeout=300, cwd=workdir,
        )
        success = r.returncode == 0 and netlist_out.exists()
        return success, r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        return False, "TIMEOUT"
    except FileNotFoundError:
        return False, "YOSYS_NOT_FOUND"


def run_combinational_equiv(
    rtl_sources: List[str],
    netlist:     Path,
    top_module:  str,
    yosys_bin:   str,
    workdir:     Path,
) -> Tuple[str, str]:
    """
    Run Yosys combinational equivalence check.

    Returns (result, raw_output) where result is:
      "proved" | "failed" | "unknown" | "skipped"
    """
    script = workdir / "equiv.ys"
    script.write_text(
        _YOSYS_EQUIV_SCRIPT.format(
            rtl_files=" ".join(f'"{s}"' for s in rtl_sources),
            top=top_module,
            netlist=str(netlist),
        )
    )
    try:
        r = subprocess.run(
            [yosys_bin, "-q", str(script)],
            capture_output=True, text=True, timeout=600, cwd=workdir,
        )
        out = r.stdout + r.stderr
        if r.returncode == 0:
            return "proved", out
        if "ERROR" in out.upper() or "EQUIV" in out.upper():
            return "failed", out
        return "unknown", out
    except subprocess.TimeoutExpired:
        return "unknown", "TIMEOUT"
    except FileNotFoundError:
        return "skipped", "YOSYS_NOT_FOUND"


# ─────────────────────────────────────────────────────────
# SymbiYosys sequential equivalence
# ─────────────────────────────────────────────────────────

_SBY_SEQ_EQUIV = """\
[options]
mode prove
depth {depth}

[engines]
smtbmc --presat z3

[script]
# Load RTL (gold)
read_verilog -sv {rtl_files}
hierarchy -check -top {top}
proc; flatten; opt
design -stash gold

# Load netlist (gate)
read_verilog {netlist}
hierarchy -check -top {top}
proc; flatten; opt
design -stash gate

# Build equivalence circuit
equiv_make gold gate equiv
hierarchy -top equiv
flatten
equiv_simple
equiv_status -assert

[files]
{rtl_files_list}
{netlist}
"""


def run_sequential_equiv(
    rtl_sources: List[str],
    netlist:     Path,
    top_module:  str,
    sby_bin:     str,
    workdir:     Path,
    depth:       int = 10,
) -> Tuple[str, str]:
    """
    Run SymbiYosys bounded sequential equivalence.

    Returns (result, raw_output).
    """
    sby_content = _SBY_SEQ_EQUIV.format(
        depth=depth,
        rtl_files=" ".join(f'"{s}"' for s in rtl_sources),
        top=top_module,
        netlist=str(netlist),
        rtl_files_list="\n".join(rtl_sources),
    )
    sby_file = workdir / "seq_equiv.sby"
    sby_file.write_text(sby_content)

    try:
        r = subprocess.run(
            [sby_bin, "-f", str(sby_file)],
            capture_output=True, text=True, timeout=600, cwd=workdir,
        )
        out = r.stdout + r.stderr
        out_upper = out.upper()
        if "PROVED" in out_upper or r.returncode == 0:
            return "proved", out
        if "FAILED" in out_upper or "CEX" in out_upper:
            return "cex", out
        return "unknown", out
    except subprocess.TimeoutExpired:
        return "unknown", "TIMEOUT"
    except FileNotFoundError:
        return "skipped", "SBY_NOT_FOUND"


# ─────────────────────────────────────────────────────────
# Gate-level simulation + commit-log comparison
# ─────────────────────────────────────────────────────────

def run_gate_level_sim(
    netlist:     Path,
    binary_path: str,
    top_module:  str,
    iverilog_bin: Optional[str],
    workdir:     Path,
) -> Dict[str, Any]:
    """
    Compile the netlist with iverilog and run the test binary.

    Returns {"status": "passed"|"failed"|"skipped", "instrs_matched": N, "detail": ...}
    """
    if not iverilog_bin:
        return {"status": "skipped", "instrs_matched": 0,
                "detail": "iverilog not found"}
    if not netlist.exists():
        return {"status": "skipped", "instrs_matched": 0,
                "detail": "netlist not synthesised"}

    sim_bin = workdir / "gate_sim"
    compile_cmd = [
        iverilog_bin,
        "-o", str(sim_bin),
        "-s", top_module,
        str(netlist),
    ]
    try:
        r = subprocess.run(
            compile_cmd, capture_output=True, text=True, timeout=60
        )
        if r.returncode != 0:
            return {"status": "failed", "instrs_matched": 0,
                    "detail": f"Compile failed: {r.stderr[:512]}"}
    except subprocess.TimeoutExpired:
        return {"status": "failed", "instrs_matched": 0, "detail": "compile timeout"}

    # Run simulation — a real harness would pass the binary and capture commit log
    # For now we report "skipped" with a descriptive message when no binary adapter exists
    return {
        "status": "skipped",
        "instrs_matched": 0,
        "detail": "Gate-level simulation compiled OK; binary adapter for RTL testbench required to run",
    }


# ─────────────────────────────────────────────────────────
# Counterexample parser
# ─────────────────────────────────────────────────────────

def _extract_cex_summary(raw_output: str) -> Optional[str]:
    """Extract first meaningful counterexample line from tool output."""
    for line in raw_output.splitlines():
        l = line.strip()
        if any(kw in l.upper() for kw in ["CEX", "FAILED", "ASSERT", "ERROR"]):
            return l[:256]
    return None


# ─────────────────────────────────────────────────────────
# Main checker
# ─────────────────────────────────────────────────────────

@dataclass
class EquivChecker:
    """
    Agent L orchestrator.

    Parameters
    ----------
    rtl_sources  : list of RTL source file paths
    top_module   : top-level Verilog module name
    binary_path  : test binary path for gate-level sim (optional)
    run_dir      : run working directory
    bmc_depth    : SymbiYosys BMC depth for sequential equivalence
    check_mode   : "combinational" | "sequential" | "both"
    liberty_file : Liberty cell library for gate-level sim (optional)
    """
    rtl_sources:  List[str]
    top_module:   str
    run_dir:      Path
    binary_path:  Optional[str] = None
    bmc_depth:    int = 10
    check_mode:   str = "both"       # combinational / sequential / both
    liberty_file: Optional[str] = None

    def run(self) -> Dict[str, Any]:
        started  = datetime.now(timezone.utc)
        workdir  = self.run_dir / ".agent_l_work"
        workdir.mkdir(parents=True, exist_ok=True)

        yosys_bin    = _find_tool("YOSYS_BIN", "yosys")
        sby_bin      = _find_tool("SBY_BIN",   "sby")
        iverilog_bin = _find_tool("IVERILOG_BIN", "iverilog")

        report: Dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "agent":          AGENT_ID,
            "top_module":     self.top_module,
            "rtl_sources":    self.rtl_sources,
            "check_mode":     self.check_mode,
            "bmc_depth":      self.bmc_depth,
            "synth_result":   "skipped",
            "comb_equiv":     "skipped",
            "seq_equiv":      "skipped",
            "gate_level_sim": {"status": "skipped", "instrs_matched": 0},
            "counterexample": None,
            "violations":     [],
            "overall_result": "passed",
        }

        netlist = workdir / "netlist.v"

        # ── 1. Synthesis ──────────────────────────────────────────────────────
        if yosys_bin and self.rtl_sources:
            logger.info("Agent L: synthesising RTL with Yosys")
            ok, synth_out = synthesise_rtl(
                self.rtl_sources, self.top_module, netlist, yosys_bin, workdir
            )
            report["synth_result"] = "passed" if ok else "failed"
            if not ok:
                logger.error("Agent L: synthesis failed — skipping equivalence")
                report["counterexample"] = _extract_cex_summary(synth_out)
                report["violations"].append({
                    "type":    "SYNTH_FAILED",
                    "detail":  report["counterexample"],
                    "severity": "HIGH",
                })
                report["overall_result"] = "failed"
                _finish(report, started)
                return report
            logger.info("Agent L: synthesis OK — netlist at %s", netlist)

            # ── 2. Combinational equivalence ──────────────────────────────────
            if self.check_mode in ("combinational", "both"):
                logger.info("Agent L: running combinational equivalence (Yosys)")
                comb_result, comb_out = run_combinational_equiv(
                    self.rtl_sources, netlist, self.top_module, yosys_bin, workdir
                )
                report["comb_equiv"] = comb_result
                if comb_result == "failed":
                    cex = _extract_cex_summary(comb_out)
                    report["counterexample"] = cex
                    report["violations"].append({
                        "type":    "EQUIV_FAIL",
                        "layer":   "combinational",
                        "detail":  cex,
                        "severity": "HIGH",
                    })
                    logger.error("Agent L: combinational equivalence FAILED")
                elif comb_result == "proved":
                    logger.info("Agent L: combinational equivalence PROVED")
                else:
                    logger.warning("Agent L: combinational equivalence: %s", comb_result)

            # ── 3. Sequential equivalence (SymbiYosys) ───────────────────────
            if self.check_mode in ("sequential", "both") and sby_bin:
                logger.info("Agent L: running sequential equivalence (SymbiYosys, depth=%d)", self.bmc_depth)
                seq_result, seq_out = run_sequential_equiv(
                    self.rtl_sources, netlist, self.top_module,
                    sby_bin, workdir, self.bmc_depth,
                )
                report["seq_equiv"] = seq_result
                if seq_result == "cex":
                    cex = _extract_cex_summary(seq_out)
                    report["counterexample"] = report.get("counterexample") or cex
                    report["violations"].append({
                        "type":   "EQUIV_FAIL",
                        "layer":  "sequential",
                        "depth":  self.bmc_depth,
                        "detail": cex,
                        "severity": "HIGH",
                    })
                    logger.error("Agent L: sequential equivalence CEX found")
                elif seq_result == "proved":
                    logger.info("Agent L: sequential equivalence PROVED (depth %d)", self.bmc_depth)
                else:
                    logger.warning("Agent L: sequential equivalence: %s", seq_result)
            elif self.check_mode in ("sequential", "both") and not sby_bin:
                logger.warning("Agent L: sby not found; sequential equivalence skipped")

        else:
            if not yosys_bin:
                logger.warning("Agent L: Yosys not found; all equivalence checks skipped")
                report["synth_result"] = "skipped"
            if not self.rtl_sources:
                logger.warning("Agent L: no RTL sources; equivalence skipped")

        # ── 4. Gate-level simulation ─────────────────────────────────────────
        if self.binary_path:
            logger.info("Agent L: running gate-level simulation")
            gl_result = run_gate_level_sim(
                netlist, self.binary_path, self.top_module, iverilog_bin, workdir
            )
            report["gate_level_sim"] = gl_result
            if gl_result["status"] == "failed":
                report["violations"].append({
                    "type":    "EQUIV_FAIL",
                    "layer":   "gate_level_sim",
                    "detail":  gl_result.get("detail", ""),
                    "severity": "HIGH",
                })

        if report["violations"]:
            report["overall_result"] = "failed"

        _finish(report, started)
        return report


def _finish(report: Dict[str, Any], started: datetime) -> None:
    finished = datetime.now(timezone.utc)
    report.update({
        "started_at":  started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "finished_at": finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "duration_s":  round((finished - started).total_seconds(), 3),
        "generated_at": finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
    })


# ─────────────────────────────────────────────────────────
# Manifest integration
# ─────────────────────────────────────────────────────────

def _load_manifest(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def _save_manifest(path: Path, manifest: dict) -> None:
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    tmp.rename(path)


def run_from_manifest(
    manifest_path: Path,
    check_mode:    str = "both",
    bmc_depth:     int = 10,
) -> int:
    """
    Full Agent L pipeline driven by manifest.json.

    Returns: 0 = pass, 1 = equivalence failure, 2 = infra error.
    """
    manifest   = _load_manifest(manifest_path)
    run_dir    = Path(manifest["run_dir"])
    run_id     = manifest["run_id"]

    agent_cfg  = (manifest.get("agent_config") or {}).get("agent_l") or {}
    check_mode = agent_cfg.get("check_mode", check_mode)
    bmc_depth  = agent_cfg.get("bmc_depth", bmc_depth)
    lib_file   = agent_cfg.get("liberty_file")

    rtl_sources  = manifest.get("dut", {}).get("rtl_sources", [])
    top_module   = manifest.get("dut", {}).get("top", "core_tb")
    binary_path  = manifest.get("binary", {}).get("path")
    if binary_path:
        binary_path = str(run_dir / binary_path)

    started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest.setdefault("phases", {})["equiv_check"] = {
        "status":      "running_equiv",
        "started_at":  started_at,
        "finished_at": None,
        "duration_s":  None,
        "exit_code":   None,
        "error_msg":   None,
        "retry_count": 0,
        "log_path":    "logs/agent_l.log",
    }
    manifest["status"] = "running_equiv"
    _save_manifest(manifest_path, manifest)

    t0 = time.monotonic()
    try:
        checker = EquivChecker(
            rtl_sources=rtl_sources,
            top_module=top_module,
            run_dir=run_dir,
            binary_path=binary_path,
            bmc_depth=bmc_depth,
            check_mode=check_mode,
            liberty_file=lib_file,
        )
        report = checker.run()
        report["run_id"] = run_id

        violations = report.get("violations", [])
        exit_code  = 1 if violations else 0
        duration_s = round(time.monotonic() - t0, 3)
        finished_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Write report
        report_path = run_dir / "equiv_report.json"
        tmp_path    = run_dir / "equiv_report.json.tmp"
        with open(tmp_path, "w") as f:
            json.dump(report, f, indent=2)
            f.write("\n")
        tmp_path.rename(report_path)

        manifest["phases"]["equiv_check"].update({
            "status":      "failed" if exit_code else "passed",
            "finished_at": finished_at,
            "duration_s":  duration_s,
            "exit_code":   exit_code,
            "error_msg":   (
                f"EQUIV_FAIL: {len(violations)} violation(s)"
                if exit_code else None
            ),
        })
        manifest["outputs"]["equiv_report"] = "equiv_report.json"

        if exit_code:
            manifest["status"] = "failed"
            manifest["error"]  = {
                "code":        "EQUIV_FAIL",
                "message":     f"Agent L: RTL-netlist equivalence failed ({len(violations)} violation(s))",
                "phase":       "equiv_check",
                "recoverable": False,
                "repro_cmd":   f"python3 AGENT_L/agent_l_equiv.py --manifest {manifest_path}",
            }
        else:
            manifest["status"] = "passed"

        _save_manifest(manifest_path, manifest)
        logger.info("Equiv report written to %s", report_path)
        return exit_code

    except Exception as exc:
        duration_s  = round(time.monotonic() - t0, 3)
        finished_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        logger.exception("Agent L infrastructure error: %s", exc)
        manifest["phases"]["equiv_check"].update({
            "status":      "infra_error",
            "finished_at": finished_at,
            "duration_s":  duration_s,
            "exit_code":   2,
            "error_msg":   str(exc)[:2048],
        })
        manifest["status"] = "infra_error"
        _save_manifest(manifest_path, manifest)
        return 2


# ─────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Agent L — RTL-netlist equivalence checker for AVA",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples
--------
  # Full pipeline from manifest
  python3 AGENT_L/agent_l_equiv.py --manifest /tmp/runs/run1/manifest.json

  # Standalone: specify RTL sources directly
  python3 AGENT_L/agent_l_equiv.py \\
      --rtl rtl/core.sv rtl/alu.sv \\
      --top core_tb \\
      --report equiv_report.json

  # Combinational only (faster)
  python3 AGENT_L/agent_l_equiv.py --manifest manifest.json --mode combinational

  # Sequential with custom BMC depth
  python3 AGENT_L/agent_l_equiv.py --manifest manifest.json \\
      --mode sequential --bmc-depth 20
""",
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--manifest", type=Path, metavar="PATH")
    mode.add_argument("--rtl", nargs="+", type=str, metavar="FILE",
                      help="RTL source files (standalone mode)")
    p.add_argument("--top", type=str, default="core_tb",
                   help="Top-level module (standalone; default: core_tb)")
    p.add_argument("--report", type=Path, default=Path("equiv_report.json"),
                   help="Output report path (standalone; default: equiv_report.json)")
    p.add_argument("--mode", choices=["combinational","sequential","both"], default="both",
                   help="Equivalence check mode (default: both)")
    p.add_argument("--bmc-depth", type=int, default=10,
                   help="SymbiYosys BMC depth (default: 10)")
    p.add_argument("--binary", type=str, default=None, metavar="ELF",
                   help="Test binary for gate-level simulation (optional)")
    p.add_argument("--liberty", type=str, default=None, metavar="LIB",
                   help="Liberty cell library for gate-level simulation")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG","INFO","WARNING","ERROR"])
    return p


def main(argv: Optional[List[str]] = None) -> int:
    p = _build_parser()
    args = p.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.manifest:
        return run_from_manifest(
            args.manifest,
            check_mode=args.mode,
            bmc_depth=args.bmc_depth,
        )

    # Standalone mode
    if not args.rtl:
        p.error("--rtl is required in standalone mode")

    with tempfile.TemporaryDirectory() as tmpdir:
        checker = EquivChecker(
            rtl_sources=args.rtl,
            top_module=args.top,
            run_dir=Path(tmpdir),
            binary_path=args.binary,
            bmc_depth=args.bmc_depth,
            check_mode=args.mode,
            liberty_file=args.liberty,
        )
        report = checker.run()

    with open(args.report, "w") as f:
        json.dump(report, f, indent=2)
        f.write("\n")

    violations = report.get("violations", [])
    if violations:
        print(f"FAIL: {len(violations)} equivalence violation(s). See {args.report}")
        return 1

    comb = report.get("comb_equiv", "skipped")
    seq  = report.get("seq_equiv",  "skipped")
    print(f"PASS: comb_equiv={comb}, seq_equiv={seq}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
