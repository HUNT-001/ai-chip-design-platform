"""
AGENT_H.coverage_collector — Functional Coverage Collector (T42)
=================================================================

Computes **functional coverage** from the commit log and emits the exact
``coverage_summary.json`` that `AGENT_H.self_evolving_engine` consumes. This is
the piece that *closes the self-evolving loop*: the RL coverage-closure planner
finally gets real coverage input (covered bins, open holes, per-bin importance)
instead of a hypothetical snapshot.

Coverage model
--------------
Bins are ``"category:key"`` labels — the same shape the self-evolving engine's
`constraint_for` already understands. Categories with a **finite, known
universe** produce real *holes* (uncovered targets); open-ended categories are
reported as observed-only telemetry.

| Category | Bin | Universe (→ holes) | Weight |
|---|---|---|---|
| register write | `reg:x{1..31}` | 31 | 1 |
| value class | `valclass:{zero,one,neg,pos_small,pos_large,all_ones}` | 6 | 1 |
| branch direction | `branch:{taken,not_taken}` | 2 | 2 |
| privilege mode | `priv:{M,S,U}` | 3 | 3 |
| instruction | `instr:{mnem}` | only if a model lists them | 1 |
| CSR / trap / vtype | `csr:… / trap:… / vtype:…` | observed-only telemetry | — |

A `model` may extend the universe (e.g. an expected-instruction list) and
override weights. Higher-weight holes are the ones the self-evolving scheduler
prioritises, so privilege/branch gaps are chased before ordinary register gaps.

Output
------
`run_from_manifest` writes both a human ``coverage_report.json`` and a
machine ``coverage_summary.json`` (`covered_bins`, `total_bins`, `holes`,
`weights`) — the latter is read verbatim by the self-evolving planner.

Stdlib-only, schema-v2.1.0, graceful degradation throughout.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

log = logging.getLogger("AGENT_H.coverage")

SCHEMA_VERSION = "2.1.0"
AGENT_NAME = "coverage_collector"

_VALUE_CLASSES = ["zero", "one", "neg", "pos_small", "pos_large", "all_ones"]
_COND_BRANCH = {"beq", "bne", "blt", "bge", "bltu", "bgeu",
                "beqz", "bnez", "c.beqz", "c.bnez"}
# Instructions whose result value-class is worth crossing (corner-case hunting).
_DEFAULT_CROSS = ["add", "sub", "addi", "and", "or", "xor",
                  "sll", "srl", "sra", "slt", "sltu", "mul"]
_CATEGORY_WEIGHT = {"reg": 1.0, "valclass": 1.0, "branch": 2.0,
                    "priv": 3.0, "instr": 1.0, "cross": 2.0}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_int(v: Any) -> Optional[int]:
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        try:
            return int(v, 16) if v.lower().startswith("0x") else int(v)
        except ValueError:
            return None
    return None


def classify_value(v: int, width: int = 32) -> str:
    """Bucket a register value into a coverage value-class."""
    m = (1 << width) - 1
    v &= m
    if v == 0:
        return "zero"
    if v == 1:
        return "one"
    if v == m:
        return "all_ones"
    if (v >> (width - 1)) & 1:
        return "neg"
    if v < 256:
        return "pos_small"
    return "pos_large"


def _mnemonic(disasm: Any) -> str:
    if not isinstance(disasm, str) or not disasm.strip():
        return ""
    return disasm.split()[0].lower()


# ─────────────────────────────────────────────────────────────────────────────
# Collector
# ─────────────────────────────────────────────────────────────────────────────
class CoverageCollector:
    def __init__(self, rtl_log: Sequence[Dict[str, Any]],
                 model: Optional[Dict[str, Any]] = None):
        self.rtl = list(rtl_log or [])
        self.model = model or {}
        self.cross_instrs = set(self.model.get("cross_instructions", _DEFAULT_CROSS))
        self.width = 64 if self._detect_rv64() else 32
        self.covered: set = set()
        self.observed_extra: set = set()
        self.by_category: Dict[str, set] = {}
        self.instr_hist: Dict[str, int] = {}

    # -- helpers --------------------------------------------------------------
    def _detect_rv64(self) -> bool:
        for rec in self.rtl:
            if not isinstance(rec, dict):
                continue
            regs = rec.get("regs", {})
            if isinstance(regs, dict):
                for val in regs.values():
                    iv = _to_int(val)
                    if iv is not None and iv > 0xFFFFFFFF:
                        return True
        return False

    def _add(self, category: str, key: str) -> None:
        self.covered.add(f"{category}:{key}")
        self.by_category.setdefault(category, set()).add(key)

    # -- universe -------------------------------------------------------------
    def _universe(self) -> Dict[str, float]:
        """Return {bin: weight} for every bin with a known finite universe."""
        uni: Dict[str, float] = {}
        for n in range(1, 32):
            uni[f"reg:x{n}"] = _CATEGORY_WEIGHT["reg"]
        for c in _VALUE_CLASSES:
            uni[f"valclass:{c}"] = _CATEGORY_WEIGHT["valclass"]
        for d in ("taken", "not_taken"):
            uni[f"branch:{d}"] = _CATEGORY_WEIGHT["branch"]
        for pmode in ("M", "S", "U"):
            uni[f"priv:{pmode}"] = _CATEGORY_WEIGHT["priv"]
        for mnem in self.model.get("instructions", []) or []:
            uni[f"instr:{str(mnem).lower()}"] = _CATEGORY_WEIGHT["instr"]
        for m in sorted(self.cross_instrs):
            for c in _VALUE_CLASSES:
                uni[f"cross:{m}:{c}"] = _CATEGORY_WEIGHT["cross"]
        # model overrides / extensions
        for b, w in (self.model.get("weights", {}) or {}).items():
            uni[b] = float(w)
        for b in (self.model.get("bins", []) or []):
            uni.setdefault(b, 1.0)
        return uni

    # -- main -----------------------------------------------------------------
    def collect(self) -> Dict[str, Any]:
        started = _now()
        abi = {"ra": 1, "sp": 2, "gp": 3, "tp": 4, "fp": 8}
        for i, rec in enumerate(self.rtl):
            if not isinstance(rec, dict):
                continue
            dis = rec.get("disasm", "")
            mnem = _mnemonic(dis)
            if mnem:
                self.instr_hist[mnem] = self.instr_hist.get(mnem, 0) + 1
                self._add("instr", mnem)

            # register writes + value classes
            regs = rec.get("regs", {})
            if isinstance(regs, dict):
                for rname, rval in regs.items():
                    idx = None
                    m = re.fullmatch(r"x(\d+)", str(rname))
                    if m:
                        idx = int(m.group(1))
                    elif str(rname) in abi:
                        idx = abi[str(rname)]
                    iv = _to_int(rval)
                    if idx is not None and idx != 0:
                        self._add("reg", f"x{idx}")
                    if iv is not None:
                        vc = classify_value(iv, self.width)
                        self._add("valclass", vc)
                        # cross coverage: instruction × result value-class
                        if mnem in self.cross_instrs and idx != 0:
                            self._add("cross", f"{mnem}:{vc}")

            # CSR coverage (telemetry)
            csrs = rec.get("csrs", {})
            if isinstance(csrs, dict):
                for cname in csrs:
                    self.observed_extra.add(f"csr:{cname}")

            # privilege mode
            pmode = rec.get("priv") or rec.get("mode")
            if isinstance(pmode, str) and pmode.upper() in ("M", "S", "U"):
                self._add("priv", pmode.upper())

            # trap coverage (telemetry)
            trap = rec.get("trap")
            if isinstance(trap, dict) and trap.get("cause") is not None:
                self.observed_extra.add(f"trap:cause{_to_int(trap.get('cause'))}")

            # vtype coverage (telemetry)
            vt = rec.get("vtype")
            if isinstance(vt, dict) and vt.get("sew") is not None:
                self.observed_extra.add(f"vtype:sew{vt.get('sew')}:lmul{vt.get('lmul')}")

            # branch direction (needs the next record's PC)
            if mnem in _COND_BRANCH:
                self._record_branch_dir(rec, i)

        return self._report(started)

    def _record_branch_dir(self, rec: Dict[str, Any], i: int) -> None:
        pc = _to_int(rec.get("pc"))
        if pc is None or i + 1 >= len(self.rtl):
            return
        nxt = self.rtl[i + 1]
        if not isinstance(nxt, dict):
            return
        npc = _to_int(nxt.get("pc"))
        if npc is None:
            return
        size = 2 if _mnemonic(rec.get("disasm", "")).startswith("c.") else 4
        self._add("branch", "not_taken" if npc == pc + size else "taken")

    # -- report ---------------------------------------------------------------
    def _report(self, started: str) -> Dict[str, Any]:
        uni = self._universe()
        total = set(uni)
        covered_in = self.covered & total
        holes = sorted(total - covered_in)
        pct = round(len(covered_in) / len(total), 4) if total else 1.0

        cats: Dict[str, Any] = {}
        for cat in ("reg", "valclass", "branch", "priv", "instr", "cross"):
            cat_total = sorted(b for b in total if b.split(":", 1)[0] == cat)
            cat_cov = sorted(b for b in covered_in if b.split(":", 1)[0] == cat)
            if cat_total:
                cats[cat] = {"covered": len(cat_cov), "total": len(cat_total),
                             "pct": round(len(cat_cov) / len(cat_total), 4)}
        band = ("VERIFIED" if pct >= 0.90 else "HIGH" if pct >= 0.70 else
                "MEDIUM" if pct >= 0.50 else "LOW" if pct >= 0.30 else "CRITICAL")

        return {
            "schema_version": SCHEMA_VERSION,
            "agent": AGENT_NAME,
            "started_at": started,
            "finished_at": _now(),
            "records_analyzed": len(self.rtl),
            "xlen": self.width,
            "coverage_pct": pct,
            "band": band,
            "pass": True,                              # collector never fails a run
            "covered_bins": sorted(covered_in),
            "total_bins": sorted(total),
            "holes": holes,
            "holes_count": len(holes),
            "weights": uni,
            "by_category": cats,
            "observed_extra": sorted(self.observed_extra)[:200],
            "top_instructions": sorted(self.instr_hist.items(),
                                       key=lambda kv: -kv[1])[:20],
            # embedded machine snapshot for the self-evolving planner
            "coverage_summary": {
                "covered_bins": sorted(covered_in),
                "total_bins": sorted(total),
                "holes": holes,
                "weights": uni,
            },
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
                    continue
    except OSError:
        pass
    return out


def run_from_manifest(manifest_path: str) -> int:
    """Load the RTL commit log, compute coverage, and write both
    ``coverage_report.json`` and the machine ``coverage_summary.json``
    (consumed by the self-evolving planner). Always returns 0 (advisory)."""
    mp = Path(manifest_path)
    try:
        manifest = json.loads(mp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("coverage_collector: cannot read manifest: %s", exc)
        return 0
    run_dir = Path(manifest.get("run_dir", mp.parent))
    outputs = manifest.get("outputs", {})
    rtl = _load_jsonl(run_dir / outputs.get("rtl_commit_log", "rtl_commit.jsonl"))
    model = manifest.get("coverage_model")
    rep = CoverageCollector(rtl, model=model).collect()
    try:
        (run_dir / "coverage_report.json").write_text(
            json.dumps(rep, indent=2), encoding="utf-8")
        (run_dir / "coverage_summary.json").write_text(
            json.dumps(rep["coverage_summary"], indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("coverage_collector: cannot write report: %s", exc)
    return 0


if __name__ == "__main__":  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="AVA functional coverage collector")
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args()
    raise SystemExit(run_from_manifest(args.manifest))
