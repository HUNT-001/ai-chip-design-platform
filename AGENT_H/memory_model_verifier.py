"""
AGENT_H.memory_model_verifier — Memory-Consistency Checker (T45)
=================================================================

Axiomatic verification of a **memory-consistency model** (SC / TSO / RVWMO) from
a multicore execution. This is the verification level *above* cache coherence:
coherence governs a single location; consistency governs the ordering of
accesses *across* locations — exactly the class of bugs (a missing fence, an
illegal store→load or load→load reordering) that are the hardest to find and the
most severe.

Method — the standard axiomatic ("herd"-style) approach
-------------------------------------------------------
An observed execution is described by relations over its memory operations:

    po    program order (per core)
    ppo   *preserved* program order — the po pairs the model keeps ordered
    rf    reads-from (each load ← the store it observed); rfe = external rf
    co    coherence order (the total order of stores to each address)
    fr    from-read: r ─fr→ w' when r reads a store co-before w'

The execution is **permitted by the model** iff two relations are acyclic:

    sc-per-location :  acyclic( po-loc ∪ rf ∪ co ∪ fr )      (coherence)
    global order    :  acyclic( ppo ∪ fence ∪ rfe ∪ co ∪ fr ) (the model)

A **cycle means the hardware exhibited an ordering the model forbids** — a real
consistency bug — reported HIGH with the offending cycle as a witness.

`ppo` is where the models differ:
    sc     — all of po is preserved.
    tso    — all of po *except* store→load (the store-buffer relaxation).
    rvwmo  — po preserved only for same-address pairs or a syntactic dependency;
             otherwise ordering must come from an explicit fence.
Fences (`op="fence"`) order every earlier op in that core before every later one.

Validated against the canonical litmus tests: SB (allowed under TSO, forbidden
under SC, forbidden under TSO once fenced), MP and LB (forbidden reorderings),
and coherence (CoRR) via the sc-per-location axiom.

Additive execution-trace contract
----------------------------------
```
each op: {"core":0, "op":"load"|"store"|"fence", "addr":"0x40", "value":"0x1",
          "cycle":3,     # optional: global order → derives co
          "rf": 5,       # optional: index of the store this load read (else inferred)
          "co": 0,       # optional: this store's coherence rank for its address
          "deps": [2]}   # optional: op indices this op has a syntactic dep on
```

Stdlib-only, schema-v2.1.0 report, graceful degradation throughout.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

log = logging.getLogger("AGENT_H.memory_model")

SCHEMA_VERSION = "2.1.0"
AGENT_NAME = "memory_model_verifier"
_MODELS = ("sc", "tso", "rvwmo")


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


def _norm_op(op: Any) -> Optional[str]:
    s = str(op).lower()
    if s in ("load", "ld", "read", "r"):
        return "load"
    if s in ("store", "st", "write", "w"):
        return "store"
    if s in ("fence", "mfence", "membar", "barrier"):
        return "fence"
    return None


_MM_PAIRS = ["store_load", "load_load", "store_store", "load_store"]
_MM_SYNC = ["fence", "aq", "rl", "rmw"]


def consistency_coverage_bins(execution: Sequence[Dict[str, Any]]) -> set:
    """Coverage of memory-ordering *mechanisms* exercised by an execution:
    which po pair types appear (reordering opportunities) and which
    synchronisation features are used (fence / acquire / release / RMW)."""
    covered: set = set()
    by_core: Dict[Any, List[Dict[str, Any]]] = {}
    for e in execution or []:
        if not isinstance(e, dict):
            continue
        k = _norm_op(e.get("op"))
        if k is None:
            continue
        by_core.setdefault(e.get("core"), []).append({"k": k, "e": e})
        if k == "fence":
            covered.add("mmsync:fence")
        if e.get("aq"):
            covered.add("mmsync:aq")
        if e.get("rl"):
            covered.add("mmsync:rl")
        if e.get("rmw") is not None:
            covered.add("mmsync:rmw")
    for seq in by_core.values():
        mem = [o["k"] for o in seq if o["k"] in ("load", "store")]
        for i in range(len(mem)):
            for j in range(i + 1, len(mem)):
                covered.add(f"mmpair:{mem[i]}_{mem[j]}")
    return covered


def consistency_universe() -> Dict[str, float]:
    uni = {f"mmpair:{p}": 2.0 for p in _MM_PAIRS}
    uni.update({f"mmsync:{s}": 3.0 for s in _MM_SYNC})
    return uni


@dataclass
class Op:
    idx: int
    core: Any
    kind: str                      # load | store | fence
    addr: Optional[int] = None
    value: Optional[int] = None
    cycle: Optional[int] = None
    co: Optional[int] = None
    rf: Optional[int] = None       # explicit reads-from (store idx)
    deps: Set[int] = field(default_factory=set)
    aq: bool = False               # acquire annotation (.aq)
    rl: bool = False               # release annotation (.rl)
    pred: Optional[Set[str]] = None  # fence predecessor set {"r","w"}
    succ: Optional[Set[str]] = None  # fence successor set {"r","w"}
    rmw: Any = None                # atomic RMW group id (lr/sc, amo)


def _letter(o: "Op") -> str:
    return "r" if o.kind == "load" else "w"


def _letter_set(v: Any) -> Optional[Set[str]]:
    if v is None:
        return None
    if isinstance(v, str):
        return set(c for c in v.lower() if c in ("r", "w"))
    if isinstance(v, (list, tuple, set)):
        return set(str(x).lower() for x in v if str(x).lower() in ("r", "w"))
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Cycle detection
# ─────────────────────────────────────────────────────────────────────────────
def find_cycle(nodes: Sequence[int], edges: Dict[int, Set[int]]) -> Optional[List[int]]:
    """Return a cycle (list of node ids) in the directed graph, or None."""
    WHITE, GREY, BLACK = 0, 1, 2
    color = {n: WHITE for n in nodes}
    parent: Dict[int, int] = {}

    def dfs(u: int) -> Optional[List[int]]:
        color[u] = GREY
        for v in edges.get(u, ()):  # noqa
            if v not in color:
                continue
            if color[v] == GREY:                     # back-edge → cycle
                cyc = [v, u]
                x = u
                while parent.get(x) is not None and parent[x] != v:
                    x = parent[x]
                    cyc.append(x)
                cyc.reverse()
                return cyc
            if color[v] == WHITE:
                parent[v] = u
                r = dfs(v)
                if r:
                    return r
        color[u] = BLACK
        return None

    for n in nodes:
        if color[n] == WHITE:
            r = dfs(n)
            if r:
                return r
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Verifier
# ─────────────────────────────────────────────────────────────────────────────
class MemoryModelVerifier:
    def __init__(self, execution: Sequence[Dict[str, Any]], model: str = "tso"):
        self.model = model.lower() if model else "tso"
        if self.model not in _MODELS:
            self.model = "tso"
        self.ops: List[Op] = []
        for i, e in enumerate(execution or []):
            if not isinstance(e, dict):
                continue
            kind = _norm_op(e.get("op"))
            if kind is None:
                continue
            self.ops.append(Op(
                idx=len(self.ops), core=e.get("core"), kind=kind,
                addr=_to_int(e.get("addr")), value=_to_int(e.get("value")),
                cycle=_to_int(e.get("cycle")), co=_to_int(e.get("co")),
                rf=_to_int(e.get("rf")),
                deps=set(d for d in (e.get("deps") or []) if isinstance(d, int)),
                aq=bool(e.get("aq", False)), rl=bool(e.get("rl", False)),
                pred=_letter_set(e.get("pred")), succ=_letter_set(e.get("succ")),
                rmw=e.get("rmw"),
            ))
        self.violations: List[Dict[str, Any]] = []
        self.mem = [o for o in self.ops if o.kind in ("load", "store")]

    # -- per-core program order ----------------------------------------------
    def _by_core(self) -> Dict[Any, List[Op]]:
        cores: Dict[Any, List[Op]] = {}
        for o in self.ops:
            cores.setdefault(o.core, []).append(o)
        return cores            # preserves list (program) order per core

    # -- coherence order per address -----------------------------------------
    def _coherence_order(self) -> Dict[int, List[Op]]:
        by_addr: Dict[int, List[Op]] = {}
        for o in self.mem:
            if o.kind == "store" and o.addr is not None:
                by_addr.setdefault(o.addr, []).append(o)
        for addr, stores in by_addr.items():
            def key(s: Op):
                if s.co is not None:
                    return (0, s.co)
                if s.cycle is not None:
                    return (1, s.cycle)
                return (2, s.idx)
            stores.sort(key=key)
        return by_addr

    # -- reads-from -----------------------------------------------------------
    def _reads_from(self, co: Dict[int, List[Op]]) -> Dict[int, Optional[int]]:
        rf: Dict[int, Optional[int]] = {}
        for o in self.mem:
            if o.kind != "load" or o.addr is None:
                continue
            if o.rf is not None:
                rf[o.idx] = o.rf
                continue
            stores = co.get(o.addr, [])
            matches = [s for s in stores if s.value == o.value]
            if matches:
                rf[o.idx] = matches[-1].idx        # co-latest store of that value
            else:
                rf[o.idx] = None                    # reads the initial value
        return rf

    # -- edge-set builders ----------------------------------------------------
    def _ppo_edges(self) -> Set[Tuple[int, int]]:
        edges: Set[Tuple[int, int]] = set()
        for seq in self._by_core().values():
            mem_seq = [o for o in seq]
            for i in range(len(mem_seq)):
                a = mem_seq[i]
                if a.kind == "fence":
                    continue
                for j in range(i + 1, len(mem_seq)):
                    b = mem_seq[j]
                    if b.kind == "fence":
                        continue
                    if self._preserved(a, b):
                        edges.add((a.idx, b.idx))
        return edges

    def _preserved(self, a: Op, b: Op) -> bool:
        if self.model == "sc":
            return True
        if self.model == "tso":
            return not (a.kind == "store" and b.kind == "load")
        # rvwmo: same-address pair, or a syntactic dependency b←a
        if a.addr is not None and a.addr == b.addr:
            return True
        return a.idx in b.deps

    def _fence_edges(self) -> Set[Tuple[int, int]]:
        edges: Set[Tuple[int, int]] = set()
        for seq in self._by_core().values():
            for k, o in enumerate(seq):
                if o.kind != "fence":
                    continue
                pred = o.pred if o.pred is not None else {"r", "w"}
                succ = o.succ if o.succ is not None else {"r", "w"}
                before = [seq[i] for i in range(k) if seq[i].kind in ("load", "store")]
                after = [seq[j] for j in range(k + 1, len(seq))
                         if seq[j].kind in ("load", "store")]
                for a in before:
                    if _letter(a) not in pred:
                        continue
                    for b in after:
                        if _letter(b) in succ:
                            edges.add((a.idx, b.idx))
        return edges

    def _annotation_edges(self) -> Set[Tuple[int, int]]:
        """Acquire/release (RCsc) ordering: an acquire is ordered before every
        later op in po; every earlier op is ordered before a release."""
        edges: Set[Tuple[int, int]] = set()
        for seq in self._by_core().values():
            mem = [o for o in seq if o.kind in ("load", "store")]
            for i, a in enumerate(mem):
                if a.aq:                              # acquire → all later
                    for b in mem[i + 1:]:
                        edges.add((a.idx, b.idx))
                if a.rl:                              # all earlier → release
                    for b in mem[:i]:
                        edges.add((b.idx, a.idx))
        return edges

    def _co_edges(self, co: Dict[int, List[Op]]) -> Set[Tuple[int, int]]:
        edges: Set[Tuple[int, int]] = set()
        for stores in co.values():
            for i in range(len(stores)):
                for j in range(i + 1, len(stores)):
                    edges.add((stores[i].idx, stores[j].idx))
        return edges

    def _fr_edges(self, co: Dict[int, List[Op]],
                  rf: Dict[int, Optional[int]]) -> Set[Tuple[int, int]]:
        edges: Set[Tuple[int, int]] = set()
        idx2op = {o.idx: o for o in self.mem}
        for lid, wid in rf.items():
            load = idx2op[lid]
            stores = co.get(load.addr, [])
            if wid is None:                          # read initial → before all stores
                for s in stores:
                    edges.add((lid, s.idx))
            else:
                pos = next((k for k, s in enumerate(stores) if s.idx == wid), None)
                if pos is not None:
                    for s in stores[pos + 1:]:
                        edges.add((lid, s.idx))
        return edges

    def _rf_edges(self, rf: Dict[int, Optional[int]], external_only: bool):
        edges: Set[Tuple[int, int]] = set()
        idx2op = {o.idx: o for o in self.mem}
        for lid, wid in rf.items():
            if wid is None or wid not in idx2op:
                continue
            if external_only and idx2op[wid].core == idx2op[lid].core:
                continue
            edges.add((wid, lid))
        return edges

    def _po_loc_edges(self) -> Set[Tuple[int, int]]:
        edges: Set[Tuple[int, int]] = set()
        for seq in self._by_core().values():
            mem = [o for o in seq if o.kind in ("load", "store")]
            for i in range(len(mem)):
                for j in range(i + 1, len(mem)):
                    if mem[i].addr is not None and mem[i].addr == mem[j].addr:
                        edges.add((mem[i].idx, mem[j].idx))
        return edges

    # -- driver ---------------------------------------------------------------
    def run(self) -> Dict[str, Any]:
        started = _now()
        co = self._coherence_order()
        rf = self._reads_from(co)
        co_e = self._co_edges(co)
        fr_e = self._fr_edges(co, rf)
        node_ids = [o.idx for o in self.mem]

        # Axiom 1 — sc-per-location (coherence)
        sc_edges = self._po_loc_edges() | self._rf_edges(rf, external_only=False) \
            | co_e | fr_e
        self._check_axiom("sc_per_location", node_ids, sc_edges,
                          "coherence (sc-per-location) cycle")

        # Axiom 2 — global order under the chosen model (+ aq/rl annotations)
        ghb_edges = (self._ppo_edges() | self._fence_edges() | self._annotation_edges()
                     | self._rf_edges(rf, external_only=True) | co_e | fr_e)
        self._check_axiom(f"consistency_{self.model}", node_ids, ghb_edges,
                          f"{self.model.upper()} global-order cycle")

        # Axiom 3 — RMW atomicity (no store interposed within an atomic RMW)
        self._check_rmw_atomicity(co, rf)

        return self._report(started, rf)

    def _check_rmw_atomicity(self, co: Dict[int, List[Op]],
                             rf: Dict[int, Optional[int]]) -> None:
        groups: Dict[Any, List[Op]] = {}
        for o in self.mem:
            if o.rmw is not None:
                groups.setdefault(o.rmw, []).append(o)
        for gid, ops in groups.items():
            load = next((o for o in ops if o.kind == "load"), None)
            store = next((o for o in ops if o.kind == "store"), None)
            if load is None or store is None or store.addr is None:
                continue
            stores = co.get(store.addr, [])
            ps = next((k for k, s in enumerate(stores) if s.idx == store.idx), None)
            if ps is None:
                continue
            wid = rf.get(load.idx)                    # store the RMW-load observed
            pr = next((k for k, s in enumerate(stores) if s.idx == wid), -1) \
                if wid is not None else -1
            interposed = [stores[k] for k in range(pr + 1, ps)]
            if interposed:
                self.violations.append({
                    "check": "rmw_atomicity", "severity": "HIGH",
                    "detail": f"RMW group {gid} @ {hex(store.addr)}: "
                              f"{len(interposed)} store(s) interposed between its "
                              f"read and write — atomicity broken",
                    "cycle": [self._op_str(load.idx)]
                             + [self._op_str(s.idx) for s in interposed]
                             + [self._op_str(store.idx)],
                })

    def _check_axiom(self, name: str, nodes: List[int],
                     edge_pairs: Set[Tuple[int, int]], detail: str) -> None:
        adj: Dict[int, Set[int]] = {n: set() for n in nodes}
        for u, v in edge_pairs:
            if u in adj and v in adj:
                adj[u].add(v)
        cyc = find_cycle(nodes, adj)
        if cyc:
            witness = [self._op_str(i) for i in cyc]
            self.violations.append({
                "check": name, "severity": "HIGH",
                "detail": f"{detail}: execution not permitted by the model",
                "cycle": witness,
            })

    def _op_str(self, idx: int) -> str:
        o = next((x for x in self.mem if x.idx == idx), None)
        if o is None:
            return f"#{idx}"
        v = "" if o.value is None else f"={hex(o.value)}"
        a = "" if o.addr is None else hex(o.addr)
        return f"c{o.core}:{o.kind[0].upper()}{a}{v}"

    def _report(self, started: str, rf: Dict[int, Optional[int]]) -> Dict[str, Any]:
        total = len(self.violations)
        high = sum(1 for v in self.violations if v["severity"] == "HIGH")
        cores = {o.core for o in self.mem}
        addrs = {o.addr for o in self.mem if o.addr is not None}
        return {
            "schema_version": SCHEMA_VERSION,
            "agent": AGENT_NAME,
            "started_at": started,
            "finished_at": _now(),
            "model": self.model,
            "records_checked": len(self.ops),
            "consistency_active": len(cores) > 1,
            "metrics": {
                "cores": len(cores), "memory_ops": len(self.mem),
                "addresses": len(addrs),
                "fences": sum(1 for o in self.ops if o.kind == "fence"),
                "loads": sum(1 for o in self.mem if o.kind == "load"),
                "stores": sum(1 for o in self.mem if o.kind == "store"),
            },
            "total_violations": total,
            "high_violations": high,
            "severity_score": high * 3,
            "band": "CLEAN" if total == 0 else "CRITICAL",
            "pass": high == 0,
            "violations": self.violations[:50],
        }


# ─────────────────────────────────────────────────────────────────────────────
# Manifest entry point
# ─────────────────────────────────────────────────────────────────────────────
def _load_execution(run_dir: Path, manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    name = (manifest.get("outputs", {}) or {}).get("consistency_trace",
                                                    "consistency_trace.jsonl")
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
    """Load a multicore execution trace, check it against the configured memory
    model, write ``memory_model_report.json``. 0 on pass/skip, 1 on violation."""
    mp = Path(manifest_path)
    try:
        manifest = json.loads(mp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("memory_model_verifier: cannot read manifest: %s", exc)
        return 0
    run_dir = Path(manifest.get("run_dir", mp.parent))
    execution = _load_execution(run_dir, manifest)
    model = manifest.get("memory_model", "tso")
    if not execution:
        rep = {"schema_version": SCHEMA_VERSION, "agent": AGENT_NAME,
               "status": "skipped", "reason": "no consistency_trace", "pass": True}
    else:
        rep = MemoryModelVerifier(execution, model=model).run()
        rep["status"] = "completed"
    try:
        (run_dir / "memory_model_report.json").write_text(
            json.dumps(rep, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("memory_model_verifier: cannot write report: %s", exc)
    return 0 if rep.get("pass", True) else 1


if __name__ == "__main__":  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="AVA memory-consistency checker")
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args()
    raise SystemExit(run_from_manifest(args.manifest))
