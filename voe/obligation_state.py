"""Obligation-level state — the correction Experiment 2 earned.

Experiment 2 showed the adaptive policy could shift allocation BETWEEN designs
but not WITHIN one. On the multiplier board formal refuted one obligation in
seconds and timed out on the other; a single per-channel rate cannot represent
that, so the policy wasted most of its budget while still "winning".

The fix is not a design-level regime label — it would not help there either,
because both obligations live in the same design. Conditioning has to be per
OBLIGATION:

    pi(a | Omega_i)   rather than   pi(a | G(D))

**The transfer problem.** Each obligation is attempted once, so there is nothing
to learn from repeating it. A per-obligation table would be useless. The policy
must therefore condition on FEATURES that recur across obligations, so what was
learned on one carries to an unseen one with a similar signature:

    "equivalence property over a multiplier"     -> formal has timed out before
    "invariant over shallow control logic"       -> formal closed it cheaply

That is what an experienced engineer means by "formal is usually effective for
this kind of property under these structural conditions" — not "formal is good".

Features are deliberately cheap and observable. Nothing here inspects the answer
it is trying to predict, and nothing here can certify anything: this module only
influences WHICH action is tried, never what the evidence is taken to show.
"""
from __future__ import annotations
import os, re, sys
from dataclasses import dataclass, field

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


@dataclass(frozen=True)
class ObligationFeatures:
    """Omega_i — the recurring, observable context of one obligation.

    kind        what is being established (equivalence / invariant / tie_off)
    arithmetic  does the cone contain multiplication? (the classic solver wall)
    sequential  does the property span state, or is it combinational?
    depth_class structural size band of the design under it
    """
    kind: str = "invariant"
    arithmetic: bool = False
    sequential: bool = False
    depth_class: str = "small"

    def signature(self) -> tuple:
        """The key learning transfers over. Coarse ON PURPOSE — too fine a
        signature never matches an unseen obligation and the policy degenerates
        to its global prior."""
        return (self.kind, self.arithmetic, self.sequential)

    def describe(self) -> str:
        bits = [self.kind]
        if self.arithmetic: bits.append("arith")
        bits.append("seq" if self.sequential else "comb")
        bits.append(self.depth_class)
        return "/".join(bits)


# A multiply used as an OPERATOR: an operand, '*', an operand. Comments are
# stripped first — an earlier version scanned raw source and matched the '*' in
# every `/* ... */` block, labelling comment-heavy combinational RTL as
# arithmetic. `**` (power) and `(* attr *)` are excluded.
_MUL = re.compile(r"([\w\.\']+|\)|\])\s*\*(?!\*)\s*([\w\.\']+|\()")
_NUMERIC = re.compile(r"^\d+$|^\d+'[bhd]", re.I)


def _strip_selects(src: str) -> str:
    """Remove bit-select / index expressions `[...]`.

    Index arithmetic lives inside brackets (`x[i*4 +: 3]`,
    `y[2*N(stg)*(seg+1)-1 : ...]`); a datapath multiply appears in the body of
    an assignment. Without this, ibex_alu's XPERM and butterfly index maths made
    it look arithmetic-heavy when it contains no multiplier at all.
    """
    out, depth = [], 0
    for ch in src:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def _has_datapath_multiply(src: str) -> bool:
    """True only for VARIABLE x VARIABLE multiplication outside an index.

    `i*4`, `2*WIDTH` are constant strides; anything inside `[...]` is indexing.
    Both elaborate away and tell the solver nothing. The feature exists to
    predict SOLVER COST, and only a datapath multiply builds the bit-blast wall
    that makes formal expensive — so a loose definition is a silent
    mis-generalisation, not a cosmetic error.
    """
    body = _strip_selects(src)
    for a, b in _MUL.findall(body):
        if not _NUMERIC.match(a.strip()) and not _NUMERIC.match(b.strip()):
            return True
    return False
# Genuinely clocked logic. always_comb is NOT sequential — counting every
# always block (as an earlier version did via rtl_graph's always_blocks) marked
# purely combinational designs like ibex_alu as stateful.
_SEQ = re.compile(r"\balways_ff\b|\balways_latch\b|"
                  r"\balways\s*@\s*\(\s*(posedge|negedge)\b")


def probe_structure(rtl_path: str | None):
    """Cheap structural read of the design a property sits over.

    Returns (arithmetic, sequential, depth_class). Comments are stripped before
    any pattern is applied — these features condition the policy, so a false
    feature is a silent mis-generalisation, not a cosmetic error.
    """
    if not rtl_path or not os.path.exists(rtl_path):
        return False, False, "small"
    try:
        with open(rtl_path, errors="ignore") as f:
            raw = f.read()
    except OSError:
        return False, False, "small"
    try:
        from AGENT_H import rtl_graph as rg
        src = rg.strip_comments(raw)
    except Exception:
        src = re.sub(r"/\*.*?\*/", " ", raw, flags=re.S)
        src = re.sub(r"//[^\n]*", " ", src)
    arithmetic = _has_datapath_multiply(src)
    sequential = bool(_SEQ.search(src))
    signals = 0
    try:
        from AGENT_H import rtl_graph as rg
        mods = rg.parse_module(raw, rtl_path)
        if mods:
            signals = len(max(mods, key=lambda x: len(x.signals)).signals)
    except Exception:
        signals = src.count("\n") // 10
    depth_class = "large" if signals > 60 else "medium" if signals > 20 else "small"
    return arithmetic, sequential, depth_class


def features_for(task, rtl_path: str | None = None) -> ObligationFeatures:
    """Build Omega_i for a board task. `kind` comes from the obligation itself;
    the rest is probed from the design it sits over."""
    kind = getattr(task, "prop_kind", None) or "invariant"
    arithmetic, sequential, depth_class = probe_structure(rtl_path)
    return ObligationFeatures(kind, arithmetic, sequential, depth_class)


@dataclass
class YieldModel:
    """What each action has actually YIELDED, keyed by obligation signature.

    The learning target is deliberately evidence yield — risk actually
    discharged per unit cost — not tool success. A formal run that terminates
    with no verdict, a simulation that never reaches the failure region, and a
    vacuous proof are all "successful" tool invocations that yield nothing. Every
    defect found in Experiments 1 and 2 was of that shape.
    """
    stats: dict = field(default_factory=dict)      # sig -> {method: [gain, cost]}
    globals_: dict = field(default_factory=dict)   # method -> [gain, cost]

    def observe(self, sig, method, gain, cost):
        s = self.stats.setdefault(sig, {})
        e = s.setdefault(method, [0.0, 0.0])
        e[0] += max(0.0, gain); e[1] += cost
        g = self.globals_.setdefault(method, [0.0, 0.0])
        g[0] += max(0.0, gain); g[1] += cost

    def rate(self, sig, method):
        """Yield per unit cost for this signature, backing off to the global
        prior when this signature has not been seen. Returns None if untried."""
        e = self.stats.get(sig, {}).get(method)
        if e and e[1] > 0:
            return e[0] / e[1]
        g = self.globals_.get(method)
        if g and g[1] > 0:
            return g[0] / g[1]
        return None

    def seen(self, sig):
        return sig in self.stats
