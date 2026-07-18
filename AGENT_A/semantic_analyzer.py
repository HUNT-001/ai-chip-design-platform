"""
AGENT_A.semantic_analyzer — Semantic Analysis (Phase 1)
========================================================

The platform's Phase-1 agent, given a concrete module here (previously the
`SemanticMap` logic lived only inside the orchestrator). Two jobs:

1. **Schema validation** — check that commit-log records and the run manifest
   conform to the v2.1.0 contract *before* any downstream agent consumes them,
   so a malformed trace fails fast with a precise error instead of silently
   corrupting a checker's golden comparison.
2. **DUT extraction** — parse the RTL to recover the design-under-test module
   name, its port list (direction + width), and the clock/reset pins — the
   "semantic map" the testbench-generation and simulation phases need.

Stdlib-only (a lightweight, dependency-free validator — no `jsonschema`),
schema-v2.1.0 report, graceful degradation throughout.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

log = logging.getLogger("AGENT_A.semantic")

SCHEMA_VERSION = "2.1.0"
AGENT_NAME = "semantic_analyzer"
_HEX = re.compile(r"^0x[0-9a-fA-F]+$")
_MANIFEST_STATUS = {"running", "completed", "fail", "pending"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# Schema validation (commit-log record + run manifest, v2.1.0)
# ─────────────────────────────────────────────────────────────────────────────
def _is_hex(v: Any) -> bool:
    return isinstance(v, str) and bool(_HEX.match(v))


def validate_record(rec: Any, idx: int = 0) -> List[str]:
    """Return a list of human-readable errors for one commit-log record."""
    errs: List[str] = []
    if not isinstance(rec, dict):
        return [f"record[{idx}]: not an object"]
    for f in ("schema_version", "seq", "pc", "disasm"):
        if f not in rec:
            errs.append(f"record[{idx}]: missing required field '{f}'")
    if "seq" in rec and not isinstance(rec["seq"], int):
        errs.append(f"record[{idx}]: 'seq' must be an integer")
    if "pc" in rec and not _is_hex(rec["pc"]):
        errs.append(f"record[{idx}]: 'pc' must be a 0x-hex string")
    if "disasm" in rec and not isinstance(rec["disasm"], str):
        errs.append(f"record[{idx}]: 'disasm' must be a string")
    for f in ("regs", "csrs"):
        if f in rec and not isinstance(rec[f], dict):
            errs.append(f"record[{idx}]: '{f}' must be an object")
    for f in ("mem_reads", "mem_writes"):
        if f in rec:
            if not isinstance(rec[f], list):
                errs.append(f"record[{idx}]: '{f}' must be an array")
            else:
                for j, a in enumerate(rec[f]):
                    if not isinstance(a, dict) or "addr" not in a:
                        errs.append(f"record[{idx}].{f}[{j}]: needs an 'addr'")
    if "trap" in rec and rec["trap"] is not None:
        t = rec["trap"]
        if not isinstance(t, dict) or "cause" not in t:
            errs.append(f"record[{idx}]: 'trap' must be an object with 'cause'")
    return errs


def validate_manifest(m: Any) -> List[str]:
    errs: List[str] = []
    if not isinstance(m, dict):
        return ["manifest: not an object"]
    for f in ("schema_version", "run_id", "run_dir", "status"):
        if f not in m:
            errs.append(f"manifest: missing required field '{f}'")
    if "status" in m and m["status"] not in _MANIFEST_STATUS:
        errs.append(f"manifest: 'status' {m['status']!r} not in {sorted(_MANIFEST_STATUS)}")
    if "outputs" in m and not isinstance(m["outputs"], dict):
        errs.append("manifest: 'outputs' must be an object")
    if m.get("schema_version") not in (None, SCHEMA_VERSION):
        errs.append(f"manifest: schema_version {m.get('schema_version')!r} != {SCHEMA_VERSION}")
    return errs


# ─────────────────────────────────────────────────────────────────────────────
# DUT extraction from Verilog / SystemVerilog
# ─────────────────────────────────────────────────────────────────────────────
_MODULE = re.compile(r"\bmodule\s+([A-Za-z_]\w*)", re.MULTILINE)
_DIR = re.compile(r"\b(input|output|inout)\b")
_TYPE = re.compile(r"\b(wire|reg|logic|signed|var)\b")
_CLK = re.compile(r"\b(clk|clock|clk_i|aclk)\b", re.IGNORECASE)
_RST = re.compile(r"\b(rst|reset|rst_n|resetn|areset|rst_ni)\b", re.IGNORECASE)


def extract_dut(rtl_text: str) -> Dict[str, Any]:
    """Recover module name, ports (dir/width), and clock/reset pins from RTL."""
    text = re.sub(r"//[^\n]*", "", rtl_text or "")          # strip line comments
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)  # strip block comments
    mod = _MODULE.search(text)
    ports: List[Dict[str, Any]] = []
    dirs = list(_DIR.finditer(text))
    for i, dm in enumerate(dirs):
        direction = dm.group(1)
        end = dirs[i + 1].start() if i + 1 < len(dirs) else len(text)
        seg = text[dm.end():end]
        seg = re.split(r"[;)]", seg, 1)[0]                  # stop at port-list boundary
        width_m = re.search(r"\[[^\]]+\]", seg)
        width = width_m.group(0) if width_m else "[0:0]"
        seg = re.sub(r"\[[^\]]+\]", "", seg)                # drop widths
        seg = _TYPE.sub("", seg)                            # drop wire/reg/logic
        for name in re.findall(r"[A-Za-z_]\w*", seg):
            ports.append({"name": name, "dir": direction, "width": width})
    port_names = " ".join(p["name"] for p in ports)
    return {
        "module": mod.group(1) if mod else None,
        "ports": ports,
        "port_count": len(ports),
        "inputs": sum(1 for p in ports if p["dir"] == "input"),
        "outputs": sum(1 for p in ports if p["dir"] == "output"),
        "clock": bool(_CLK.search(port_names)),
        "reset": bool(_RST.search(port_names)),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Analyzer
# ─────────────────────────────────────────────────────────────────────────────
class SemanticAnalyzer:
    def __init__(self, commit_log: Optional[Sequence[Dict[str, Any]]] = None,
                 manifest: Optional[Dict[str, Any]] = None,
                 rtl_text: Optional[str] = None,
                 max_record_errors: int = 100):
        self.commit_log = list(commit_log or [])
        self.manifest = manifest
        self.rtl_text = rtl_text
        self.max_record_errors = max_record_errors

    def analyze(self) -> Dict[str, Any]:
        started = _now()
        rec_errors: List[str] = []
        for i, rec in enumerate(self.commit_log):
            rec_errors.extend(validate_record(rec, i))
            if len(rec_errors) >= self.max_record_errors:
                rec_errors.append("… (truncated)")
                break
        man_errors = validate_manifest(self.manifest) if self.manifest is not None else []
        dut = extract_dut(self.rtl_text) if self.rtl_text else None

        total = len(rec_errors) + len(man_errors)
        valid = total == 0
        return {
            "schema_version": SCHEMA_VERSION,
            "agent": AGENT_NAME,
            "started_at": started,
            "finished_at": _now(),
            "records_checked": len(self.commit_log),
            "schema_valid": valid,
            "pass": valid,
            "total_errors": total,
            "record_errors": rec_errors[:self.max_record_errors],
            "manifest_errors": man_errors,
            "dut": dut,
            "band": "CLEAN" if valid else "CRITICAL",
        }


# ─────────────────────────────────────────────────────────────────────────────
# Manifest entry point
# ─────────────────────────────────────────────────────────────────────────────
def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    out.append({"__parse_error__": line[:40]})
    except OSError:
        pass
    return out


def run_from_manifest(manifest_path: str) -> int:
    """Validate the run's commit log + manifest and (if present) the DUT RTL;
    write ``semantic_report.json``. Returns 0 if valid, 1 on schema errors."""
    mp = Path(manifest_path)
    try:
        manifest = json.loads(mp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("semantic_analyzer: cannot read manifest: %s", exc)
        return 0
    run_dir = Path(manifest.get("run_dir", mp.parent))
    outputs = manifest.get("outputs", {})
    log_recs = _load_jsonl(run_dir / outputs.get("rtl_commit_log", "rtl_commit.jsonl"))
    rtl_text = None
    rtl_name = manifest.get("rtl") or outputs.get("rtl")
    if rtl_name:
        p = Path(rtl_name) if Path(rtl_name).is_absolute() else run_dir / rtl_name
        try:
            rtl_text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            rtl_text = None
    rep = SemanticAnalyzer(log_recs, manifest, rtl_text).analyze()
    try:
        (run_dir / "semantic_report.json").write_text(
            json.dumps(rep, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("semantic_analyzer: cannot write report: %s", exc)
    return 0 if rep["pass"] else 1


if __name__ == "__main__":  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="AVA Phase-1 semantic analyzer")
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args()
    raise SystemExit(run_from_manifest(args.manifest))
