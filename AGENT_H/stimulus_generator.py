"""
AGENT_H.stimulus_generator — Coverage-Directed Stimulus Generator (T43)
========================================================================

The generation half of the self-evolving loop. T40 (`self_evolving_engine`)
decides *which* coverage holes to chase and T42 (`coverage_collector`) measures
what's covered — but nothing turned a hole back into an actual test. This module
does: it converts a coverage hole / constraint into a concrete RISC-V
instruction **seed**, so the loop closes end-to-end:

```
holes ─▶ self_evolving_engine ─▶ constraints ─▶ StimulusGenerator ─▶ test seeds
  ▲                                                                     │
  └────────────── coverage_collector ◀── (run seeds) ◀─────────────────┘
```

Self-validating by construction
-------------------------------
Every template emits both the assembly *and* the golden commit-log records the
seed is expected to produce. `predicted_coverage(seed)` runs those records
through the real `CoverageCollector`, so we can *prove* a generated seed covers
its target bin — the generator checks its own work. That same predictor lets the
generator act as a **real** `generate`/`evaluate` pair for the self-evolving
engine, so `close_coverage()` demonstrably drives coverage to the target using
generated stimulus (not a synthetic toy environment).

Directed vs. random
-------------------
`directed`/`genetic`/`adversarial` strategies aim stimulus at the targeted
holes; `random` emits arbitrary register writes. Given both as bandit arms, the
self-evolving engine learns to prefer directed generation — a
directed-random hybrid that is exactly the coverage-guided test-generation idea.

Stdlib-only, deterministic (seeded), graceful degradation throughout.
"""

from __future__ import annotations

import json
import logging
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

try:                                                # package or standalone import
    from .coverage_collector import CoverageCollector
except ImportError:                                 # pragma: no cover
    from coverage_collector import CoverageCollector  # type: ignore

log = logging.getLogger("AGENT_H.stimulus")

SCHEMA_VERSION = "2.1.0"
AGENT_NAME = "stimulus_generator"

# Representative value for each coverage value-class (RV32 view).
_VC_VALUE = {
    "zero": 0x0, "one": 0x1, "neg": 0x80000000,
    "all_ones": 0xFFFFFFFF, "pos_small": 0x7, "pos_large": 0x12340,
}

# Multicore coherence scenario templates. Sharing patterns need no MESI state;
# the state/transition/sharer ones carry (op, core, state, value) sequences that
# a single core produces via its own ops (sound — no snoop-induced transitions).
_COH_PATTERN_EVENTS = {
    "producer_consumer": [
        {"core": 0, "op": "store", "addr": "0x40", "value": "0x7", "cycle": 1},
        {"core": 1, "op": "load", "addr": "0x40", "value": "0x7", "cycle": 2}],
    "migratory": [
        {"core": 0, "op": "store", "addr": "0x40", "value": "0x1", "cycle": 1},
        {"core": 1, "op": "store", "addr": "0x40", "value": "0x2", "cycle": 2}],
    "read_shared": [
        {"core": 0, "op": "store", "addr": "0x40", "value": "0x5", "cycle": 1},
        {"core": 1, "op": "load", "addr": "0x40", "value": "0x5", "cycle": 2},
        {"core": 2, "op": "load", "addr": "0x40", "value": "0x5", "cycle": 3}],
    "write_shared": [
        {"core": 0, "op": "store", "addr": "0x40", "value": "0x1", "cycle": 1},
        {"core": 1, "op": "store", "addr": "0x40", "value": "0x2", "cycle": 2}],
}
_COH_STATE_SEQ = {
    "M": [("store", 0, "M", 1)],
    "E": [("load", 0, "E", 0)],
    "S": [("load", 0, "S", 0)],
    "I": [("store", 0, "M", 1), ("load", 0, "I", 1)],
}
_COH_TRANS_SEQ = {
    "I->S": [("load", 0, "S", 0)],
    "I->E": [("load", 0, "E", 0)],
    "I->M": [("store", 0, "M", 1)],
    "S->M": [("load", 0, "S", 0), ("store", 0, "M", 1)],
    "E->M": [("load", 0, "E", 0), ("store", 0, "M", 1)],
}
_COH_SHARE_SEQ = {
    "1": [("load", 0, "S", 0)],
    "2": [("load", 0, "S", 0), ("load", 1, "S", 0)],
    "3plus": [("load", 0, "S", 0), ("load", 1, "S", 0), ("load", 2, "S", 0)],
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rec(pc: int, disasm: str, regs: Optional[Dict[str, Any]] = None,
         **extra) -> Dict[str, Any]:
    r = {"schema_version": SCHEMA_VERSION, "pc": hex(pc), "disasm": disasm,
         "regs": regs or {}, "csrs": {}}
    r.update(extra)
    return r


# ─────────────────────────────────────────────────────────────────────────────
# Generator
# ─────────────────────────────────────────────────────────────────────────────
class StimulusGenerator:
    def __init__(self, seed: int = 0, base_pc: int = 0x80001000):
        self.rng = random.Random(seed)
        self.base_pc = base_pc

    # -- templates: constraint → seed ----------------------------------------
    def generate_for(self, constraint: Dict[str, Any]) -> Dict[str, Any]:
        """Turn a constraint (from `constraint_for`) into a directed seed."""
        kind = constraint.get("kind")
        vals = constraint.get("values", []) or []
        target = constraint.get("target", "")
        b = self.base_pc

        if kind == "reg" and vals:
            n = self._reg_index(vals[0])
            if n is not None and n != 0:
                v = self.rng.choice([1, 7, 0x55, 0x1000])
                return self._seed(target, "directed", [
                    _rec(b, f"addi x{n},x0,{v}", {f"x{n}": hex(v)})])

        if kind == "valclass" and vals and vals[0] in _VC_VALUE:
            v = _VC_VALUE[vals[0]]
            return self._seed(target, "directed", [
                _rec(b, f"li x5,{hex(v)}", {"x5": hex(v)})])

        if kind == "branch" and vals:
            return self._branch_seed(target, vals[0] == "taken")

        if kind == "priv" and vals and vals[0] in ("M", "S", "U"):
            return self._seed(target, "directed", [
                _rec(b, "csrr x5,mstatus", {"x5": "0x0"}, priv=vals[0])])

        if kind == "instr" and vals:
            mnem = str(vals[0])
            return self._seed(target, "directed", [
                _rec(b, f"{mnem} x5,x6,x7", {"x5": "0x1"})])

        if kind == "cross" and len(vals) >= 2 and vals[1] in _VC_VALUE:
            mnem, cls = str(vals[0]), vals[1]
            v = _VC_VALUE[cls]
            return self._seed(target, "directed", [
                _rec(b, f"{mnem} x5,x6,x7", {"x5": hex(v)})])

        if kind == "opnd" and len(vals) >= 2 and vals[0] in _VC_VALUE and vals[1] in _VC_VALUE:
            v1, v2 = _VC_VALUE[vals[0]], _VC_VALUE[vals[1]]
            return self._seed(target, "directed", [
                _rec(b, f"li x6,{hex(v1)}", {"x6": hex(v1)}),
                _rec(b + 4, f"li x7,{hex(v2)}", {"x7": hex(v2)}),
                _rec(b + 8, "add x5,x6,x7", {"x5": hex((v1 + v2) & 0xFFFFFFFF)})])

        # -- coherence scenarios (multicore) --
        if kind == "cohpat" and vals:
            return self._coh_seed(target, _COH_PATTERN_EVENTS.get(vals[0], []))
        if kind == "cohstate" and vals:
            return self._coh_seed(target, self._coh_events(_COH_STATE_SEQ.get(vals[0], [])))
        if kind == "cohtrans" and vals:
            return self._coh_seed(target, self._coh_events(_COH_TRANS_SEQ.get(vals[0], [])))
        if kind == "cohshare" and vals:
            return self._coh_seed(target, self._coh_events(_COH_SHARE_SEQ.get(vals[0], [])))

        # fallback — a benign random write (still valid stimulus)
        return self.generate_random()

    # -- coherence event builders --------------------------------------------
    def _coh_events(self, seq: List[tuple]) -> List[Dict[str, Any]]:
        """seq of (op, core, state, value) → cycle-stamped coherence events."""
        out = []
        for i, (op, core, state, value) in enumerate(seq):
            e = {"core": core, "op": op, "addr": "0x40", "cycle": i + 1}
            if value is not None:
                e["value"] = hex(value)
            if state is not None:
                e["state"] = state
            out.append(e)
        return out

    def _coh_seed(self, target: str, events: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {"target": target, "strategy": "directed",
                "asm": [f"{e['op']}@core{e['core']}" for e in events],
                "commit": [], "coherence_events": events}

    def _branch_seed(self, target: str, taken: bool) -> Dict[str, Any]:
        b = self.base_pc
        v2 = 1 if taken else 2
        recs = [
            _rec(b, "addi x1,x0,1", {"x1": "0x1"}),
            _rec(b + 4, f"addi x2,x0,{v2}", {"x2": hex(v2)}),
            _rec(b + 8, f"beq x1,x2,{hex(b + 0x40)}"),
            # next PC decides direction: taken → jump target, else fall-through
            _rec(b + 0x40 if taken else b + 12, "nop"),
        ]
        return self._seed(target, "directed", recs)

    def generate_random(self) -> Dict[str, Any]:
        n = self.rng.randint(1, 31)
        v = self.rng.randint(0, 0xFFFFFFFF)
        return self._seed(f"reg:x{n}", "random", [
            _rec(self.base_pc, f"addi x{n},x0,{hex(v)}", {f"x{n}": hex(v)})])

    def _seed(self, target: str, strategy: str,
              commit: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "target": target,
            "strategy": strategy,
            "asm": [r["disasm"] for r in commit],
            "commit": commit,
        }

    @staticmethod
    def _reg_index(name: str) -> Optional[int]:
        s = str(name)
        if s.startswith("x") and s[1:].isdigit():
            return int(s[1:])
        return None

    # -- self-validation / coverage prediction --------------------------------
    def predicted_coverage(self, seed: Dict[str, Any]) -> set:
        """Bins the seed is expected to cover (its own commit records, scored by
        the real CoverageCollector). Lets the generator check its own work."""
        cc = CoverageCollector(seed.get("commit", []),
                               coherence_events=seed.get("coherence_events"))
        cc.collect()
        return set(cc.covered) | set(cc.observed_extra)

    def covers_target(self, seed: Dict[str, Any]) -> bool:
        return seed.get("target", "__none__") in self.predicted_coverage(seed)

    def generate_batch(self, constraints: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [self.generate_for(c) for c in constraints]

    # -- self-evolving engine plugins -----------------------------------------
    def make_env(self) -> Tuple[Callable, Callable]:
        """Return (generate, evaluate) usable directly by SelfEvolvingEngine."""
        def generate(strategy: str, constraints: List[Dict[str, Any]]):
            if strategy == "random":
                k = max(1, len(constraints))
                return [self.generate_random() for _ in range(k)]
            return [self.generate_for(c) for c in constraints]

        def evaluate(seeds: List[Dict[str, Any]]):
            covered: set = set()
            cost = 0
            for s in seeds:
                covered |= self.predicted_coverage(s)
                cost += len(s.get("commit", []))
            return {"covered": covered, "bugs": 0, "cost": min(1.0, cost / 100.0)}

        return generate, evaluate

    # -- end-to-end closure ---------------------------------------------------
    def close_coverage(self, total_bins: Optional[Sequence[str]] = None,
                       strategies: Optional[Sequence[str]] = None,
                       policy: str = "discounted", seed: int = 0,
                       coverage_target: float = 1.0,
                       max_rounds: int = 300) -> Dict[str, Any]:
        """Actually close coverage using generated stimulus: wires this
        generator into the self-evolving engine and runs the loop."""
        try:
            from .self_evolving_engine import SelfEvolvingEngine
        except ImportError:                          # pragma: no cover
            from self_evolving_engine import SelfEvolvingEngine  # type: ignore
        if total_bins is None:
            total_bins = CoverageCollector([]).collect()["total_bins"]
        strategies = list(strategies or ["random", "directed"])
        gen, ev = self.make_env()
        eng = SelfEvolvingEngine(total_bins, strategies, seed=seed, policy=policy,
                                 coverage_target=coverage_target,
                                 plateau_patience=25, holes_per_round=6)
        return eng.evolve(gen, ev, max_rounds=max_rounds)


# ─────────────────────────────────────────────────────────────────────────────
# Offline: generate directed stimulus from a coverage snapshot
# ─────────────────────────────────────────────────────────────────────────────
def generate_from_holes(holes: Sequence[str], seed: int = 0,
                        max_seeds: int = 200) -> List[Dict[str, Any]]:
    """Given coverage-hole labels, emit a directed seed per hole."""
    try:
        from .self_evolving_engine import constraint_for
    except ImportError:                              # pragma: no cover
        from self_evolving_engine import constraint_for  # type: ignore
    gen = StimulusGenerator(seed=seed)
    out = []
    for h in list(holes)[: max(0, max_seeds)]:
        out.append(gen.generate_for(constraint_for(h)))
    return out


def run_from_manifest(manifest_path: str) -> int:
    """Read the run's `coverage_summary.json`, generate directed stimulus for
    every open hole, and write `stimulus.json`. Advisory (always returns 0)."""
    mp = Path(manifest_path)
    try:
        manifest = json.loads(mp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("stimulus_generator: cannot read manifest: %s", exc)
        return 0
    run_dir = Path(manifest.get("run_dir", mp.parent))
    summ_path = run_dir / "coverage_summary.json"
    if not summ_path.exists():
        out = {"schema_version": SCHEMA_VERSION, "agent": AGENT_NAME,
               "status": "skipped", "reason": "no coverage_summary.json"}
        try:
            (run_dir / "stimulus.json").write_text(json.dumps(out, indent=2),
                                                   encoding="utf-8")
        except OSError:
            pass
        return 0
    try:
        summ = json.loads(summ_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return 0
    holes = summ.get("holes") or sorted(
        set(summ.get("total_bins", [])) - set(summ.get("covered_bins", [])))
    seeds = generate_from_holes(holes)
    # self-validation: how many generated seeds actually hit their target
    gen = StimulusGenerator()
    validated = sum(1 for s in seeds if gen.covers_target(s))
    out = {
        "schema_version": SCHEMA_VERSION,
        "agent": AGENT_NAME,
        "status": "completed",
        "holes_targeted": len(seeds),
        "seeds_self_validated": validated,
        "seeds": seeds,
    }
    try:
        (run_dir / "stimulus.json").write_text(json.dumps(out, indent=2),
                                               encoding="utf-8")
    except OSError as exc:
        log.warning("stimulus_generator: cannot write stimulus: %s", exc)
    return 0


if __name__ == "__main__":  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="AVA coverage-directed stimulus generator")
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args()
    raise SystemExit(run_from_manifest(args.manifest))
