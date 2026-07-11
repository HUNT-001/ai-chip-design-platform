"""
AGENT_H.ooo_verifier — Out-of-Order Scoreboard Checker (T52)
=============================================================

Golden checker for out-of-order (OOO) execution *scheduling* — the correctness
of the scoreboard / Tomasulo / ROB machinery that lets instructions execute out
of program order while preserving sequential semantics. The functional result
of each instruction is already covered by the tandem-diff and the
`pipeline_verifier`; what OOO adds — and what this agent targets — is the
*timing/ordering* discipline that makes OOO safe:

- instructions may **execute** out of order, but must **commit in program
  order** (the ROB retires in order);
- an instruction may **issue only when its source operands are ready** — i.e.
  after the newest earlier producer of each source has completed (RAW respected
  through forwarding/wakeup);
- register **renaming** must give each in-flight instruction a private physical
  destination (no two live instructions sharing one);
- a **squashed** (mis-speculated) instruction must never commit
  (precise-exception / branch-recovery discipline).

Checks
------
| Check | Sev | Catches |
|---|---|---|
| `ooo_commit_order` | HIGH | commit not in program order (ROB retired out of order) |
| `ooo_exec_timing` | HIGH | `issue ≤ complete ≤ commit` violated |
| `ooo_raw_hazard` | HIGH | issued before a source producer completed (stale read) |
| `ooo_rename` | MED | two in-flight instructions share a physical dest reg |
| `ooo_squash` | MED | a squashed instruction committed |

Metrics (never fail): instruction count, max in-flight (reorder depth), mean
issue→commit latency.

Additive trace contract (extends the commit log per record)
-----------------------------------------------------------
```
{"seq":0, "pc":"0x..", "disasm":"add x5,x6,x7",
 "ooo": {"issue":1, "complete":2, "commit":3,
         "src":["x6","x7"], "dst":"x5", "pdst":40, "squashed":false}}
```
`src`/`dst` may be omitted — they're parsed from the disassembly. `issue`/
`complete`/`commit`/`pdst`/`squashed` may also be given at top level.

Stdlib-only, schema-v2.1.0 report, graceful degradation throughout.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

log = logging.getLogger("AGENT_H.ooo")

SCHEMA_VERSION = "2.1.0"
AGENT_NAME = "ooo_verifier"
_ABI = {"zero": 0, "ra": 1, "sp": 2, "gp": 3, "tp": 4, "fp": 8,
        "t0": 5, "t1": 6, "t2": 7, "s0": 8, "s1": 9,
        "a0": 10, "a1": 11, "a2": 12, "a3": 13, "a4": 14, "a5": 15,
        "a6": 16, "a7": 17}


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


def _reg(tok: str) -> Optional[str]:
    """Normalise a register token to canonical xN, or None if not a register."""
    t = tok.strip().strip("()")
    if re.fullmatch(r"x\d+", t):
        return t
    if t in _ABI:
        return f"x{_ABI[t]}"
    return None


def _decode_regs(disasm: str) -> Tuple[Optional[str], List[str]]:
    """(dst, [srcs]) parsed from a disassembly, registers only."""
    if not isinstance(disasm, str) or not disasm.strip():
        return None, []
    toks = disasm.replace(",", " ").split()
    if len(toks) < 2:
        return None, []
    ops = [_reg(t) for t in toks[1:]]
    ops = [o for o in ops if o]
    if not ops:
        return None, []
    dst = ops[0] if ops[0] != "x0" else None       # x0 writes discarded
    srcs = [o for o in ops[1:] if o and o != "x0"]
    return dst, srcs


class _Instr:
    __slots__ = ("i", "seq", "dst", "srcs", "issue", "complete",
                 "commit", "pdst", "squashed", "trap")

    def __init__(self, i: int, rec: Dict[str, Any]):
        o = rec.get("ooo", {}) if isinstance(rec.get("ooo"), dict) else {}

        def g(k):
            return o.get(k, rec.get(k))
        self.i = i
        self.seq = _to_int(rec.get("seq"))
        if self.seq is None:
            self.seq = i
        dst, srcs = _decode_regs(rec.get("disasm", ""))
        self.dst = o.get("dst", rec.get("dst", dst))
        self.srcs = o.get("src", rec.get("src", srcs)) or []
        self.issue = _to_int(g("issue"))
        self.complete = _to_int(g("complete"))
        self.commit = _to_int(g("commit"))
        self.pdst = _to_int(g("pdst"))
        self.squashed = bool(g("squashed"))
        self.trap = isinstance(rec.get("trap"), dict)


class OOOVerifier:
    def __init__(self, records: Sequence[Dict[str, Any]]):
        self.recs = [r for r in (records or []) if isinstance(r, dict)]
        self.violations: List[Dict[str, Any]] = []
        self.metrics = {"instructions": 0, "max_inflight": 0,
                        "mean_latency": 0.0, "ooo_active": False}

    def _v(self, seq: Any, check: str, sev: str, detail: str) -> None:
        self.violations.append({"seq": seq, "check": check,
                                "severity": sev, "detail": detail})

    def run(self) -> Dict[str, Any]:
        started = _now()
        instrs = [_Instr(i, r) for i, r in enumerate(self.recs)]
        # program order
        instrs.sort(key=lambda x: x.seq)
        has_sched = any(x.issue is not None or x.commit is not None for x in instrs)
        self.metrics["ooo_active"] = has_sched
        self.metrics["instructions"] = len(instrs)

        last_writer: Dict[str, _Instr] = {}
        prev_commit: Optional[int] = None
        latencies: List[int] = []

        for x in instrs:
            # -- exec timing --
            if x.issue is not None and x.complete is not None and x.complete < x.issue:
                self._v(x.seq, "ooo_exec_timing", "HIGH",
                        f"complete({x.complete}) < issue({x.issue})")
            if x.complete is not None and x.commit is not None and x.commit < x.complete:
                self._v(x.seq, "ooo_exec_timing", "HIGH",
                        f"commit({x.commit}) < complete({x.complete})")

            # -- RAW hazard: issue only after the newest earlier producer done --
            if x.issue is not None:
                for s in x.srcs:
                    p = last_writer.get(s)
                    if p is not None and p.complete is not None and x.issue < p.complete:
                        self._v(x.seq, "ooo_raw_hazard", "HIGH",
                                f"issued at {x.issue} but source {s} producer "
                                f"(seq {p.seq}) completes at {p.complete}")

            # -- commit in program order --
            if x.commit is not None and not x.squashed:
                if prev_commit is not None and x.commit < prev_commit:
                    self._v(x.seq, "ooo_commit_order", "HIGH",
                            f"commit {x.commit} < previous program-order commit {prev_commit}")
                prev_commit = max(prev_commit, x.commit) if prev_commit is not None else x.commit

            # -- squash discipline --
            if x.squashed and x.commit is not None:
                self._v(x.seq, "ooo_squash", "MEDIUM",
                        f"squashed instruction committed (cycle {x.commit})")

            if x.issue is not None and x.commit is not None:
                latencies.append(x.commit - x.issue)

            if x.dst and x.dst != "x0" and not x.squashed:
                last_writer[x.dst] = x

        self._check_rename(instrs)
        self._check_inflight(instrs)
        if latencies:
            self.metrics["mean_latency"] = round(sum(latencies) / len(latencies), 3)
        return self._report(started)

    def _check_rename(self, instrs: List[_Instr]) -> None:
        """No two in-flight (issue..commit) instructions may share a pdst."""
        by_pdst: Dict[int, List[_Instr]] = {}
        for x in instrs:
            if x.pdst is not None and x.issue is not None and x.commit is not None:
                by_pdst.setdefault(x.pdst, []).append(x)
        for pdst, xs in by_pdst.items():
            xs.sort(key=lambda z: z.issue)
            for a, b in zip(xs, xs[1:]):
                if b.issue < a.commit:              # overlap while both live
                    self._v(b.seq, "ooo_rename", "MEDIUM",
                            f"physical reg {pdst} reused by seq {b.seq} at issue "
                            f"{b.issue} while seq {a.seq} live until commit {a.commit}")

    def _check_inflight(self, instrs: List[_Instr]) -> None:
        events: List[Tuple[int, int]] = []
        for x in instrs:
            if x.issue is not None and x.commit is not None:
                events.append((x.issue, +1))
                events.append((x.commit, -1))
        events.sort()
        cur = mx = 0
        for _, delta in events:
            cur += delta
            mx = max(mx, cur)
        self.metrics["max_inflight"] = mx

    def _report(self, started: str) -> Dict[str, Any]:
        total = len(self.violations)
        high = sum(1 for v in self.violations if v["severity"] == "HIGH")
        med = total - high
        return {
            "schema_version": SCHEMA_VERSION,
            "agent": AGENT_NAME,
            "started_at": started,
            "finished_at": _now(),
            "records_checked": len(self.recs),
            "ooo_active": self.metrics["ooo_active"],
            "metrics": self.metrics,
            "total_violations": total,
            "high_violations": high,
            "medium_violations": med,
            "severity_score": high * 3 + med,
            "band": "CLEAN" if total == 0 else "CRITICAL" if high else "DEGRADED",
            "pass": high == 0,
            "violations": self.violations[:100],
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
    mp = Path(manifest_path)
    try:
        manifest = json.loads(mp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("ooo_verifier: cannot read manifest: %s", exc)
        return 0
    run_dir = Path(manifest.get("run_dir", mp.parent))
    outputs = manifest.get("outputs", {})
    name = outputs.get("ooo_trace", outputs.get("rtl_commit_log", "rtl_commit.jsonl"))
    recs = _load_jsonl(run_dir / name)
    rep = OOOVerifier(recs).run()
    if not rep.get("ooo_active"):
        rep = {"schema_version": SCHEMA_VERSION, "agent": AGENT_NAME,
               "status": "skipped", "reason": "no OOO scheduling fields", "pass": True}
    else:
        rep["status"] = "completed"
    try:
        (run_dir / "ooo_report.json").write_text(json.dumps(rep, indent=2),
                                                 encoding="utf-8")
    except OSError as exc:
        log.warning("ooo_verifier: cannot write report: %s", exc)
    return 0 if rep.get("pass", True) else 1


if __name__ == "__main__":  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="AVA out-of-order scoreboard checker")
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args()
    raise SystemExit(run_from_manifest(args.manifest))
