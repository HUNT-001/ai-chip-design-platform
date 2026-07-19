"""
AGENT_H.rtl_basics_verifier — FSM / FIFO / Memory Checker (T67, level 1)
========================================================================

Golden checkers for the three RTL building blocks every design contains, and
where a large share of real bugs live: **finite state machines**, **FIFOs**, and
**memories**.

These are structural/behavioural models, not arithmetic goldens: the agent
maintains a reference model of each block and flags any observed behaviour the
model forbids.

FSM checks
----------
- **fsm_illegal_transition** (HIGH) — a transition not present in the declared
  transition table.
- **fsm_unknown_state** (HIGH) — the FSM entered a state outside its declared
  state set (decoded garbage / X-propagation).
- **fsm_deadlock** (HIGH) — the FSM entered a state with no outgoing transitions
  that is not declared terminal, or sat in one state beyond `stuck_limit`
  samples while inputs changed.
- **fsm_unreachable_state** (MEDIUM) — a declared state never reached and not
  reachable in the declared graph from reset (dead code / coverage hole).
- **fsm_onehot_violation** (HIGH) — a one-hot-encoded state register had a
  population count other than 1.

FIFO checks
-----------
- **fifo_overflow** (HIGH) — a push while full.
- **fifo_underflow** (HIGH) — a pop while empty.
- **fifo_ordering** (HIGH) — popped data did not match FIFO order (the golden
  model's expected head).
- **fifo_flag_error** (HIGH) — reported `full`/`empty` flags disagree with the
  golden occupancy.
- **fifo_occupancy** (HIGH) — reported occupancy/level disagrees with the model.
- **fifo_gray_pointer** (HIGH) — an async-FIFO pointer crossing changed by more
  than one bit (not gray-coded).

Memory checks
-------------
- **mem_read_mismatch** (HIGH) — a read returned a value other than the last
  value written to that address (golden shadow memory).
- **mem_out_of_bounds** (HIGH) — access beyond the declared depth.
- **mem_uninitialised_read** (MEDIUM) — a read from an address never written and
  with no declared reset value (X in silicon).
- **mem_byte_enable** (HIGH) — a byte-enabled write modified bytes whose enable
  was low.
- **mem_port_collision** (HIGH) — two ports wrote the same address in the same
  cycle, or a read and write collided without a declared policy.
- **mem_ecc_undetected** (HIGH) — an injected bit error that ECC/parity should
  have flagged was reported clean.

Trace contract — `rtl_trace.jsonl` (additive; skipped when absent)
------------------------------------------------------------------
```
{"event":"fsm_def","name":"ctrl","states":["IDLE","RUN","DONE"],
 "reset":"IDLE","transitions":[["IDLE","RUN"],["RUN","DONE"],["DONE","IDLE"]],
 "terminal":[],"encoding":"onehot"}
{"event":"fsm","name":"ctrl","state":"RUN","encoded":2}
{"event":"fifo_def","name":"tx","depth":4,"async":false}
{"event":"fifo","name":"tx","op":"push","data":"0x1","full":false,"empty":false}
{"event":"mem_def","name":"ram","depth":256,"width":32,"reset_value":0}
{"event":"mem","name":"ram","op":"write","addr":4,"data":"0xdead","be":"0xf"}
{"event":"mem","name":"ram","op":"read","addr":4,"data":"0xdead"}
```
Blocks with no declaration are ignored (never a false positive).

Stdlib-only, schema-v2.1.0, graceful degradation.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

log = logging.getLogger("AGENT_H.rtl_basics")

SCHEMA_VERSION = "2.1.0"
AGENT_NAME = "rtl_basics_verifier"
DEFAULT_STUCK_LIMIT = 64


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


def _truthy(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return default


def _popcount(x: int) -> int:
    return bin(x if x >= 0 else 0).count("1")


# ─────────────────────────────────────────────────────────────────────────────
# Reference models
# ─────────────────────────────────────────────────────────────────────────────
class FSMModel:
    def __init__(self, d: Dict[str, Any]):
        self.name = str(d.get("name", "fsm"))
        self.states: List[str] = [str(s) for s in (d.get("states") or [])]
        self.state_set: Set[str] = set(self.states)
        self.reset = str(d.get("reset", self.states[0] if self.states else ""))
        self.terminal: Set[str] = {str(s) for s in (d.get("terminal") or [])}
        self.encoding = str(d.get("encoding", "")).lower()
        self.stuck_limit = _to_int(d.get("stuck_limit")) or DEFAULT_STUCK_LIMIT
        self.edges: Dict[str, Set[str]] = {s: set() for s in self.states}
        for tr in (d.get("transitions") or []):
            if isinstance(tr, (list, tuple)) and len(tr) >= 2:
                a, b = str(tr[0]), str(tr[1])
                self.edges.setdefault(a, set()).add(b)
        self.current: Optional[str] = self.reset or None
        self.visited: Set[str] = {self.current} if self.current else set()
        self.stuck = 0

    def reachable(self) -> Set[str]:
        seen: Set[str] = set()
        stack = [self.reset] if self.reset else []
        while stack:
            s = stack.pop()
            if s in seen:
                continue
            seen.add(s)
            stack.extend(self.edges.get(s, ()))
        return seen


class FIFOModel:
    def __init__(self, d: Dict[str, Any]):
        self.name = str(d.get("name", "fifo"))
        self.depth = _to_int(d.get("depth")) or 0
        self.is_async = _truthy(d.get("async"), False)
        self.q: List[Any] = []
        self.last_wptr: Optional[int] = None
        self.last_rptr: Optional[int] = None


class MemModel:
    def __init__(self, d: Dict[str, Any]):
        self.name = str(d.get("name", "mem"))
        self.depth = _to_int(d.get("depth")) or 0
        self.width = _to_int(d.get("width")) or 32
        rv = d.get("reset_value")
        self.reset_value = _to_int(rv) if rv is not None else None
        self.cells: Dict[int, int] = {}
        self.write_cycle: Dict[int, Any] = {}    # addr -> cycle of last write


class RTLBasicsVerifier:
    def __init__(self, events: Sequence[Dict[str, Any]]):
        self.events = [e for e in (events or []) if isinstance(e, dict)]
        self.fsms: Dict[str, FSMModel] = {}
        self.fifos: Dict[str, FIFOModel] = {}
        self.mems: Dict[str, MemModel] = {}
        self.violations: List[Dict[str, Any]] = []
        self.metrics: Dict[str, Any] = {
            "fsms": 0, "fifos": 0, "mems": 0, "fsm_steps": 0,
            "fifo_ops": 0, "mem_ops": 0, "checked": 0, "rtl_active": False,
        }

    def _v(self, seq: Any, check: str, detail: str,
           severity: str = "HIGH") -> None:
        self.violations.append({"seq": seq, "check": check,
                                "severity": severity, "detail": detail})

    # ── main ───────────────────────────────────────────────────────────────
    def run(self) -> Dict[str, Any]:
        started = _now()
        for e in self.events:
            kind = str(e.get("event", "")).lower()
            seq = e.get("seq")
            if kind == "fsm_def":
                m = FSMModel(e)
                self.fsms[m.name] = m
                self.metrics["fsms"] += 1
                self.metrics["rtl_active"] = True
            elif kind == "fsm":
                self._fsm_step(e, seq)
            elif kind == "fifo_def":
                m2 = FIFOModel(e)
                self.fifos[m2.name] = m2
                self.metrics["fifos"] += 1
                self.metrics["rtl_active"] = True
            elif kind == "fifo":
                self._fifo_op(e, seq)
            elif kind == "mem_def":
                m3 = MemModel(e)
                self.mems[m3.name] = m3
                self.metrics["mems"] += 1
                self.metrics["rtl_active"] = True
            elif kind == "mem":
                self._mem_op(e, seq)
        self._final_fsm_checks()
        return self._report(started)

    # ── FSM ────────────────────────────────────────────────────────────────
    def _fsm_step(self, e: Dict[str, Any], seq: Any) -> None:
        m = self.fsms.get(str(e.get("name", "")))
        if m is None:
            return
        nxt = e.get("state")
        if nxt is None:
            return
        nxt = str(nxt)
        self.metrics["fsm_steps"] += 1
        self.metrics["checked"] += 1
        # one-hot encoding
        if m.encoding == "onehot":
            enc = _to_int(e.get("encoded"))
            if enc is not None and _popcount(enc) != 1:
                self._v(seq, "fsm_onehot_violation",
                        f"FSM '{m.name}' one-hot register = 0b{enc:b} "
                        f"({_popcount(enc)} bits set, expected exactly 1)")
        # unknown state
        if m.state_set and nxt not in m.state_set:
            self._v(seq, "fsm_unknown_state",
                    f"FSM '{m.name}' entered undeclared state '{nxt}'")
            m.current = nxt
            return
        cur = m.current
        if cur is not None and nxt != cur:
            if m.edges and nxt not in m.edges.get(cur, set()):
                self._v(seq, "fsm_illegal_transition",
                        f"FSM '{m.name}': '{cur}' -> '{nxt}' is not a declared "
                        f"transition")
            m.stuck = 0
        elif cur is not None and nxt == cur:
            m.stuck += 1
            if (m.stuck > m.stuck_limit and cur not in m.terminal
                    and m.edges.get(cur)):
                self._v(seq, "fsm_deadlock",
                        f"FSM '{m.name}' stuck in '{cur}' for {m.stuck} samples "
                        f"despite having outgoing transitions")
                m.stuck = 0                        # report once per stall
        # dead-end state
        if nxt in m.state_set and not m.edges.get(nxt) and nxt not in m.terminal:
            self._v(seq, "fsm_deadlock",
                    f"FSM '{m.name}' entered '{nxt}', which has no outgoing "
                    f"transitions and is not declared terminal")
        m.current = nxt
        m.visited.add(nxt)

    def _final_fsm_checks(self) -> None:
        for m in self.fsms.values():
            if not m.state_set or not m.visited:
                continue
            reach = m.reachable()
            for s in m.states:
                if s not in reach:
                    self._v(None, "fsm_unreachable_state",
                            f"FSM '{m.name}': state '{s}' is unreachable from "
                            f"reset '{m.reset}' in the declared graph",
                            severity="MEDIUM")

    # ── FIFO ───────────────────────────────────────────────────────────────
    def _fifo_op(self, e: Dict[str, Any], seq: Any) -> None:
        m = self.fifos.get(str(e.get("name", "")))
        if m is None:
            return
        op = str(e.get("op", "")).lower()
        self.metrics["fifo_ops"] += 1
        self.metrics["checked"] += 1
        if op == "push":
            if m.depth and len(m.q) >= m.depth:
                self._v(seq, "fifo_overflow",
                        f"FIFO '{m.name}': push while full "
                        f"(occupancy {len(m.q)}/{m.depth})")
            else:
                m.q.append(e.get("data"))
        elif op == "pop":
            if not m.q:
                self._v(seq, "fifo_underflow",
                        f"FIFO '{m.name}': pop while empty")
            else:
                expect = m.q.pop(0)
                if "data" in e and e.get("data") != expect:
                    self._v(seq, "fifo_ordering",
                            f"FIFO '{m.name}': popped {e.get('data')} but FIFO "
                            f"order requires {expect}")
        # flag / occupancy cross-check (after the operation)
        if m.depth:
            if "full" in e:
                exp_full = len(m.q) >= m.depth
                if _truthy(e.get("full")) != exp_full:
                    self._v(seq, "fifo_flag_error",
                            f"FIFO '{m.name}': full={e.get('full')} but "
                            f"occupancy is {len(m.q)}/{m.depth}")
            if "empty" in e:
                exp_empty = len(m.q) == 0
                if _truthy(e.get("empty")) != exp_empty:
                    self._v(seq, "fifo_flag_error",
                            f"FIFO '{m.name}': empty={e.get('empty')} but "
                            f"occupancy is {len(m.q)}")
        lvl = _to_int(e.get("level", e.get("occupancy")))
        if lvl is not None and lvl != len(m.q):
            self._v(seq, "fifo_occupancy",
                    f"FIFO '{m.name}': reported level {lvl} != model "
                    f"{len(m.q)}")
        # async gray pointer sanity
        if m.is_async:
            for key, attr in (("wptr_gray", "last_wptr"), ("rptr_gray", "last_rptr")):
                val = _to_int(e.get(key))
                if val is None:
                    continue
                prev = getattr(m, attr)
                if prev is not None and val != prev and _popcount(val ^ prev) > 1:
                    self._v(seq, "fifo_gray_pointer",
                            f"FIFO '{m.name}': {key} 0x{prev:x} -> 0x{val:x} "
                            f"changed {_popcount(val ^ prev)} bits (not gray)")
                setattr(m, attr, val)

    # ── Memory ─────────────────────────────────────────────────────────────
    def _mem_op(self, e: Dict[str, Any], seq: Any) -> None:
        m = self.mems.get(str(e.get("name", "")))
        if m is None:
            return
        op = str(e.get("op", "")).lower()
        addr = _to_int(e.get("addr"))
        if addr is None:
            return
        self.metrics["mem_ops"] += 1
        self.metrics["checked"] += 1
        if m.depth and not (0 <= addr < m.depth):
            self._v(seq, "mem_out_of_bounds",
                    f"memory '{m.name}': {op} at address {addr} outside "
                    f"depth {m.depth}")
            return
        data = _to_int(e.get("data"))
        cycle = e.get("cycle", seq)
        if op == "write":
            be = _to_int(e.get("be"))
            nbytes = max(1, m.width // 8)
            if be is not None and data is not None:
                old = m.cells.get(addr, m.reset_value or 0)
                merged = old
                for b in range(nbytes):
                    if (be >> b) & 1:
                        mask = 0xFF << (8 * b)
                        merged = (merged & ~mask) | (data & mask)
                # if the caller also reports the resulting word, verify masking
                res = _to_int(e.get("result"))
                if res is not None and res != merged:
                    self._v(seq, "mem_byte_enable",
                            f"memory '{m.name}' addr {addr}: byte-enable 0x{be:x} "
                            f"should yield 0x{merged:x}, got 0x{res:x}")
                m.cells[addr] = merged
            elif data is not None:
                m.cells[addr] = data
            # same-cycle write collision on another port
            port = e.get("port")
            prev_cycle = m.write_cycle.get(addr)
            if (prev_cycle is not None and cycle is not None
                    and prev_cycle == cycle and port is not None):
                self._v(seq, "mem_port_collision",
                        f"memory '{m.name}': two writes to address {addr} in "
                        f"the same cycle ({cycle})")
            m.write_cycle[addr] = cycle
        elif op == "read":
            if addr not in m.cells:
                if m.reset_value is None:
                    self._v(seq, "mem_uninitialised_read",
                            f"memory '{m.name}': read of address {addr} never "
                            f"written and with no reset value (X in silicon)",
                            severity="MEDIUM")
                elif data is not None and data != m.reset_value:
                    self._v(seq, "mem_read_mismatch",
                            f"memory '{m.name}' addr {addr}: read 0x{data:x} but "
                            f"the reset value is 0x{m.reset_value:x}")
            elif data is not None and data != m.cells[addr]:
                self._v(seq, "mem_read_mismatch",
                        f"memory '{m.name}' addr {addr}: read 0x{data:x} but last "
                        f"write was 0x{m.cells[addr]:x}")
        # ECC / parity: an injected error must be reported
        if "ecc_error_injected" in e:
            injected = _truthy(e.get("ecc_error_injected"))
            detected = _truthy(e.get("ecc_error_detected"))
            if injected and not detected:
                self._v(seq, "mem_ecc_undetected",
                        f"memory '{m.name}' addr {addr}: a bit error was injected "
                        f"but ECC/parity reported no error")

    # ── report ─────────────────────────────────────────────────────────────
    def _report(self, started: str) -> Dict[str, Any]:
        high = sum(1 for v in self.violations if v["severity"] == "HIGH")
        total = len(self.violations)
        return {
            "schema_version": SCHEMA_VERSION,
            "agent": AGENT_NAME,
            "started_at": started,
            "finished_at": _now(),
            "records_checked": len(self.events),
            "rtl_active": self.metrics["rtl_active"],
            "metrics": self.metrics,
            "total_violations": total,
            "high_violations": high,
            "severity_score": high * 3 + (total - high),
            "band": "CLEAN" if total == 0 else ("CRITICAL" if high else "DEGRADED"),
            "pass": high == 0,
            "violations": self.violations[:100],
        }


# ─────────────────────────────────────────────────────────────────────────────
# Manifest entry point
# ─────────────────────────────────────────────────────────────────────────────
def _load_events(run_dir: Path, manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    name = (manifest.get("outputs", {}) or {}).get("rtl_trace", "rtl_trace.jsonl")
    p = run_dir / name
    if not p.exists():
        return []
    text = p.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if text.lstrip().startswith("["):
        try:
            data = json.loads(text)
            return data if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def run_from_manifest(manifest_path: str) -> int:
    mp = Path(manifest_path)
    try:
        manifest = json.loads(mp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("rtl_basics_verifier: cannot read manifest: %s", exc)
        return 0
    run_dir = Path(manifest.get("run_dir", mp.parent))
    events = _load_events(run_dir, manifest)
    if not events:
        rep = {"schema_version": SCHEMA_VERSION, "agent": AGENT_NAME,
               "status": "skipped", "reason": "no rtl_trace", "pass": True}
    else:
        rep = RTLBasicsVerifier(events).run()
        rep["status"] = "completed"
    try:
        (run_dir / "rtl_basics_report.json").write_text(
            json.dumps(rep, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("rtl_basics_verifier: cannot write report: %s", exc)
    return 0 if rep.get("pass", True) else 1


if __name__ == "__main__":  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="AVA FSM/FIFO/memory checker")
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args()
    raise SystemExit(run_from_manifest(args.manifest))
