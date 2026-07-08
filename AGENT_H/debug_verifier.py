"""
AGENT_H.debug_verifier — Debug & Trigger Module Checker (T48)
==============================================================

Golden checker for the RISC-V **Debug** spec and the **Trigger** (hardware
breakpoint/watchpoint) module. Debug bugs — a trigger that fires on the wrong
address, a breakpoint that reports the wrong `dcsr.cause`, a single-step that
runs two instructions, an abstract command that reads a stale register — are
severe (they break every debugger and every self-hosted bring-up flow) and are
not covered by the trap/privilege agents.

Trigger module (mcontrol) — golden match
-----------------------------------------
A trigger fires on an access iff its access-type is enabled (execute / load /
store), the current privilege is enabled (m/s/u), and the compare value
`tdata2` matches the accessed address (execute ⇒ PC, load/store ⇒ data address).

- **trigger_missed** (HIGH) — the condition matched but the DUT did not fire.
- **trigger_spurious** (HIGH) — the DUT fired with no matching enabled trigger.
- **trigger_cause** (HIGH) — a fire that enters debug must set `dcsr.cause = 2`.

Debug mode entry / step
-----------------------
- **debug_cause** (HIGH) — `dcsr.cause` reflects the source: `ebreak=1`,
  `trigger=2`, `haltreq=3`, `step=4`, `resethaltreq=5`.
- **debug_dpc** (HIGH) — `dpc` holds the PC of the halted instruction.
- **step_count** (HIGH) — a single-step executes **exactly one** instruction.

Abstract commands
-----------------
- **abstract_nothalted** (HIGH) — an abstract command must only run while the
  hart is halted.
- **abstract_result** (HIGH) — an access-register read returns the register's
  actual value.

Additive `debug_trace.jsonl` contract
-------------------------------------
```
{"op":"trigger_config","index":0,"execute":true,"tdata2":"0x80000040",
 "action":1,"priv":["M"]}
{"op":"exec","pc":"0x80000040","priv":"M","fired":true,"dcsr_cause":2}
{"op":"load","addr":"0x2000","pc":"0x80000010","priv":"M","fired":false}
{"op":"halt","cause":"haltreq","dpc":"0x80000010","dcsr_cause":3}
{"op":"step","instrs_executed":1}
{"op":"resume"}
{"op":"abstract","cmd":"access_reg","regno":10,"halted":true,"result":"0x5",
 "expected":"0x5"}
```

Stdlib-only, schema-v2.1.0 report, graceful degradation throughout.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

log = logging.getLogger("AGENT_H.debug")

SCHEMA_VERSION = "2.1.0"
AGENT_NAME = "debug_verifier"
_DCSR_CAUSE = {"ebreak": 1, "trigger": 2, "haltreq": 3, "step": 4,
               "resethaltreq": 5}


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


class Trigger:
    def __init__(self, cfg: Dict[str, Any]):
        t1 = cfg.get("tdata1", {}) if isinstance(cfg.get("tdata1"), dict) else {}
        def g(k, d=False):
            return bool(cfg.get(k, t1.get(k, d)))
        self.execute = g("execute")
        self.load = g("load")
        self.store = g("store")
        self.action = _to_int(cfg.get("action", t1.get("action", 0))) or 0
        priv = cfg.get("priv", t1.get("priv"))
        if priv is None:
            self.priv = {"M", "S", "U"}
        else:
            self.priv = set(str(p).upper() for p in priv)
        self.tdata2 = _to_int(cfg.get("tdata2"))
        self.enabled = self.execute or self.load or self.store

    def fires(self, kind: str, addr: Optional[int], priv: str) -> bool:
        if not self.enabled or self.tdata2 is None or addr is None:
            return False
        if priv and priv.upper() not in self.priv:
            return False
        type_ok = (kind == "exec" and self.execute) or \
                  (kind == "load" and self.load) or \
                  (kind == "store" and self.store)
        return type_ok and addr == self.tdata2


class DebugVerifier:
    def __init__(self, events: Sequence[Dict[str, Any]]):
        self.events = [e for e in (events or []) if isinstance(e, dict)]
        self.violations: List[Dict[str, Any]] = []
        self.metrics = {"triggers": 0, "fires": 0, "halts": 0, "steps": 0,
                        "abstract_cmds": 0, "debug_active": False}

    def _v(self, i: int, check: str, detail: str) -> None:
        self.violations.append({"event": i, "check": check,
                                "severity": "HIGH", "detail": detail})

    def run(self) -> Dict[str, Any]:
        started = _now()
        triggers: Dict[int, Trigger] = {}
        halted = False
        for i, e in enumerate(self.events):
            op = str(e.get("op", "")).lower()
            if op == "trigger_config":
                idx = _to_int(e.get("index")) or 0
                triggers[idx] = Trigger(e)
                self.metrics["triggers"] = len(triggers)
                self.metrics["debug_active"] = True
            elif op in ("exec", "load", "store"):
                self._check_access(i, e, op, triggers)
            elif op == "halt":
                halted = True
                self.metrics["halts"] += 1
                self.metrics["debug_active"] = True
                self._check_halt(i, e)
            elif op == "step":
                self.metrics["steps"] += 1
                n = _to_int(e.get("instrs_executed"))
                if n is not None and n != 1:
                    self._v(i, "step_count",
                            f"single-step executed {n} instructions (must be 1)")
            elif op == "resume":
                halted = False
            elif op == "abstract":
                self._check_abstract(i, e, halted)
        return self._report(started)

    def _check_access(self, i: int, e: Dict[str, Any], kind: str,
                      triggers: Dict[int, Trigger]) -> None:
        addr = _to_int(e.get("pc")) if kind == "exec" else _to_int(e.get("addr"))
        priv = str(e.get("priv", "M"))
        golden = any(t.fires(kind, addr, priv) for t in triggers.values())
        fired = bool(e.get("fired", False))
        if fired:
            self.metrics["fires"] += 1
        if golden and not fired:
            self._v(i, "trigger_missed",
                    f"{kind} @ {hex(addr) if addr is not None else '?'} matched a "
                    f"trigger but did not fire")
        elif fired and not golden:
            self._v(i, "trigger_spurious",
                    f"{kind} fired with no matching enabled trigger for priv {priv}")
        if fired and golden:
            # a fire entering debug (action=1) must set dcsr.cause = trigger(2)
            enters = any(t.fires(kind, addr, priv) and t.action == 1
                         for t in triggers.values())
            dc = _to_int(e.get("dcsr_cause"))
            if enters and dc is not None and dc != _DCSR_CAUSE["trigger"]:
                self._v(i, "trigger_cause",
                        f"trigger entered debug but dcsr.cause={dc} (expected 2)")

    def _check_halt(self, i: int, e: Dict[str, Any]) -> None:
        cause = str(e.get("cause", "haltreq")).lower()
        golden = _DCSR_CAUSE.get(cause)
        dc = _to_int(e.get("dcsr_cause"))
        if golden is not None and dc is not None and dc != golden:
            self._v(i, "debug_cause",
                    f"halt cause '{cause}' → dcsr.cause should be {golden}, got {dc}")
        dpc = _to_int(e.get("dpc"))
        pc = _to_int(e.get("pc"))
        if dpc is not None and pc is not None and dpc != pc:
            self._v(i, "debug_dpc", f"dpc={hex(dpc)} != halted PC {hex(pc)}")

    def _check_abstract(self, i: int, e: Dict[str, Any], halted: bool) -> None:
        self.metrics["abstract_cmds"] += 1
        self.metrics["debug_active"] = True
        is_halted = bool(e.get("halted", halted))
        executed = e.get("cmderr", 0) in (0, None) and e.get("result") is not None
        if not is_halted and executed:
            self._v(i, "abstract_nothalted",
                    "abstract command executed while hart not halted")
        res, exp = _to_int(e.get("result")), _to_int(e.get("expected"))
        if is_halted and res is not None and exp is not None and res != exp:
            self._v(i, "abstract_result",
                    f"access-register read {hex(res)} != expected {hex(exp)}")

    def _report(self, started: str) -> Dict[str, Any]:
        total = len(self.violations)
        return {
            "schema_version": SCHEMA_VERSION,
            "agent": AGENT_NAME,
            "started_at": started,
            "finished_at": _now(),
            "records_checked": len(self.events),
            "debug_active": self.metrics["debug_active"],
            "metrics": self.metrics,
            "total_violations": total,
            "high_violations": total,
            "severity_score": total * 3,
            "band": "CLEAN" if total == 0 else "CRITICAL",
            "pass": total == 0,
            "violations": self.violations[:100],
        }


# ─────────────────────────────────────────────────────────────────────────────
# Manifest entry point
# ─────────────────────────────────────────────────────────────────────────────
def _load_events(run_dir: Path, manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    name = (manifest.get("outputs", {}) or {}).get("debug_trace", "debug_trace.jsonl")
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
        log.warning("debug_verifier: cannot read manifest: %s", exc)
        return 0
    run_dir = Path(manifest.get("run_dir", mp.parent))
    events = _load_events(run_dir, manifest)
    if not events:
        rep = {"schema_version": SCHEMA_VERSION, "agent": AGENT_NAME,
               "status": "skipped", "reason": "no debug_trace", "pass": True}
    else:
        rep = DebugVerifier(events).run()
        rep["status"] = "completed"
    try:
        (run_dir / "debug_report.json").write_text(
            json.dumps(rep, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("debug_verifier: cannot write report: %s", exc)
    return 0 if rep.get("pass", True) else 1


if __name__ == "__main__":  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="AVA debug & trigger checker")
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args()
    raise SystemExit(run_from_manifest(args.manifest))
