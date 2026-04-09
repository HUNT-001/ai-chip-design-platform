#!/usr/bin/env python3
"""
backends/run_rtl.py — Verilator RTL runner, AVA v2.0.0.

TWO MODES
---------
A) Manifest mode (recommended — used by orchestrator):
     python backends/run_rtl.py --manifest runs/run_42/run_manifest.json

   Reads from manifest:  runid, seed, binary (elf), dut, rundir
   Writes back to manifest (atomic):
       phases.build, phases.rtl
       outputs.rtlcommitlog, outputs.coverageraw, outputs.waveform,
       outputs.signature, outputs.totalcycles

B) Standalone mode (development / CI):
     python backends/run_rtl.py \
         --rtl rtl/example_cpu/rv32im_core.v rtl/example_cpu/cpu_top.v \
         --top cpu_top  --elf tests/add_loop.elf \
         --seed 42      --out runs/run_42

Outputs in <rundir>/:
   rtl.commitlog.jsonl   AVA v2.0.0 JSONL retire stream
   coverage.dat          Verilator raw counters  (NOT rtl.coverage.dat)
   coverage_report.json  parsed {line, branch, toggle, functional} 0..1
   rtl.fst               FST waveform (--trace, 5-20x smaller than VCD)
   signature.hex         RISCOF hex words (--sig-out)
   run_manifest.json     patched manifest (manifest mode)
   build/                Verilator build artefacts

Verilator flags:
   --cc --exe --build    C++ model + compile
   --coverage            line + branch + expression
   --coverage-underscore also cover _-prefixed signals
   --assert              SVA checks
   -O2                   optimise generated C++
   --trace-fst           FST trace (replaces --trace / VCD; natively GTKWave)
   -DVM_TRACE_FST        tell sim_main.cpp FST is enabled
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

SCRIPT_DIR  = Path(__file__).resolve().parent
SIM_SRC_DIR = SCRIPT_DIR / "sim"


# ─────────────────────────────────────────────────────────────────────────────
# Argument parsing
# ─────────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Verilator RTL runner for AVA v2.0.0.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # ── manifest mode (mutually exclusive with standalone flags)
    p.add_argument("--manifest",   metavar="PATH",
                   help="Path to orchestrator run_manifest.json (manifest mode).")

    # ── standalone mode flags
    p.add_argument("--rtl",       nargs="+", metavar="FILE")
    p.add_argument("--top",       metavar="MODULE")
    p.add_argument("--elf",       metavar="FILE")
    p.add_argument("--seed",      type=int,   default=42)
    p.add_argument("--out",       metavar="DIR")

    # ── common options
    p.add_argument("--max-insns",   type=int, default=100_000)
    p.add_argument("--flush-every", type=int, default=1000,
                   help="Flush commit log every N instructions (crash safety).")
    p.add_argument("--mem-base",  default="0x80000000")
    p.add_argument("--mem-size",  type=int, default=64 * 1024 * 1024)
    p.add_argument("--trace",     action="store_true",
                   help="Enable FST waveform dump (5-20x smaller than VCD).")
    p.add_argument("--sig-out",   metavar="PATH",
                   help="Write RISCOF signature to this file.")
    p.add_argument("--sig-begin", default="0x80002000",
                   help="Signature region start (hex). Default: 0x80002000.")
    p.add_argument("--sig-end",   default="0x80002040",
                   help="Signature region end (hex). Default: 0x80002040.")
    p.add_argument("--jobs",      type=int, default=4)
    p.add_argument("--verilator", default="verilator")
    p.add_argument("--verbose",   action="store_true")
    p.add_argument("--rebuild",   action="store_true")
    return p


# ─────────────────────────────────────────────────────────────────────────────
# Manifest helpers  (atomic read-modify-write)
# ─────────────────────────────────────────────────────────────────────────────

def load_manifest(path: Path) -> Dict[str, Any]:
    with open(path) as f:
        return json.load(f)


def patch_manifest(path: Path, updates: Dict[str, Any]) -> None:
    """Read manifest, deep-merge updates, write atomically via tmp→rename."""
    with open(path) as f:
        data = json.load(f)
    _deep_merge(data, updates)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)   # atomic on POSIX


def _deep_merge(base: dict, overlay: dict) -> None:
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


# ─────────────────────────────────────────────────────────────────────────────
# Resolve run parameters from manifest or CLI
# ─────────────────────────────────────────────────────────────────────────────

def resolve_params(args: argparse.Namespace) -> Dict[str, Any]:
    """
    Returns a normalised params dict with keys:
        runid, seed, elf, top, rtl_files, run_dir, manifest_path
    """
    if args.manifest:
        mp = Path(args.manifest).resolve()
        if not mp.exists():
            sys.exit(f"[run_rtl] Manifest not found: {mp}")
        m = load_manifest(mp)
        run_dir = Path(m["rundir"]).resolve()
        run_dir.mkdir(parents=True, exist_ok=True)
        return {
            "runid":         m["runid"],
            "seed":          m["seed"],
            "elf":           m["binary"],
            "top":           m["dut"],
            "rtl_files":     m.get("rtl_files", []),
            "run_dir":       run_dir,
            "manifest_path": mp,
        }
    else:
        # Standalone mode — validate required args
        missing = [f for f in ("rtl", "top", "elf", "out") if not getattr(args, f.replace("-","_"), None)]
        if missing:
            sys.exit(f"[run_rtl] Standalone mode requires: {missing}  (or use --manifest)")
        run_dir = Path(args.out).resolve()
        run_dir.mkdir(parents=True, exist_ok=True)
        return {
            "runid":         str(uuid.uuid4()),
            "seed":          args.seed,
            "elf":           args.elf,
            "top":           args.top,
            "rtl_files":     args.rtl or [],
            "run_dir":       run_dir,
            "manifest_path": None,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Build step
# ─────────────────────────────────────────────────────────────────────────────

def verilator_build(
    args:      argparse.Namespace,
    params:    Dict[str, Any],
    build_dir: Path,
) -> Path:
    top      = params["top"]
    rtl_list = params["rtl_files"]
    exe      = build_dir / f"V{top}"

    if exe.exists() and not args.rebuild:
        print(f"[build] Reusing: {exe}")
        return exe

    build_dir.mkdir(parents=True, exist_ok=True)

    cmd: List[str] = [
        args.verilator,
        "--cc", "--exe", "--build",
        "--coverage", "--coverage-underscore",
        "--assert", "-O2",
        "--top-module", top,
        "--Mdir", str(build_dir),
        "-j", str(args.jobs),
    ]

    # FST tracing (default when --trace; 5-20x smaller than VCD)
    if args.trace:
        cmd += [
            "--trace-fst",
            "-CFLAGS", "-DVM_TRACE_FST",
        ]

    cmd += ["-CFLAGS", f"-DDUT_HEADER='\"V{top}.h\"'"]
    cmd += ["-CFLAGS", f"-I{SIM_SRC_DIR}"]
    cmd += [str(Path(f).resolve()) for f in rtl_list]
    cmd += [str((SIM_SRC_DIR / "sim_main.cpp").resolve())]

    print("[build] " + " ".join(shlex.quote(c) for c in cmd))
    t0 = datetime.now(timezone.utc)
    r  = subprocess.run(cmd)
    elapsed = (datetime.now(timezone.utc) - t0).total_seconds()

    if r.returncode != 0:
        raise RuntimeError(f"Verilator build failed (exit {r.returncode})")
    if not exe.exists():
        raise RuntimeError(f"Binary not found: {exe}")

    print(f"[build] Done in {elapsed:.1f}s — {exe}")
    return exe, elapsed


# ─────────────────────────────────────────────────────────────────────────────
# Simulation step
# ─────────────────────────────────────────────────────────────────────────────

def run_simulation(
    args:    argparse.Namespace,
    params:  Dict[str, Any],
    exe:     Path,
    run_dir: Path,
) -> dict:
    commitlog = run_dir / "rtl.commitlog.jsonl"
    coverage  = run_dir / "coverage.dat"          # renamed from rtl.coverage.dat
    trace_out = run_dir / "rtl.fst" if args.trace else None   # FST not VCD
    sig_out   = Path(args.sig_out).resolve() if args.sig_out else None

    cmd: List[str] = [
        str(exe),
        "--elf",         str(Path(params["elf"]).resolve()),
        "--runid",       params["runid"],
        "--commit",      str(commitlog),
        "--cov",         str(coverage),
        "--maxinsns",    str(args.max_insns),
        "--flush-every", str(args.flush_every),
        "--membase",     args.mem_base,
        "--memsize",     str(args.mem_size),
        "--seed",        str(params["seed"]),
    ]
    if trace_out:
        cmd += ["--trace", str(trace_out)]
    if sig_out:
        cmd += [
            "--sig-out",   str(sig_out),
            "--sig-begin", args.sig_begin,
            "--sig-end",   args.sig_end,
        ]
    if args.verbose:
        cmd.append("--verbose")

    print("[sim] " + " ".join(shlex.quote(c) for c in cmd))
    t0 = datetime.now(timezone.utc)
    r  = subprocess.run(cmd)
    elapsed = (datetime.now(timezone.utc) - t0).total_seconds()

    if r.returncode not in (0, 1):
        raise RuntimeError(f"Simulation crashed (exit {r.returncode})")

    retired = 0
    if commitlog.exists():
        with open(commitlog) as f:
            retired = sum(1 for ln in f if ln.strip())

    # Estimate total cycles from commit log (parse last seq)
    total_cycles = retired * 3  # rough: 3 cycles/insn for optimised core
    if commitlog.exists() and retired > 0:
        try:
            with open(commitlog) as f:
                lines = [l.strip() for l in f if l.strip()]
            if lines:
                last = json.loads(lines[-1])
                total_cycles = last.get("seq", retired) * 3
        except Exception:
            pass

    print(f"[sim] Done in {elapsed:.2f}s — retired={retired:,}  cycles≈{total_cycles:,}")
    return {
        "commitlog_path": str(commitlog),
        "coverage_path":  str(coverage),
        "trace_path":     str(trace_out) if trace_out and trace_out.exists() else None,
        "sig_path":       str(sig_out) if sig_out and sig_out.exists() else None,
        "retired":        retired,
        "total_cycles":   total_cycles,
        "sim_exit_code":  r.returncode,
        "elapsed_sec":    elapsed,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Coverage parsing
# ─────────────────────────────────────────────────────────────────────────────

def parse_coverage(dat_file: Path) -> dict:
    """Parse Verilator coverage.dat → {line, branch, toggle, functional} ratios 0..1."""
    if not dat_file.exists():
        return {"line": 0.0, "branch": 0.0, "toggle": 0.0, "functional": 0.0}

    buckets: Dict[str, Dict[str, int]] = {
        k: {"hit": 0, "total": 0} for k in ("line", "branch", "toggle")
    }
    with open(dat_file) as f:
        for raw in f:
            raw = raw.strip()
            if not raw or not raw.startswith("C "):
                continue
            parts = raw.split()
            try:
                count = int(parts[-1])
            except ValueError:
                continue
            joined = " ".join(parts)
            if "'line'" in joined or ",line," in joined:
                buckets["line"]["total"] += 1
                if count > 0: buckets["line"]["hit"] += 1
            elif any(t in joined for t in ("'b0'", "'b1'", ",b0,", ",b1,")):
                buckets["branch"]["total"] += 1
                if count > 0: buckets["branch"]["hit"] += 1
            elif any(t in joined for t in ("'tgl0'", "'tgl1'", ",tgl,")):
                buckets["toggle"]["total"] += 1
                if count > 0: buckets["toggle"]["hit"] += 1

    def ratio(k: str) -> float:
        t = buckets[k]["total"]
        return round(buckets[k]["hit"] / t, 4) if t else 0.0

    return {
        "line":       ratio("line"),
        "branch":     ratio("branch"),
        "toggle":     ratio("toggle"),
        "functional": 0.0,   # populated by Agent F's functional coverage harness
        "raw":        buckets,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = build_parser()
    args   = parser.parse_args()

    # Validate tool availability
    if shutil.which(args.verilator) is None and not Path(args.verilator).exists():
        sys.exit(f"[run_rtl] Verilator not found: {args.verilator}\n"
                 f"  Install: sudo apt install verilator")

    # ── Resolve run parameters ────────────────────────────────────────────────
    params    = resolve_params(args)
    run_dir   = params["run_dir"]
    build_dir = run_dir / "build"
    now_iso   = datetime.now(timezone.utc).isoformat()

    print(f"[ava] runid={params['runid']}  seed={params['seed']}")
    print(f"[ava] elf={params['elf']}")
    print(f"[ava] out={run_dir}")

    # ── Validate inputs ───────────────────────────────────────────────────────
    if not Path(params["elf"]).exists():
        sys.exit(f"[run_rtl] ELF not found: {params['elf']}")
    for f in params["rtl_files"]:
        if not Path(f).exists():
            sys.exit(f"[run_rtl] RTL file not found: {f}")

    build_elapsed = 0.0
    build_status  = "pass"

    try:
        exe, build_elapsed = verilator_build(args, params, build_dir)
    except RuntimeError as e:
        print(f"[build] FAILED: {e}")
        build_status = "fail"
        if params["manifest_path"]:
            patch_manifest(params["manifest_path"], {
                "phases": {"build": {
                    "status":      "fail",
                    "elapsed_sec": 0.0,
                    "timestamp":   now_iso,
                    "error":       str(e),
                }},
            })
        return 1

    # ── Simulate ──────────────────────────────────────────────────────────────
    try:
        sim = run_simulation(args, params, exe, run_dir)
    except RuntimeError as e:
        print(f"[sim] FAILED: {e}")
        if params["manifest_path"]:
            patch_manifest(params["manifest_path"], {
                "phases": {
                    "build": {"status": build_status, "elapsed_sec": build_elapsed,
                              "timestamp": now_iso},
                    "rtl":   {"status": "fail", "elapsed_sec": 0.0,
                              "timestamp": now_iso, "error": str(e)},
                },
            })
        return 1

    # ── Parse coverage ────────────────────────────────────────────────────────
    cov_raw  = parse_coverage(Path(sim["coverage_path"]))
    cov_rpt  = run_dir / "coverage_report.json"
    cov_rpt.write_text(json.dumps(cov_raw, indent=2))

    # ── Patch manifest (manifest mode) ────────────────────────────────────────
    if params["manifest_path"]:
        patch_manifest(params["manifest_path"], {
            "phases": {
                "build": {
                    "status":      build_status,
                    "elapsed_sec": round(build_elapsed, 3),
                    "timestamp":   now_iso,
                },
                "rtl": {
                    "status":      "pass" if sim["sim_exit_code"] == 0 else "fail",
                    "elapsed_sec": round(sim["elapsed_sec"], 3),
                    "retired":     sim["retired"],
                    "cycles":      sim["total_cycles"],
                    "timestamp":   now_iso,
                },
            },
            "outputs": {
                "rtlcommitlog": sim["commitlog_path"],
                "coverageraw":  sim["coverage_path"],      # coverage.dat
                "waveform":     sim["trace_path"],         # rtl.fst
                "signature":    sim["sig_path"],           # signature.hex
                "totalcycles":  sim["total_cycles"],
            },
        })
        print(f"[ava] Manifest patched: {params['manifest_path']}")
    else:
        # Standalone mode: write a full manifest for downstream consumers
        manifest = {
            "schemaversion": "2.0.0",
            "runid":   params["runid"],
            "seed":    params["seed"],
            "binary":  params["elf"],
            "dut":     params["top"],
            "rundir":  str(run_dir),
            "phases": {
                "build": {"status": build_status, "elapsed_sec": round(build_elapsed, 3),
                          "timestamp": now_iso},
                "rtl":   {"status": "pass" if sim["sim_exit_code"] == 0 else "fail",
                          "elapsed_sec": round(sim["elapsed_sec"], 3),
                          "retired": sim["retired"], "cycles": sim["total_cycles"],
                          "timestamp": now_iso},
            },
            "outputs": {
                "rtlcommitlog": sim["commitlog_path"],
                "coverageraw":  sim["coverage_path"],
                "waveform":     sim["trace_path"],
                "signature":    sim["sig_path"],
                "totalcycles":  sim["total_cycles"],
            },
        }
        mp = run_dir / "run_manifest.json"
        mp.write_text(json.dumps(manifest, indent=2))
        print(f"[ava] Manifest: {mp}")

    # ── Print summary ─────────────────────────────────────────────────────────
    status = "PASS" if sim["sim_exit_code"] == 0 else "FAIL"
    print(f"\n{'='*60}")
    print(f"  {status}  retired={sim['retired']:,}  cycles≈{sim['total_cycles']:,}")
    print(f"  cov line={cov_raw['line']*100:.1f}%  branch={cov_raw['branch']*100:.1f}%"
          f"  toggle={cov_raw['toggle']*100:.1f}%")
    print(f"{'='*60}")
    print(f"[ava] commit log  → {sim['commitlog_path']}")
    print(f"[ava] coverage    → {sim['coverage_path']}")
    print(f"[ava] cov report  → {cov_rpt}")
    if sim["trace_path"]:
        print(f"[ava] trace (fst) → {sim['trace_path']}")
    if sim["sig_path"]:
        print(f"[ava] signature   → {sim['sig_path']}")

    return 0 if sim["sim_exit_code"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
