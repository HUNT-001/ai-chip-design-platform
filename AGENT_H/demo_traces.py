"""
AGENT_H.demo_traces — Canonical Trace Synthesizer / worked examples
====================================================================

Emits a **well-formed, passing** example of every trace the extended-tier agents
consume, plus an enriched commit log, into a run directory — and writes a
matching run manifest. Two purposes:

1. **Reference** — a canonical, spec-valid example of each additive trace
   format (`coherence_trace`, `consistency_trace`, `interrupt_trace`,
   `debug_trace`, `hypervisor_trace`, `aia_trace`, `reset_snapshot`, and an
   enriched `rtl_commit.jsonl`). This is the contract the real ISS/RTL adapters
   (AGENT_C/D) should populate to make the opt-in agents fire in a live run.
2. **Integration harness** — `write_demo_run(run_dir)` lets a single test drive
   every Phase-6 manifest-agent end-to-end (see `tests/test_agents.py`).

Every trace here is deliberately *clean* (no violations), so a correct agent
returns `pass=True` / `status="completed"` — the integration test asserts the
tier fires (not skipped) and agrees.

Stdlib-only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List


# ─────────────────────────────────────────────────────────────────────────────
# Enriched commit log — exercises vector, perf-counter, branch-predictor,
# coverage-collector (all commit-log agents) in one stream.
# ─────────────────────────────────────────────────────────────────────────────
def commit_log() -> List[Dict[str, Any]]:
    return [
        # vector configure: VLMAX = 1*128/8 = 16, AVL 4 → vl 4
        {"schema_version": "2.1.0", "seq": 0, "pc": "0x80000000",
         "disasm": "vsetvli x1,x2,e8,m1", "regs": {"x1": "0x4"}, "csrs": {},
         "vtype": {"sew": 8, "lmul": 1}, "vl": 4, "avl": 4, "vlen": 128,
         "perf_counters": {"cycles": 1, "instret": 1}},
        # vector add: element-wise golden 1+10,2+20,3+30,4+40
        {"schema_version": "2.1.0", "seq": 1, "pc": "0x80000004",
         "disasm": "vadd.vv v1,v2,v3", "regs": {}, "csrs": {},
         "vtype": {"sew": 8, "lmul": 1}, "vl": 4,
         "vregs": {"v2": [1, 2, 3, 4], "v3": [10, 20, 30, 40],
                   "v1": [11, 22, 33, 44]},
         "perf_counters": {"cycles": 2, "instret": 2}},
        # scalar arithmetic (reg/valclass/cross/operand coverage)
        {"schema_version": "2.1.0", "seq": 2, "pc": "0x80000008",
         "disasm": "add x5,x6,x7", "regs": {"x5": "0x80000000"},
         "csrs": {"mstatus": "0x1800"}, "priv": "M",
         "perf_counters": {"cycles": 3, "instret": 3}},
        # taken conditional branch (operands equal) → next pc = target
        {"schema_version": "2.1.0", "seq": 3, "pc": "0x8000000c",
         "disasm": "beq x1,x1,0x80000040", "regs": {}, "csrs": {},
         "target": "0x80000040",
         "perf_counters": {"cycles": 4, "instret": 4}},
        {"schema_version": "2.1.0", "seq": 4, "pc": "0x80000040",
         "disasm": "addi x0,x0,0", "regs": {}, "csrs": {},
         "perf_counters": {"cycles": 5, "instret": 5}},
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Additive multicore / system traces — all clean/passing.
# ─────────────────────────────────────────────────────────────────────────────
def coherence_trace() -> List[Dict[str, Any]]:
    # producer/consumer, then two shared readers — coherent
    return [
        {"core": 0, "op": "store", "addr": "0x40", "value": "0x7", "cycle": 1},
        {"core": 1, "op": "load", "addr": "0x40", "value": "0x7", "cycle": 2},
        {"core": 2, "op": "load", "addr": "0x40", "value": "0x7", "cycle": 3},
    ]


def consistency_trace() -> List[Dict[str, Any]]:
    # SB (store buffering) — permitted under TSO, so no violation
    return [
        {"core": 0, "op": "store", "addr": "0x10", "value": 1},
        {"core": 0, "op": "load", "addr": "0x20", "value": 0},
        {"core": 1, "op": "store", "addr": "0x20", "value": 1},
        {"core": 1, "op": "load", "addr": "0x10", "value": 0},
    ]


def interrupt_trace() -> List[Dict[str, Any]]:
    return [
        {"op": "config", "priorities": {"3": 7, "5": 4},
         "enables": {"0": [3, 5]}, "thresholds": {"0": 2}},
        {"op": "pending", "source": 3}, {"op": "pending", "source": 5},
        {"op": "claim", "context": 0, "result": 3},       # 3 has higher priority
        {"op": "clint", "mtime": 100, "mtimecmp": 100, "mtip": True},
        {"op": "clic_config", "nlbits": 4, "ctl": {"3": 0xF0, "5": 0xC0},
         "enables": [3, 5]},
        {"op": "pending", "source": 3}, {"op": "pending", "source": 5},
        {"op": "clic_claim", "result": 3},
    ]


def debug_trace() -> List[Dict[str, Any]]:
    return [
        {"op": "trigger_config", "index": 0, "execute": True,
         "tdata2": "0x80000040", "action": 1, "priv": ["M"]},
        {"op": "exec", "pc": "0x80000040", "priv": "M", "fired": True,
         "dcsr_cause": 2},
        {"op": "halt", "cause": "haltreq", "pc": "0x100", "dpc": "0x100",
         "dcsr_cause": 3},
        {"op": "step", "instrs_executed": 1},
    ]


def hypervisor_trace() -> List[Dict[str, Any]]:
    return [
        {"op": "config",
         "vs_map": {"0x80": {"gpn": "0x120", "r": True, "w": True,
                             "x": True, "v": True}},
         "g_map": {"0x120": {"ppn": "0x300", "r": True, "w": True,
                             "x": True, "v": True}}},
        {"op": "translate", "gva": "0x80abc", "access": "load", "pa": "0x300abc"},
    ]


def aia_trace() -> List[Dict[str, Any]]:
    return [
        {"op": "imsic_config", "eidelivery": 1, "eithreshold": 8,
         "eie": [3, 7], "eip": [3, 7]},
        {"op": "imsic_topei", "result": 3},               # lowest identity
    ]


def reset_snapshot() -> Dict[str, Any]:
    return {"hart": 0, "priv": "M", "pc": "0x80000000",
            "csrs": {"mstatus": "0x0", "misa": "0x40141101", "mie": "0x0"},
            "expected": {"pc": "0x80000000"}}


# ─────────────────────────────────────────────────────────────────────────────
# Writer
# ─────────────────────────────────────────────────────────────────────────────
_OUTPUTS = {
    "rtl_commit_log": "rtl_commit.jsonl",
    "coherence_trace": "coherence_trace.jsonl",
    "consistency_trace": "consistency_trace.jsonl",
    "interrupt_trace": "interrupt_trace.jsonl",
    "debug_trace": "debug_trace.jsonl",
    "hypervisor_trace": "hypervisor_trace.jsonl",
    "aia_trace": "aia_trace.jsonl",
    "reset_snapshot": "reset_snapshot.json",
}


def _write_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")


def write_demo_run(run_dir: str) -> str:
    """Populate ``run_dir`` with every demo trace + a run manifest.
    Returns the manifest path."""
    d = Path(run_dir)
    d.mkdir(parents=True, exist_ok=True)
    _write_jsonl(d / _OUTPUTS["rtl_commit_log"], commit_log())
    _write_jsonl(d / _OUTPUTS["coherence_trace"], coherence_trace())
    _write_jsonl(d / _OUTPUTS["consistency_trace"], consistency_trace())
    _write_jsonl(d / _OUTPUTS["interrupt_trace"], interrupt_trace())
    _write_jsonl(d / _OUTPUTS["debug_trace"], debug_trace())
    _write_jsonl(d / _OUTPUTS["hypervisor_trace"], hypervisor_trace())
    _write_jsonl(d / _OUTPUTS["aia_trace"], aia_trace())
    (d / _OUTPUTS["reset_snapshot"]).write_text(
        json.dumps(reset_snapshot()), encoding="utf-8")
    manifest = {
        "schema_version": "2.1.0", "run_id": "demo", "run_dir": str(d),
        "status": "completed", "isa": "rv32imafdcv", "memory_model": "tso",
        "outputs": dict(_OUTPUTS),
    }
    mpath = d / "run_manifest.json"
    mpath.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return str(mpath)


if __name__ == "__main__":  # pragma: no cover
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else "./demo_run"
    print("wrote demo run + manifest:", write_demo_run(out))
