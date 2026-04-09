#!/usr/bin/env python3
"""
run_iss.py  —  AVA Agent C: Spike Golden Backend
=================================================

Runs Spike (RISC-V ISS) for a given ELF and produces:
  <run_dir>/iss.commitlog.jsonl      — schema v2.0.0 JSONL commit log
  <run_dir>/outputs/iss_commitlog.jsonl  (manifest mode path)
  <run_dir>/manifest.json            — run metadata (updated / created)
  <run_dir>/logs/iss.log             — raw Spike stdout+stderr

AVA Orchestrator manifest mode (recommended)
--------------------------------------------
    python run_iss.py --manifest ./runs/run_001/manifest.json

    Reads from manifest:  rundir, binary, spikebin, isa
    Writes to manifest:   phases.iss.status, outputs.iss_commitlog
    Exit codes:
        0  PASS     — iss_commitlog.jsonl produced
        2  INFRA    — Spike or parser crash / timeout
        3  CONFIG   — missing ELF / bad manifest

Legacy direct mode (for debugging / Agent C standalone)
--------------------------------------------------------
    python run_iss.py --isa RV32IM --elf prog.elf --out ./run_001

    python run_iss.py --isa RV32IM --elf prog.elf --out ./run_001 \\
                      --spike /opt/riscv/bin/spike               \\
                      --pk    /opt/riscv/riscv32-unknown-elf/bin/pk \\
                      --max-instrs 100000 --seed 42              \\
                      --extra-spike-args "--misaligned"

End-to-end example (legacy mode)
---------------------------------
    # 1. Compile a minimal bare-metal test
    riscv32-unknown-elf-gcc -nostdlib -T link.ld test.s -o test.elf

    # 2. Run golden ISS (Spike must be on PATH or given via --spike)
    python run_iss.py --isa RV32IM --elf test.elf --out ./runs/run_001

    # 3. Inspect first 5 commit records
    head -5 ./runs/run_001/iss.commitlog.jsonl | python3 -m json.tool

    # 4. Count instructions (must match RTL commitlog line count)
    wc -l ./runs/run_001/iss.commitlog.jsonl

Definition of done
------------------
* iss.commitlog.jsonl created with schema_version=2.0.0 records
* For the same ELF as the RTL runner the instruction count matches
  (verify: wc -l iss.commitlog.jsonl == wc -l rtl.commitlog.jsonl)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ── Local import from same package ─────────────────────────────────────────
_SELF_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SELF_DIR))
from spike_parser import parse_spike_log_streaming, detect_format, SCHEMA_VERSION  # noqa: E402
from iss_efficiency import record_manifest_run  # noqa: E402

# ── Logging setup ───────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("run_iss")

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_SPIKE      = "spike"
DEFAULT_MAX_INSTRS = 10_000_000
COMMITLOG_FILENAME = "iss.commitlog.jsonl"
MANIFEST_FILENAME  = "manifest.json"
ISS_LOG_FILENAME   = "iss.log"

# ── AVA orchestrator exit codes ───────────────────────────────────────────────
EXIT_PASS   = 0   # commitlog produced successfully
EXIT_INFRA  = 2   # Spike/parser crash or timeout (retry may help)
EXIT_CONFIG = 3   # missing ELF, bad manifest, missing field (fix config)

# Normalised ISA string → Spike --isa value
_ISA_MAP: dict[str, str] = {
    "RV32I":     "rv32i",
    "RV32IM":    "rv32im",
    "RV32IMA":   "rv32ima",
    "RV32IMAC":  "rv32imac",
    "RV32IMAFC": "rv32imafc",
    "RV32G":     "rv32g",
    "RV32GC":    "rv32gc",
    "RV64I":     "rv64i",
    "RV64IM":    "rv64im",
    "RV64IMA":   "rv64ima",
    "RV64G":     "rv64g",
    "RV64GC":    "rv64gc",
}


# ─────────────────────────────────────────────────────────────────────────────
# Spike capability probe
# ─────────────────────────────────────────────────────────────────────────────

def probe_spike(spike_bin: str) -> dict:
    """
    Run 'spike --help' and infer available flags.

    Returns
    -------
    dict with keys:
      found           : bool  — binary reachable
      version         : str   — version string from help output
      has_log_commits : bool  — --log-commits flag present (FORMAT B, rich)
      has_commit_log  : bool  — --enable-commitlog flag (older name, FORMAT B)
      has_l_flag      : bool  — -l trace flag (all versions, FORMAT A)
    """
    spike_path = shutil.which(spike_bin) or spike_bin
    caps: dict = {
        "found":           Path(spike_path).exists() or bool(shutil.which(spike_bin)),
        "version":         "unknown",
        "has_log_commits": False,
        "has_commit_log":  False,
        "has_l_flag":      True,   # universally present
        "spike_path":      spike_path,
    }

    if not caps["found"]:
        return caps

    try:
        result = subprocess.run(
            [spike_path, "--help"],
            capture_output=True, text=True, timeout=15,
        )
        help_text = result.stdout + result.stderr

        for line in help_text.splitlines():
            low = line.lower()
            if "spike" in low and any(c.isdigit() for c in line):
                caps["version"] = line.strip()
                break

        caps["has_log_commits"] = "--log-commits" in help_text
        caps["has_commit_log"]  = "--enable-commitlog" in help_text

    except Exception as exc:  # noqa: BLE001
        log.warning("Spike probe failed: %s", exc)

    log.debug("Spike caps: %s", caps)
    return caps


# ─────────────────────────────────────────────────────────────────────────────
# Command builder
# ─────────────────────────────────────────────────────────────────────────────

def build_spike_cmd(
    spike_bin:   str,
    elf_path:    Path,
    isa:         str,
    max_instrs:  int,
    caps:        dict,
    pk_path:     Optional[str],
    extra_args:  List[str],
) -> Tuple[List[str], str]:
    """
    Construct the Spike command list.

    Returns
    -------
    (cmd, log_mode)
      cmd      : list[str]   — argv for subprocess
      log_mode : str         — 'log_commits' | 'enable_cl' | 'trace_only'

    Priority order for commit logging:
      1. --log-commits        → FORMAT B (richest: PC + instr + reg/mem writes)
      2. --enable-commitlog   → FORMAT B (older API, same data)
      3. -l                   → FORMAT A (PC + instr + disasm only, no reg values)
    """
    isa_lower = _ISA_MAP.get(isa.upper(), isa.lower())
    cmd: List[str] = [caps["spike_path"], f"--isa={isa_lower}"]

    # ── Commit log flag selection ──────────────────────────────────────────
    if caps.get("has_log_commits"):
        cmd.append("--log-commits")
        log_mode = "log_commits"
        log.info("Using Spike --log-commits (FORMAT B — registers included)")
    elif caps.get("has_commit_log"):
        cmd.append("--enable-commitlog")
        log_mode = "enable_cl"
        log.info("Using Spike --enable-commitlog (FORMAT B — registers included)")
    else:
        cmd.append("-l")
        log_mode = "trace_only"
        log.warning(
            "Spike does not support --log-commits or --enable-commitlog. "
            "Falling back to -l (FORMAT A — no register values). "
            "Upgrade Spike for full differential verification."
        )

    # ── Instruction count cap ──────────────────────────────────────────────
    # Spike ≥1.1 uses --max-commit-insns; older uses -m (memory size, different!)
    # We try --max-commit-insns; fall back is to not set it.
    cmd += ["--max-commit-insns", str(max_instrs)]

    # ── User extra args ────────────────────────────────────────────────────
    cmd.extend(extra_args)

    # ── ELF / pk ──────────────────────────────────────────────────────────
    if pk_path:
        log.info("Using proxy kernel: %s", pk_path)
        cmd += [pk_path, str(elf_path)]
    else:
        cmd.append(str(elf_path))

    return cmd, log_mode


# ─────────────────────────────────────────────────────────────────────────────
# Spike execution
# ─────────────────────────────────────────────────────────────────────────────

def run_spike_process(
    cmd:       List[str],
    log_file:  Path,
    timeout_s: int,
) -> Tuple[int, str]:
    """
    Execute Spike; capture combined stdout+stderr.

    Spike writes the commit log to *stderr* in all known versions;
    stdout is used for HTIF output.  We merge both streams.

    Returns (returncode, combined_text).
    Note: Spike exits with code 1 when the program calls tohost=1 (success);
    treat both 0 and 1 as potentially successful.
    """
    log.info("Executing: %s", " ".join(cmd))
    log_file.parent.mkdir(parents=True, exist_ok=True)

    lines: List[str] = []

    try:
        with open(log_file, "w") as flog:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # merge stderr into stdout pipe
                text=True,
                bufsize=1,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                flog.write(line)
                lines.append(line)
                if len(lines) % 100_000 == 0:
                    log.debug("... %d lines captured so far", len(lines))

            proc.wait(timeout=timeout_s)
            rc = proc.returncode

    except subprocess.TimeoutExpired:
        proc.kill()
        log.error("Spike subprocess timed out after %ds", timeout_s)
        rc = -9
    except FileNotFoundError:
        log.error("Spike binary not found: %s", cmd[0])
        rc = -127

    return rc, "".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Commit-log writer
# ─────────────────────────────────────────────────────────────────────────────

def write_commitlog(
    spike_output: str,
    out_file:     Path,
    fmt_hint:     Optional[str],
    max_records:  Optional[int],
) -> int:
    """
    Parse the raw Spike output and write JSONL to out_file.

    Returns the number of records written.
    """
    out_file.parent.mkdir(parents=True, exist_ok=True)
    count = 0

    with open(out_file, "w") as fout:
        for record in parse_spike_log_streaming(
            spike_output, source="iss", fmt=fmt_hint
        ):
            fout.write(json.dumps(record, separators=(",", ":")) + "\n")
            count += 1
            if max_records is not None and count >= max_records:
                log.warning("Capped at %d records (--max-records)", max_records)
                break

    return count


# ─────────────────────────────────────────────────────────────────────────────
# Schema validation (lightweight — no external deps)
# ─────────────────────────────────────────────────────────────────────────────

_PC_RE    = re.compile(r"^0x[0-9a-fA-F]{1,16}$")
_INSTR_RE = re.compile(r"^0x[0-9a-fA-F]{4,8}$")

def validate_commitlog(path: Path, sample_size: int = 200) -> List[str]:
    """
    Read up to `sample_size` records from path and check mandatory fields.

    Validates against AVA schema v2.0.0:
      schema_version, seq, pc, instr, src, hart, fpregs

    Returns list of error strings (empty means OK).
    """
    errors: List[str] = []
    try:
        with open(path) as f:
            for i, raw in enumerate(f):
                if i >= sample_size:
                    break
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError as exc:
                    errors.append(f"Line {i}: JSON parse error: {exc}")
                    continue

                # Mandatory v2.0.0 fields
                for fld in ("schema_version", "seq", "pc", "instr", "src", "hart"):
                    if fld not in rec:
                        errors.append(f"Line {i}: missing '{fld}'")

                if "schema_version" in rec and rec["schema_version"] != "2.0.0":
                    errors.append(f"Line {i}: schema_version='{rec['schema_version']}' expected '2.0.0'")
                if "pc" in rec and not _PC_RE.match(str(rec["pc"])):
                    errors.append(f"Line {i}: invalid pc='{rec['pc']}'")
                if "instr" in rec and not _INSTR_RE.match(str(rec["instr"])):
                    errors.append(f"Line {i}: invalid instr='{rec['instr']}'")
                if "seq" in rec and rec["seq"] != i:
                    errors.append(f"Line {i}: seq={rec['seq']} expected {i}")
                if "src" in rec and rec["src"] not in ("rtl", "iss", "formal"):
                    errors.append(f"Line {i}: invalid src='{rec['src']}'")
                if "hart" in rec and not isinstance(rec["hart"], int):
                    errors.append(f"Line {i}: hart must be int, got {type(rec['hart']).__name__}")
    except OSError as exc:
        errors.append(f"Cannot open {path}: {exc}")
    return errors


# ─────────────────────────────────────────────────────────────────────────────
# Manifest helpers
# ─────────────────────────────────────────────────────────────────────────────

def load_or_create_manifest(run_dir: Path, config: dict) -> dict:
    p = run_dir / MANIFEST_FILENAME
    if p.exists():
        with open(p) as f:
            return json.load(f)
    return {
        "run_id":    str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "config":    config,
        "inputs":    {},
        "outputs":   {},
        "status":    "pending",
        "bugs":      [],
    }


def save_manifest(run_dir: Path, manifest: dict) -> None:
    with open(run_dir / MANIFEST_FILENAME, "w") as f:
        json.dump(manifest, f, indent=2)


# ─────────────────────────────────────────────────────────────────────────────
# AVA Orchestrator manifest mode
# ─────────────────────────────────────────────────────────────────────────────

def atomic_update_manifest(manifest_path: Path, updates: Dict) -> None:
    """
    Atomically update a manifest JSON file using dotted-key notation.

    ``updates`` maps dotted paths to values:
        {"phases.iss.status": "completed", "outputs.iss_commitlog": "foo.jsonl"}
    becomes:
        manifest["phases"]["iss"]["status"] = "completed"
        manifest["outputs"]["iss_commitlog"] = "foo.jsonl"

    Uses write-to-tmp then rename for crash safety on POSIX filesystems.
    """
    manifest = json.loads(manifest_path.read_text())

    for dotted_key, value in updates.items():
        parts = dotted_key.split(".")
        node = manifest
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    tmp = manifest_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, indent=2, default=str))
    tmp.rename(manifest_path)   # atomic on POSIX


def run_iss_manifest(manifest_path: Path) -> int:
    """
    AVA Contract manifest mode — the recommended orchestrator entry-point.

    Reads from manifest
    -------------------
    rundir      : str   — working directory for this run
    binary      : str   — path to ELF binary
    isa         : str   — ISA string, e.g. "rv32im"  (default: "rv32im")
    spikebin    : str   — Spike binary name/path      (default: "spike")
    timeout     : int   — Spike timeout in seconds    (default: 300)
    max_instrs  : int   — instruction cap             (default: 10_000_000)

    Writes to manifest
    ------------------
    phases.iss.status        : "running" → "completed" | "error" | "timeout"
    phases.iss.duration_s    : float
    phases.iss.commit_count  : int
    phases.iss.log_mode      : str
    phases.iss.spike_exit    : int
    outputs.iss_commitlog    : relative path "outputs/iss_commitlog.jsonl"

    Exit codes
    ----------
    EXIT_PASS   (0) — commitlog written, phases.iss.status = "completed"
    EXIT_INFRA  (2) — Spike/parser crash or timeout
    EXIT_CONFIG (3) — missing ELF, missing manifest field, bad config
    """
    if not manifest_path.exists():
        log.error("Manifest not found: %s", manifest_path)
        return EXIT_CONFIG

    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as exc:
        log.error("Cannot parse manifest %s: %s", manifest_path, exc)
        return EXIT_CONFIG

    # ── Extract config from manifest ───────────────────────────────────────
    run_dir_str = manifest.get("rundir")
    binary_str  = manifest.get("binary")

    if not run_dir_str:
        log.error("Manifest missing 'rundir' field")
        atomic_update_manifest(manifest_path, {
            "phases.iss.status": "error",
            "phases.iss.error":  "manifest missing 'rundir'",
        })
        return EXIT_CONFIG

    if not binary_str:
        log.error("Manifest missing 'binary' field")
        atomic_update_manifest(manifest_path, {
            "phases.iss.status": "error",
            "phases.iss.error":  "manifest missing 'binary'",
        })
        return EXIT_CONFIG

    run_dir  = Path(run_dir_str)
    elf_path = Path(binary_str)

    if not elf_path.exists():
        log.error("ELF not found: %s", elf_path)
        atomic_update_manifest(manifest_path, {
            "phases.iss.status": "error",
            "phases.iss.error":  f"ELF missing: {elf_path}",
        })
        return EXIT_CONFIG

    spike_bin  = manifest.get("spikebin",   "spike")
    isa        = manifest.get("isa",        "rv32im").upper()
    timeout_s  = int(manifest.get("timeout",    300))
    max_instrs = int(manifest.get("max_instrs", DEFAULT_MAX_INSTRS))

    run_dir.mkdir(parents=True, exist_ok=True)

    # ── Mark phase as running ──────────────────────────────────────────────
    atomic_update_manifest(manifest_path, {"phases.iss.status": "running"})

    # ── Probe Spike ────────────────────────────────────────────────────────
    caps = probe_spike(spike_bin)
    if not caps["found"]:
        log.error("Spike not found: '%s'", spike_bin)
        atomic_update_manifest(manifest_path, {
            "phases.iss.status": "error",
            "phases.iss.error":  f"Spike binary not found: {spike_bin}",
        })
        return EXIT_INFRA

    # ── Build command and run ──────────────────────────────────────────────
    cmd, log_mode = build_spike_cmd(
        spike_bin=spike_bin,
        elf_path=elf_path,
        isa=isa,
        max_instrs=max_instrs,
        caps=caps,
        pk_path=None,
        extra_args=[],
    )

    iss_log = run_dir / "logs" / ISS_LOG_FILENAME
    t0 = time.monotonic()
    rc, spike_output = run_spike_process(cmd, iss_log, timeout_s)
    duration_s = round(time.monotonic() - t0, 3)

    log.info("Spike exited %d in %.2fs", rc, duration_s)

    if rc == -9:   # timeout sentinel set by run_spike_process
        atomic_update_manifest(manifest_path, {
            "phases.iss.status":     "timeout",
            "phases.iss.duration_s": duration_s,
        })
        return EXIT_INFRA

    if rc == -127 or not spike_output.strip():
        atomic_update_manifest(manifest_path, {
            "phases.iss.status": "error",
            "phases.iss.error":  f"Spike produced no output (exit={rc})",
            "phases.iss.duration_s": duration_s,
        })
        return EXIT_INFRA

    # ── Parse → iss_commitlog.jsonl ───────────────────────────────────────
    out_dir     = run_dir / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    commitlog   = out_dir / "iss_commitlog.jsonl"

    fmt_hint = "B" if log_mode in ("log_commits", "enable_cl") else None
    try:
        count = write_commitlog(spike_output, commitlog, fmt_hint, max_records=None)
    except Exception as exc:
        log.error("Parser failed: %s", exc, exc_info=True)
        atomic_update_manifest(manifest_path, {
            "phases.iss.status": "error",
            "phases.iss.error":  f"Parser exception: {exc}",
        })
        return EXIT_INFRA

    if count == 0:
        atomic_update_manifest(manifest_path, {
            "phases.iss.status": "error",
            "phases.iss.error":  "Parser produced zero records",
        })
        return EXIT_INFRA

    # ── Validate sample ────────────────────────────────────────────────────
    errs = validate_commitlog(commitlog, sample_size=50)
    if errs:
        log.warning("Commitlog validation warnings (first 3): %s", errs[:3])

    # ── Atomic manifest success update ────────────────────────────────────
    atomic_update_manifest(manifest_path, {
        "phases.iss.status":       "completed",
        "phases.iss.duration_s":   duration_s,
        "phases.iss.commit_count": count,
        "phases.iss.log_mode":     log_mode,
        "phases.iss.spike_exit":   rc,
        "outputs.iss_commitlog":   "outputs/iss_commitlog.jsonl",
    })

    # ── Track metrics + plateau detection ─────────────────────────────────
    db_path = run_dir / "iss_metrics.db"
    plateau = record_manifest_run(
        manifest_path=manifest_path,
        db_path=db_path,
        commit_count=count,
        duration_s=duration_s,
        log_mode=log_mode,
        spike_exit=rc,
    )
    if plateau:
        log.warning(
            "ISS coverage plateau detected for ISA=%s — "
            "consider rotating seeds or escalating to formal (Agent H).",
            isa,
        )
        atomic_update_manifest(manifest_path, {
            "phases.iss.plateau_detected": True,
        })

    log.info(
        "Agent C complete: %d commits → %s  (%.2fs)",
        count, commitlog, duration_s
    )
    return EXIT_PASS


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry-point
# ─────────────────────────────────────────────────────────────────────────────

def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="run_iss.py",
        description="AVA Agent C — Spike ISS golden commit-log runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # ── Manifest mode (AVA orchestrator — recommended) ────────────────────
    p.add_argument("--manifest", type=Path, default=None,
                   help="AVA manifest JSON path (orchestrator contract mode). "
                        "Reads binary/isa/spikebin from manifest; writes phases.iss.*.")

    # ── Legacy direct mode (debugging / standalone) ───────────────────────
    p.add_argument("--isa",  default=None,
                   help="ISA string, e.g. RV32IM (required in legacy mode)")
    p.add_argument("--elf",  default=None,
                   help="Input ELF binary path (required in legacy mode)")
    p.add_argument("--out",  default=None,
                   help="Output run directory (required in legacy mode)")
    p.add_argument("--spike", default=DEFAULT_SPIKE,
                   help=f"Spike binary name or full path (default: {DEFAULT_SPIKE})")
    p.add_argument("--pk", default=None,
                   help="Proxy kernel path (omit for bare-metal ELFs with tohost)")
    p.add_argument("--max-instrs", type=int, default=DEFAULT_MAX_INSTRS,
                   help=f"Max committed instructions (default: {DEFAULT_MAX_INSTRS:,})")
    p.add_argument("--max-records", type=int, default=None,
                   help="Cap written commit records (default: unlimited)")
    p.add_argument("--timeout", type=int, default=300,
                   help="Spike process timeout in seconds (default: 300)")
    p.add_argument("--extra-spike-args", default="",
                   help="Additional Spike flags (space-separated, quote the whole string)")
    p.add_argument("--force-format", choices=["A", "B"], default=None,
                   help="Force Spike log format: A=trace-only, B=commitlog")
    p.add_argument("--seed", type=int, default=None,
                   help="Seed stored in manifest (Spike itself is deterministic per ELF)")
    p.add_argument("--validate", action="store_true",
                   help="Validate first 200 commit records after parsing (schema v2.0.0)")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Debug logging")

    args = p.parse_args(argv)

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # ── Route: manifest mode vs legacy ────────────────────────────────────
    if args.manifest:
        return run_iss_manifest(args.manifest)

    # ── Legacy mode: require isa + elf + out ──────────────────────────────
    if not args.isa or not args.elf or not args.out:
        p.error("Legacy mode requires --isa, --elf, and --out "
                "(or use --manifest for orchestrator mode)")

    elf_path = Path(args.elf).resolve()
    if not elf_path.exists():
        log.error("ELF file not found: %s", elf_path)
        return EXIT_CONFIG

    run_dir = Path(args.out)
    run_dir.mkdir(parents=True, exist_ok=True)

    isa_upper  = args.isa.upper()
    xlen       = 64 if "64" in isa_upper else 32
    extra_args = args.extra_spike_args.split() if args.extra_spike_args.strip() else []

    # ── Probe Spike ────────────────────────────────────────────────────────
    log.info("Probing Spike at '%s' ...", args.spike)
    caps = probe_spike(args.spike)

    if not caps["found"]:
        log.error(
            "Spike not found: '%s'\n\n"
            "To install Spike:\n"
            "  git clone https://github.com/riscv-software-src/riscv-isa-sim\n"
            "  cd riscv-isa-sim && mkdir build && cd build\n"
            "  ../configure --prefix=/opt/riscv\n"
            "  make -j$(nproc) && make install\n"
            "  export PATH=/opt/riscv/bin:$PATH",
            args.spike,
        )
        return EXIT_INFRA

    log.info(
        "Spike found  | version: %s | --log-commits: %s | --enable-commitlog: %s",
        caps["version"], caps["has_log_commits"], caps["has_commit_log"],
    )

    # ── Apply --force-format overrides ────────────────────────────────────
    if args.force_format == "A":
        caps["has_log_commits"] = False
        caps["has_commit_log"]  = False
    elif args.force_format == "B":
        if not caps["has_log_commits"] and not caps["has_commit_log"]:
            log.warning("--force-format B: Spike may not support --log-commits; attempting anyway")
            caps["has_log_commits"] = True

    # ── Build Spike command ────────────────────────────────────────────────
    cmd, log_mode = build_spike_cmd(
        spike_bin=args.spike,
        elf_path=elf_path,
        isa=isa_upper,
        max_instrs=args.max_instrs,
        caps=caps,
        pk_path=args.pk,
        extra_args=extra_args,
    )

    # ── Initialise manifest ────────────────────────────────────────────────
    config = {
        "xlen":       xlen,
        "isa":        isa_upper,
        "priv":       ["M"],
        "seed":       args.seed,
        "max_instrs": args.max_instrs,
        "timeout_s":  args.timeout,
    }
    manifest = load_or_create_manifest(run_dir, config)
    manifest["inputs"]["elf"] = str(elf_path)
    manifest["status"] = "running"
    save_manifest(run_dir, manifest)

    # ── Run Spike ──────────────────────────────────────────────────────────
    iss_log = run_dir / "logs" / ISS_LOG_FILENAME
    t0 = time.monotonic()
    rc, spike_output = run_spike_process(cmd, iss_log, args.timeout)
    elapsed = time.monotonic() - t0

    log.info("Spike exited %d in %.2fs | output lines: %d",
             rc, elapsed, spike_output.count("\n"))

    if rc == -127:
        log.error("Spike binary could not be executed.")
        manifest["status"] = "error"
        save_manifest(run_dir, manifest)
        return EXIT_INFRA

    if not spike_output.strip():
        log.error(
            "Spike produced no output.\n"
            "Checklist:\n"
            "  1. Valid RISC-V binary? (file %s)\n"
            "  2. ISA matches? (--isa %s)\n"
            "  3. Has tohost symbol? (nm %s | grep tohost)\n"
            "  4. No tohost? Use --pk or add self-hosted exit.",
            elf_path, isa_upper, elf_path,
        )
        manifest["status"] = "error"
        save_manifest(run_dir, manifest)
        return EXIT_INFRA

    # ── Parse commit log (library call — no subprocess overhead) ───────────
    commitlog_path = run_dir / COMMITLOG_FILENAME

    fmt_hint: Optional[str]
    if args.force_format:
        fmt_hint = args.force_format
    elif log_mode in ("log_commits", "enable_cl"):
        fmt_hint = "B"
    else:
        fmt_hint = None

    log.info("Parsing Spike log (hint=%s) → %s", fmt_hint or "auto", commitlog_path)
    count = write_commitlog(spike_output, commitlog_path, fmt_hint=fmt_hint,
                            max_records=args.max_records)
    log.info("Commit records written: %d", count)

    if count == 0:
        log.error(
            "Parser produced zero records.\nFirst 30 lines of Spike output:\n%s",
            "\n".join(spike_output.splitlines()[:30]),
        )
        manifest["status"] = "error"
        save_manifest(run_dir, manifest)
        return EXIT_INFRA

    # ── Schema v2.0.0 validation ───────────────────────────────────────────
    if args.validate:
        log.info("Validating commit log (schema v2.0.0)...")
        errs = validate_commitlog(commitlog_path, sample_size=200)
        if errs:
            log.error("Validation FAILED:\n  %s", "\n  ".join(errs))
            manifest["status"] = "error"
            save_manifest(run_dir, manifest)
            return EXIT_INFRA
        log.info("Validation PASSED")

    # ── Finalise legacy manifest ───────────────────────────────────────────
    manifest["outputs"]["iss_commitlog"] = str(commitlog_path)
    manifest["outputs"]["iss_log"]       = str(iss_log)
    manifest["status"] = "pass"
    manifest["iss_stats"] = {
        "commit_count":  count,
        "elapsed_s":     round(elapsed, 3),
        "log_mode":      log_mode,
        "spike_exit":    rc,
        "spike_version": caps["version"],
    }
    save_manifest(run_dir, manifest)

    bar = "─" * 62
    print(f"\n{bar}")
    print(f"  AVA Agent C — Spike Golden Runner  [DONE]")
    print(bar)
    print(f"  ELF         : {elf_path.name}")
    print(f"  ISA         : {isa_upper}")
    print(f"  Schema      : v{__import__('spike_parser').SCHEMA_VERSION}")
    print(f"  Log mode    : {log_mode}")
    print(f"  Commits     : {count:,}")
    print(f"  Elapsed     : {elapsed:.2f}s")
    print(f"  Spike exit  : {rc}")
    print(f"  Output      : {commitlog_path}")
    print(f"  Manifest    : {run_dir / MANIFEST_FILENAME}")
    print(f"{bar}\n")

    return EXIT_PASS


if __name__ == "__main__":
    sys.exit(main())
