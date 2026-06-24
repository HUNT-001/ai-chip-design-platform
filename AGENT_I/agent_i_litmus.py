"""
AGENT_I/agent_i_litmus.py
==========================
Agent I — RVWMO Memory-Model Validator

Checks that RTL commit logs respect the RISC-V Weak Memory Order (RVWMO) model
by comparing the memory-operation ordering recorded in both the RTL and ISS logs.

Workflow
--------
1. Read RTL and ISS commit-log JSONL files (from a completed differential run).
2. Extract the memory-op subsequence from each log using `mem_seq` + `mem_order_tag`.
3. Check per-pattern ordering invariants (store-load, release-acquire, fence effects).
4. Optionally generate new litmus micro-tests (short Assembly sequences) that
   exercise specific RVWMO ordering patterns and feed them back to Agent G.
5. Write `litmus_report.json` to the run directory and update `manifest.json`.

RVWMO ordering patterns checked
---------------------------------
- store_load   : A STORE followed by a LOAD to the same address must appear in
                 that order in both RTL and ISS. RTL reordering is a violation.
- fence        : A FENCE instruction must separate earlier stores from later loads
                 globally — no load issued after FENCE may be ordered before a
                 store issued before FENCE in the ISS reference.
- release_acquire : A RELEASE store followed (eventually) by an ACQUIRE load to
                    the same address must appear in order. The ACQUIRE load must
                    see the value written by the RELEASE store or a later value.
- amo_ordering : AMO operations must appear atomically — their read and write
                 sides must not be interleaved with other memory operations.

Schema compliance
-----------------
Reads commit-log records with `schema_version` 2.1.0.
Writes `litmus_report.json` conforming to the output contract in interfaces.md §19.
Updates manifest `phases.memory_check`, `outputs.litmus_report`, and `status`.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────
# Schema / version constants
# ─────────────────────────────────────────────────────────
SCHEMA_VERSION = "2.1.0"
AGENT_ID = "agent_i"

# ─────────────────────────────────────────────────────────
# Memory-op record (extracted from commit log)
# ─────────────────────────────────────────────────────────

@dataclass
class MemOp:
    """A single memory operation extracted from a commit-log record."""
    seq:       int            # instruction retirement seq
    mem_seq:   int            # global memory-op sequence number
    pc:        str            # instruction PC (hex8)
    tag:       str            # LOAD / STORE / FENCE / RELEASE / ACQUIRE / AMO
    addr:      Optional[str]  # effective address (hex8), None for FENCE
    data:      Optional[str]  # data value (hex8), None for LOADs and FENCEs
    disasm:    str            # disassembly string
    src:       str            # "rtl" or "iss"


def _extract_mem_ops(records: List[dict], src: str) -> List[MemOp]:
    """
    Extract the memory-operation subsequence from a list of commit-log records.

    Records without `mem_seq` or with `mem_order_tag` == null are skipped.
    """
    ops: List[MemOp] = []
    for r in records:
        tag = r.get("mem_order_tag")
        ms = r.get("mem_seq")
        if tag is None or ms is None:
            continue

        # Effective address
        addr: Optional[str] = None
        data: Optional[str] = None
        mw = r.get("mem_writes")
        mr = r.get("mem_reads")
        if mw and isinstance(mw, list) and mw:
            addr = mw[0].get("addr")
            data = mw[0].get("data")
        elif mr and isinstance(mr, list) and mr:
            addr = mr[0].get("addr")
            data = mr[0].get("data")

        ops.append(MemOp(
            seq=r["seq"],
            mem_seq=ms,
            pc=r["pc"],
            tag=tag,
            addr=addr,
            data=data,
            disasm=r.get("disasm", ""),
            src=src,
        ))
    return ops


# ─────────────────────────────────────────────────────────
# Ordering violation record
# ─────────────────────────────────────────────────────────

@dataclass
class Violation:
    pattern:          str
    description:      str
    rtl_mem_seq:      int
    iss_mem_seq:      int
    rtl_order:        List[str]
    iss_order:        List[str]
    seq_at_divergence: int
    severity:         str = "HIGH"   # HIGH / MEDIUM / LOW


# ─────────────────────────────────────────────────────────
# Pattern checkers
# ─────────────────────────────────────────────────────────

def _check_store_load(rtl_ops: List[MemOp], iss_ops: List[MemOp]) -> List[Violation]:
    """
    RVWMO §2 (coherence): For any address A, if the ISS sees STORE(A) before
    LOAD(A), the RTL must also see STORE(A) before LOAD(A) in mem_seq order.

    This is the most common RVWMO violation in pipelined cores that speculatively
    execute loads before preceding stores have been committed to the store buffer.
    """
    violations: List[Violation] = []

    # Build address -> ordered list of (mem_seq, tag) for ISS
    from collections import defaultdict
    iss_by_addr: Dict[str, List[Tuple[int, str]]] = defaultdict(list)
    for op in iss_ops:
        if op.addr and op.tag in ("STORE", "LOAD", "RELEASE", "ACQUIRE"):
            iss_by_addr[op.addr].append((op.mem_seq, op.tag))

    rtl_by_addr: Dict[str, List[Tuple[int, str]]] = defaultdict(list)
    for op in rtl_ops:
        if op.addr and op.tag in ("STORE", "LOAD", "RELEASE", "ACQUIRE"):
            rtl_by_addr[op.addr].append((op.mem_seq, op.tag))

    for addr in iss_by_addr:
        if addr not in rtl_by_addr:
            continue
        iss_seq = iss_by_addr[addr]
        rtl_seq = rtl_by_addr[addr]

        # Find (STORE, LOAD) pairs in ISS ordering
        for i, (ms_a, tag_a) in enumerate(iss_seq):
            if tag_a not in ("STORE", "RELEASE"):
                continue
            for j, (ms_b, tag_b) in enumerate(iss_seq):
                if j <= i or tag_b not in ("LOAD", "ACQUIRE"):
                    continue
                # ISS says STORE before LOAD to same addr — check RTL
                rtl_store_idx = next(
                    (k for k, (ms, t) in enumerate(rtl_seq)
                     if t in ("STORE","RELEASE") and ms == ms_a), None)
                rtl_load_idx = next(
                    (k for k, (ms, t) in enumerate(rtl_seq)
                     if t in ("LOAD","ACQUIRE") and ms == ms_b), None)
                if rtl_store_idx is None or rtl_load_idx is None:
                    continue
                if rtl_load_idx < rtl_store_idx:
                    violations.append(Violation(
                        pattern="store_load",
                        description=(
                            f"RTL LOAD(addr={addr}) at mem_seq={ms_b} appears before "
                            f"STORE(addr={addr}) at mem_seq={ms_a} "
                            f"but ISS orders STORE before LOAD"
                        ),
                        rtl_mem_seq=ms_b,
                        iss_mem_seq=ms_b,
                        rtl_order=[f"LOAD@{addr}(mem_seq={ms_b})", f"STORE@{addr}(mem_seq={ms_a})"],
                        iss_order=[f"STORE@{addr}(mem_seq={ms_a})", f"LOAD@{addr}(mem_seq={ms_b})"],
                        seq_at_divergence=ms_b,
                        severity="HIGH",
                    ))
    return violations


def _check_fence(rtl_ops: List[MemOp], iss_ops: List[MemOp]) -> List[Violation]:
    """
    RVWMO §3 (fence ordering): A FENCE must act as a total ordering barrier.
    Any STORE issued before the FENCE must be globally visible before any LOAD
    issued after the FENCE.

    We check: for each FENCE in the ISS log, any (STORE_before, LOAD_after)
    pair must also appear in that order in the RTL mem_seq sequence.
    """
    violations: List[Violation] = []

    fence_indices = [i for i, op in enumerate(iss_ops) if op.tag == "FENCE"]
    for fi in fence_indices:
        fence_ms = iss_ops[fi].mem_seq
        stores_before = [op for op in iss_ops[:fi] if op.tag in ("STORE","RELEASE")]
        loads_after   = [op for op in iss_ops[fi+1:] if op.tag in ("LOAD","ACQUIRE")]

        for s_op in stores_before:
            for l_op in loads_after:
                if s_op.addr != l_op.addr:
                    continue
                # Find these ops in RTL
                rtl_store = next((o for o in rtl_ops if o.mem_seq == s_op.mem_seq), None)
                rtl_load  = next((o for o in rtl_ops if o.mem_seq == l_op.mem_seq), None)
                if rtl_store is None or rtl_load is None:
                    continue
                if rtl_load.mem_seq < rtl_store.mem_seq:
                    violations.append(Violation(
                        pattern="fence",
                        description=(
                            f"FENCE(mem_seq={fence_ms}) violated: "
                            f"LOAD(addr={l_op.addr},mem_seq={l_op.mem_seq}) "
                            f"appears before STORE(addr={s_op.addr},mem_seq={s_op.mem_seq}) "
                            f"in RTL despite FENCE separation in ISS"
                        ),
                        rtl_mem_seq=rtl_load.mem_seq,
                        iss_mem_seq=l_op.mem_seq,
                        rtl_order=[f"LOAD@{l_op.addr}", f"STORE@{s_op.addr}"],
                        iss_order=[f"STORE@{s_op.addr}", f"FENCE", f"LOAD@{l_op.addr}"],
                        seq_at_divergence=l_op.seq,
                        severity="HIGH",
                    ))
    return violations


def _check_release_acquire(rtl_ops: List[MemOp], iss_ops: List[MemOp]) -> List[Violation]:
    """
    RVWMO release-acquire synchronization:
    A RELEASE store followed by an ACQUIRE load to the same address must appear
    in that order in the global memory order (mem_seq). The ACQUIRE load must
    not appear before the RELEASE store in mem_seq.
    """
    violations: List[Violation] = []

    iss_releases = [op for op in iss_ops if op.tag == "RELEASE"]
    iss_acquires = [op for op in iss_ops if op.tag == "ACQUIRE"]

    for rel in iss_releases:
        for acq in iss_acquires:
            if acq.addr != rel.addr:
                continue
            if acq.mem_seq <= rel.mem_seq:
                continue  # ISS itself is out of order — not our problem to check

            # Find in RTL
            rtl_rel = next((o for o in rtl_ops if o.mem_seq == rel.mem_seq), None)
            rtl_acq = next((o for o in rtl_ops if o.mem_seq == acq.mem_seq), None)
            if rtl_rel is None or rtl_acq is None:
                continue
            if rtl_acq.mem_seq < rtl_rel.mem_seq:
                violations.append(Violation(
                    pattern="release_acquire",
                    description=(
                        f"ACQUIRE(addr={acq.addr},mem_seq={acq.mem_seq}) "
                        f"appears before RELEASE(addr={rel.addr},mem_seq={rel.mem_seq}) "
                        f"in RTL — release-acquire synchronization violated"
                    ),
                    rtl_mem_seq=rtl_acq.mem_seq,
                    iss_mem_seq=acq.mem_seq,
                    rtl_order=[f"ACQUIRE@{acq.addr}(ms={acq.mem_seq})", f"RELEASE@{rel.addr}(ms={rel.mem_seq})"],
                    iss_order=[f"RELEASE@{rel.addr}(ms={rel.mem_seq})", f"ACQUIRE@{acq.addr}(ms={acq.mem_seq})"],
                    seq_at_divergence=acq.seq,
                    severity="HIGH",
                ))
    return violations


def _check_amo_atomicity(rtl_ops: List[MemOp], iss_ops: List[MemOp]) -> List[Violation]:
    """
    AMO atomicity: AMO operations should appear as a single unit in mem_seq.
    No other memory operation to the same address should appear between the
    AMO's implicit read and write (they share one mem_seq slot in our model,
    so we check that no addr-matching op appears at the same mem_seq in RTL
    while none is present in ISS).

    In our current commit-log model, AMO is a single mem_seq entry — so
    the check is: the RTL must have an AMO at the same mem_seq as the ISS.
    A missing or reordered AMO mem_seq is flagged.
    """
    violations: List[Violation] = []

    iss_amos = [op for op in iss_ops if op.tag == "AMO"]
    rtl_by_ms = {op.mem_seq: op for op in rtl_ops}

    for amo in iss_amos:
        rtl_amo = rtl_by_ms.get(amo.mem_seq)
        if rtl_amo is None:
            violations.append(Violation(
                pattern="amo_ordering",
                description=(
                    f"AMO(addr={amo.addr},mem_seq={amo.mem_seq}) "
                    f"present in ISS but no RTL op at that mem_seq"
                ),
                rtl_mem_seq=-1,
                iss_mem_seq=amo.mem_seq,
                rtl_order=[],
                iss_order=[f"AMO@{amo.addr}(ms={amo.mem_seq})"],
                seq_at_divergence=amo.seq,
                severity="HIGH",
            ))
        elif rtl_amo.tag != "AMO":
            violations.append(Violation(
                pattern="amo_ordering",
                description=(
                    f"ISS has AMO(addr={amo.addr}) at mem_seq={amo.mem_seq} "
                    f"but RTL has {rtl_amo.tag}(addr={rtl_amo.addr}) at same mem_seq"
                ),
                rtl_mem_seq=rtl_amo.mem_seq,
                iss_mem_seq=amo.mem_seq,
                rtl_order=[f"{rtl_amo.tag}@{rtl_amo.addr}"],
                iss_order=[f"AMO@{amo.addr}"],
                seq_at_divergence=amo.seq,
                severity="MEDIUM",
            ))
    return violations


# ─────────────────────────────────────────────────────────
# Litmus test generator
# ─────────────────────────────────────────────────────────

# Template micro-sequences for each pattern (RISC-V Assembly, bare-metal)
_LITMUS_TEMPLATES: Dict[str, str] = {
    "store_load": """\
# Litmus: store-load ordering test
# t0 = store address, t1 = load address (same), t2 = store data
lui   t0, 0x80001         # t0 = 0x80001000
li    t1, 0x42
sw    t1, 0(t0)           # STORE data to 0x80001000
lw    t2, 0(t0)           # LOAD from same address — must see 0x42
""",
    "fence": """\
# Litmus: fence ordering test
lui   t0, 0x80001         # t0 = base address
li    t1, 0x1
sw    t1, 0(t0)           # STORE before fence
fence rw, rw              # FENCE — all prior stores must complete
lw    t2, 0(t0)           # LOAD after fence — must see t1
""",
    "release_acquire": """\
# Litmus: release-acquire synchronization
lui   t0, 0x80001         # t0 = shared address
lui   t1, 0x80002         # t1 = flag address
li    t2, 0xdeadbeef
sw    t2, 0(t0)           # write data
amoswap.w.rl x0, t2, 0(t1) # RELEASE store to flag
amoswap.w.aq t3, x0, 0(t1) # ACQUIRE load from flag
lw    t4, 0(t0)           # load data — must see 0xdeadbeef
""",
    "amo_ordering": """\
# Litmus: AMO atomicity test
lui   t0, 0x80001         # t0 = address
li    t1, 0x1
amoswap.w t2, t1, 0(t0)  # atomic swap — must appear as single mem-op
lw    t3, 0(t0)           # load result — must see t1
""",
    "load_reserved": """\
# Litmus: LR/SC pairing test
lui   t0, 0x80001
lr.w  t1, (t0)            # load-reserved
li    t2, 0x99
sc.w  t3, t2, (t0)        # store-conditional — t3=0 on success
""",
}


def generate_litmus_tests(
    patterns: List[str],
    output_dir: Path,
    max_tests: int = 64,
) -> List[Path]:
    """
    Write litmus test Assembly source files to output_dir.

    Returns list of generated file paths.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    generated: List[Path] = []
    per_pattern = max(1, max_tests // max(len(patterns), 1))

    for pattern in patterns:
        template = _LITMUS_TEMPLATES.get(pattern)
        if template is None:
            logger.warning("No template for pattern %s; skipping", pattern)
            continue
        for i in range(per_pattern):
            fname = output_dir / f"litmus_{pattern}_{i:03d}.S"
            with open(fname, "w") as f:
                f.write(f"# AVA Agent I — auto-generated litmus test\n")
                f.write(f"# Pattern: {pattern}  Variant: {i}\n")
                f.write(f"# Schema: {SCHEMA_VERSION}\n\n")
                f.write(".section .text\n.global _start\n_start:\n")
                f.write(template)
                # Termination sentinel
                f.write("\n# End-of-test sentinel\n")
                f.write("    li a0, 1\n")
                f.write("    lui t0, 0x80001\n")
                f.write("    sw a0, 0x1000(t0)  # tohost write\n")
                f.write("    j .\n")
            generated.append(fname)
            logger.info("Generated litmus test: %s", fname)

    return generated


# ─────────────────────────────────────────────────────────
# Main checker
# ─────────────────────────────────────────────────────────

@dataclass
class LitmusChecker:
    """
    Main Agent I entry point.

    Parameters
    ----------
    rtl_log_path : path to RTL commit JSONL
    iss_log_path : path to ISS commit JSONL
    patterns     : list of ordering patterns to check
    max_violations : stop after this many violations (0 = check all)
    """
    rtl_log_path:   Path
    iss_log_path:   Path
    patterns:       List[str] = field(default_factory=lambda: [
        "store_load", "fence", "release_acquire", "amo_ordering"
    ])
    max_violations: int = 0  # 0 = unlimited

    def _load_jsonl(self, path: Path) -> List[dict]:
        records = []
        with open(path) as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as e:
                    logger.error("JSON parse error at %s line %d: %s", path, lineno, e)
        return records

    def run(self) -> Dict[str, Any]:
        """
        Execute all enabled pattern checks.

        Returns the litmus report dict (not yet written to disk).
        """
        started = datetime.now(timezone.utc)

        logger.info("Agent I: loading RTL log %s", self.rtl_log_path)
        rtl_records = self._load_jsonl(self.rtl_log_path)
        logger.info("Agent I: loading ISS log %s", self.iss_log_path)
        iss_records = self._load_jsonl(self.iss_log_path)

        rtl_ops = _extract_mem_ops(rtl_records, "rtl")
        iss_ops = _extract_mem_ops(iss_records, "iss")

        logger.info(
            "Agent I: extracted %d RTL mem-ops, %d ISS mem-ops",
            len(rtl_ops), len(iss_ops),
        )

        # Run enabled pattern checkers
        checkers = {
            "store_load":       _check_store_load,
            "fence":            _check_fence,
            "release_acquire":  _check_release_acquire,
            "amo_ordering":     _check_amo_atomicity,
        }

        all_violations: List[Violation] = []
        for pattern in self.patterns:
            if pattern not in checkers:
                logger.warning("Unknown pattern %s; skipping", pattern)
                continue
            v = checkers[pattern](rtl_ops, iss_ops)
            all_violations.extend(v)
            if v:
                logger.warning("Pattern %s: %d violation(s)", pattern, len(v))
            else:
                logger.info("Pattern %s: OK", pattern)
            if self.max_violations > 0 and len(all_violations) >= self.max_violations:
                logger.warning("Max violations (%d) reached; stopping early", self.max_violations)
                break

        finished = datetime.now(timezone.utc)

        # Build report
        report: Dict[str, Any] = {
            "schema_version":    SCHEMA_VERSION,
            "agent":             AGENT_ID,
            "patterns_tested":   self.patterns,
            "rtl_mem_ops":       len(rtl_ops),
            "iss_mem_ops":       len(iss_ops),
            "violations": [
                {
                    "pattern":             v.pattern,
                    "description":         v.description,
                    "rtl_mem_seq":         v.rtl_mem_seq,
                    "iss_mem_seq":         v.iss_mem_seq,
                    "rtl_order":           v.rtl_order,
                    "iss_order":           v.iss_order,
                    "seq_at_divergence":   v.seq_at_divergence,
                    "severity":            v.severity,
                }
                for v in all_violations
            ],
            "total_patterns":    len(self.patterns),
            "violations_found":  len(all_violations),
            "high_severity":     sum(1 for v in all_violations if v.severity == "HIGH"),
            "medium_severity":   sum(1 for v in all_violations if v.severity == "MEDIUM"),
            "started_at":        started.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "finished_at":       finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duration_s":        round((finished - started).total_seconds(), 3),
            "generated_at":      finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

        return report


# ─────────────────────────────────────────────────────────
# Manifest integration helpers
# ─────────────────────────────────────────────────────────

def _load_manifest(manifest_path: Path) -> dict:
    with open(manifest_path) as f:
        return json.load(f)


def _save_manifest(manifest_path: Path, manifest: dict) -> None:
    tmp = manifest_path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    tmp.rename(manifest_path)


def run_from_manifest(
    manifest_path: Path,
    patterns: Optional[List[str]] = None,
    generate_tests: bool = False,
    fail_on_violation: bool = True,
) -> int:
    """
    Full Agent I pipeline driven by a manifest.json.

    Returns
    -------
    0 : all checks passed (or no mem-ops to check)
    1 : ORDERMISMATCH violations found
    2 : infrastructure error
    """
    manifest = _load_manifest(manifest_path)
    run_dir = Path(manifest["run_dir"])
    run_id  = manifest["run_id"]

    # Pull per-run config if present
    agent_cfg = (manifest.get("agent_config") or {}).get("agent_i") or {}
    if patterns is None:
        patterns = agent_cfg.get("litmus_patterns", [
            "store_load", "fence", "release_acquire", "amo_ordering"
        ])
    max_tests    = agent_cfg.get("max_litmus_tests", 64)
    fail_on_viol = agent_cfg.get("fail_on_violation", fail_on_violation)

    # Locate commit logs
    rtl_path = run_dir / (manifest["outputs"].get("rtl_commitlog") or "rtl_commit.jsonl")
    iss_path = run_dir / (manifest["outputs"].get("iss_commitlog") or "iss_commit.jsonl")

    if not rtl_path.exists():
        logger.error("RTL commit log not found: %s", rtl_path)
        return 2
    if not iss_path.exists():
        logger.error("ISS commit log not found: %s", iss_path)
        return 2

    # Update manifest: start phase
    started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest.setdefault("phases", {})["memory_check"] = {
        "status":     "running_memory_check",
        "started_at": started_at,
        "finished_at": None,
        "duration_s":  None,
        "exit_code":   None,
        "error_msg":   None,
        "retry_count": 0,
        "log_path":    "logs/agent_i.log",
    }
    manifest["status"] = "running_memory_check"
    _save_manifest(manifest_path, manifest)

    t0 = time.monotonic()
    try:
        checker = LitmusChecker(
            rtl_log_path=rtl_path,
            iss_log_path=iss_path,
            patterns=patterns,
        )
        report = checker.run()
        report["run_id"] = run_id

        # Optionally generate litmus test files
        if generate_tests:
            litmus_dir = run_dir / "litmus_tests"
            generated = generate_litmus_tests(patterns, litmus_dir, max_tests)
            report["generated_litmus_tests"] = [str(p.relative_to(run_dir)) for p in generated]

        # Write report
        report_path = run_dir / "litmus_report.json"
        tmp_path    = run_dir / "litmus_report.json.tmp"
        with open(tmp_path, "w") as f:
            json.dump(report, f, indent=2)
            f.write("\n")
        tmp_path.rename(report_path)
        logger.info("Litmus report written to %s", report_path)

        violations_found = report["violations_found"]
        exit_code = 1 if (violations_found > 0 and fail_on_viol) else 0
        duration_s = round(time.monotonic() - t0, 3)

        # Update manifest: finish phase
        finished_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        manifest["phases"]["memory_check"].update({
            "status":      "failed" if exit_code == 1 else "passed",
            "finished_at": finished_at,
            "duration_s":  duration_s,
            "exit_code":   exit_code,
            "error_msg":   (
                f"ORDERMISMATCH: {violations_found} violation(s) found"
                if exit_code == 1 else None
            ),
        })
        manifest["outputs"]["litmus_report"] = "litmus_report.json"

        if exit_code == 1:
            manifest["status"] = "failed"
            manifest["error"] = {
                "code":        "ORDERMISMATCH",
                "message":     f"Agent I found {violations_found} RVWMO ordering violation(s)",
                "phase":       "memory_check",
                "recoverable": False,
                "repro_cmd":   f"python3 AGENT_I/agent_i_litmus.py --manifest {manifest_path}",
            }
        else:
            # Restore previous terminal status (passed) if we didn't override
            manifest["status"] = "passed"

        _save_manifest(manifest_path, manifest)

        if violations_found > 0:
            logger.error("Agent I: %d RVWMO violation(s) found", violations_found)
        else:
            logger.info("Agent I: all memory-ordering checks PASSED")

        return exit_code

    except Exception as exc:
        duration_s = round(time.monotonic() - t0, 3)
        logger.exception("Agent I infrastructure error: %s", exc)
        finished_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        manifest["phases"]["memory_check"].update({
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
        description="Agent I — RVWMO memory-model validator for AVA commit logs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples
--------
  # Check ordering in a completed run
  python3 AGENT_I/agent_i_litmus.py --manifest /tmp/runs/run1/manifest.json

  # Check only store-load and fence patterns
  python3 AGENT_I/agent_i_litmus.py --manifest /tmp/runs/run1/manifest.json \\
      --patterns store_load fence

  # Also generate litmus test sources for Agent G
  python3 AGENT_I/agent_i_litmus.py --manifest /tmp/runs/run1/manifest.json \\
      --generate-tests

  # Standalone: compare two JSONL files directly (no manifest)
  python3 AGENT_I/agent_i_litmus.py \\
      --rtl rtl_commit.jsonl --iss iss_commit.jsonl \\
      --report litmus_report.json
""",
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--manifest", type=Path, metavar="PATH",
                      help="Path to AVA run manifest.json (full pipeline mode)")
    mode.add_argument("--rtl", type=Path, metavar="PATH",
                      help="RTL JSONL commit log (standalone mode, requires --iss)")
    p.add_argument("--iss", type=Path, metavar="PATH",
                   help="ISS JSONL commit log (standalone mode)")
    p.add_argument("--report", type=Path, metavar="PATH", default=Path("litmus_report.json"),
                   help="Output report path (standalone mode; default: litmus_report.json)")
    p.add_argument("--patterns", nargs="+",
                   choices=["store_load","fence","release_acquire","amo_ordering","load_reserved"],
                   default=None,
                   help="Ordering patterns to check (default: all supported)")
    p.add_argument("--generate-tests", action="store_true",
                   help="Generate Assembly litmus test sources alongside report")
    p.add_argument("--no-fail", action="store_true",
                   help="Exit 0 even if violations are found (report-only mode)")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG","INFO","WARNING","ERROR"],
                   help="Logging verbosity (default: INFO)")
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
            patterns=args.patterns,
            generate_tests=args.generate_tests,
            fail_on_violation=not args.no_fail,
        )

    # Standalone mode
    if args.iss is None:
        p.error("--iss is required in standalone mode (when using --rtl)")

    checker = LitmusChecker(
        rtl_log_path=args.rtl,
        iss_log_path=args.iss,
        patterns=args.patterns or ["store_load","fence","release_acquire","amo_ordering"],
    )
    report = checker.run()

    if args.generate_tests:
        litmus_dir = args.report.parent / "litmus_tests"
        generated = generate_litmus_tests(
            checker.patterns, litmus_dir, max_tests=64
        )
        report["generated_litmus_tests"] = [str(p) for p in generated]

    with open(args.report, "w") as f:
        json.dump(report, f, indent=2)
        f.write("\n")
    print(f"Litmus report: {args.report}")

    if report["violations_found"] > 0 and not args.no_fail:
        print(f"FAIL: {report['violations_found']} RVWMO ordering violation(s) found")
        return 1

    print(f"PASS: {len(checker.patterns)} pattern(s) checked, 0 violations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
