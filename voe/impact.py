"""Impact propagation — what a conclusion rests on, and what dies when it moves.

Until now the organisation was purely *reactive*: observe, reason, plan, verify.
It could tell you what it knew, but not what would stop being true if something
changed. A senior engineer thinks the other way round —

    "if this FIFO is wrong, every subsystem that assumed it is now suspect"
    "that RTL edit invalidates every proof that read those lines"
    "we relied on RV32BNone; if that changes, these proofs are void"

That is foresight, and it is a *graph* problem, not a planning problem.

**This is not a new kernel primitive — it is an obligation the foundation
already wrote down.** Attack-sheet item 3.1 states the requirement directly
("cross-commit safety ⟺ COI soundness: show a sound COI that still lets stale
evidence certify a post-commit bug"), and the kernel's Sem-2′ already reserves
exactly one class of event in which residual risk may legitimately *rise*:
`commit`. Retracting stale knowledge is that event. So this module discharges a
debt rather than extending the theory, and it needs no kernel change — a claim
the tests verify rather than assert.

Two dependency kinds, both transitive:

    sources      files whose CONTENT the claim depends on (hash-pinned)
    assumptions  named premises, which may be other properties

Invalidating either propagates to everything downstream.
"""
from __future__ import annotations
import hashlib, os
from collections import defaultdict
from dataclasses import dataclass, field


def _hash(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except OSError:
        return None


@dataclass(frozen=True)
class Basis:
    """What a judgment rests on. Empty basis = rests on nothing recorded."""
    sources: tuple = ()          # RTL/source files read to establish the claim
    assumptions: tuple = ()      # named premises, or other property ids


@dataclass
class ImpactReport:
    trigger: str
    directly_hit: list = field(default_factory=list)
    transitively_hit: list = field(default_factory=list)
    retracted: list = field(default_factory=list)
    risk_before: float = 0.0
    risk_after: float = 0.0

    @property
    def impacted(self):
        return list(dict.fromkeys(self.directly_hit + self.transitively_hit))


class ImpactGraph:
    """Dependency graph over evidence, assumptions, proofs and obligations."""

    def __init__(self):
        self.basis: dict[str, Basis] = {}
        self.source_hash: dict[str, str | None] = {}
        self._by_source: dict[str, set] = defaultdict(set)
        self._by_assumption: dict[str, set] = defaultdict(set)

    # -- recording ---------------------------------------------------------- #
    def record(self, phi: str, sources=(), assumptions=()):
        """Register what a claim rests on, pinning source content at this moment."""
        b = Basis(tuple(sources), tuple(assumptions))
        self.basis[phi] = b
        for s in b.sources:
            self._by_source[s].add(phi)
            self.source_hash.setdefault(s, _hash(s))
        for a in b.assumptions:
            self._by_assumption[a].add(phi)

    # -- queries ------------------------------------------------------------ #
    def changed_sources(self):
        """Source files whose content differs from when claims were recorded."""
        return [s for s, h in self.source_hash.items() if _hash(s) != h]

    def _closure(self, seed):
        """Everything that transitively depends on the seed set."""
        seen, frontier = set(), list(seed)
        while frontier:
            cur = frontier.pop()
            for phi in self._by_assumption.get(cur, ()):
                if phi not in seen:
                    seen.add(phi)
                    frontier.append(phi)      # a dependent may itself be a premise
        return seen

    def impacted_by_sources(self, files):
        direct = set()
        for f in files:
            direct |= self._by_source.get(f, set())
        return sorted(direct), sorted(self._closure(direct) - direct)

    def impacted_by_assumption(self, assumption: str):
        direct = set(self._by_assumption.get(assumption, ()))
        return sorted(direct), sorted(self._closure(direct) - direct)

    # -- propagation -------------------------------------------------------- #
    def propagate(self, ks, kern, weights, trigger: str,
                  files=None, assumption=None, retract=True) -> ImpactReport:
        """Invalidate stale knowledge and report the risk that comes back.

        `retract=True` REMOVES the affected judgments from the canonical state.
        Residual risk rises as a result, which is legal precisely because this is
        a `commit`-class event under Sem-2′ — the one case where the kernel
        permits risk to increase. Nothing else about the kernel is touched.
        """
        if assumption is not None:
            direct, trans = self.impacted_by_assumption(assumption)
        else:
            files = list(files) if files is not None else self.changed_sources()
            direct, trans = self.impacted_by_sources(files)

        rep = ImpactReport(trigger, direct, trans)
        rep.risk_before = kern.R(ks, weights)
        if retract:
            for phi in rep.impacted:
                if phi in ks.K:
                    del ks.K[phi]             # the claim no longer has standing
                    rep.retracted.append(phi)
        rep.risk_after = kern.R(ks, weights)
        # re-pin the new content so the next commit is measured from here
        for s in (files or []):
            if s in self.source_hash:
                self.source_hash[s] = _hash(s)
        return rep

    # -- reporting ---------------------------------------------------------- #
    def explain(self, phi: str) -> str:
        b = self.basis.get(phi)
        if not b:
            return f"{phi}: no recorded basis"
        return (f"{phi} rests on "
                f"{len(b.sources)} source(s) {list(b.sources)} and "
                f"{len(b.assumptions)} assumption(s) {list(b.assumptions)}")

    def summary(self, rep: ImpactReport) -> str:
        d = (f"  trigger: {rep.trigger}\n"
             f"  directly invalidated : {rep.directly_hit or '—'}\n"
             f"  transitively suspect : {rep.transitively_hit or '—'}\n"
             f"  retracted            : {rep.retracted or '—'}\n"
             f"  risk {rep.risk_before:.3f} -> {rep.risk_after:.3f}")
        return d
