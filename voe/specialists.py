"""Phase-4 specialised engineers.

The kernel's rule (VSA_KERNEL_FREEZE_AND_ROADMAP.md): **semantic specialisation
is state ownership, not a task role.** A domain expert is the agent that owns a
region of the failure space — its *property class* — together with the schemas
about that domain held in its semantic memory `M_s`. "Shift Verification
Engineer" is not a microservice; it is whoever owns the shift properties and the
shift schemas.

Two orthogonal axes, deliberately kept separate:

    cognition  (archetype)   how it reasons     — skeptic / explorer
    expertise  (M_s + class) what it reasons about — arithmetic / shift / ...

A skeptic-shift-specialist and an explorer-shift-specialist know the same domain
facts and behave differently; a skeptic-shift and a skeptic-compare reason alike
about different regions. This is heterogeneous cognition over a shared kernel.

**The invariant that matters (attack sheet 2.3 — memory cannot certify).**
`M_s` may change *what a specialist tries next* — method, ordering, effort — but
it has no path to the residual risk `R`. Risk is computed only from judgments in
the canonical knowledge state, and every judgment needs a real witness. Domain
experience makes an engineer faster, never more certain without evidence.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field

from workers import Worker, Proposal


# --------------------------------------------------------------------------- #
# Property class — the owned region of the failure space                      #
# --------------------------------------------------------------------------- #
@dataclass
class PropertyClass:
    name: str
    pattern: str                    # regex matched against the property id

    def owns(self, phi: str) -> bool:
        return re.search(self.pattern, phi) is not None


# --------------------------------------------------------------------------- #
# Semantic memory M_s — schemas, NOT evidence                                 #
# --------------------------------------------------------------------------- #
@dataclass
class SemanticMemory:
    """Domain schemas held by a specialist.

    `known_failure_modes` is institutional experience about where bugs hide in
    this domain. `preferred_method` and `formal_first` express how that
    experience shapes a plan. `difficulty` is a prior on how hard the class is
    to close. NONE of these can discharge risk — they only steer the planner.
    """
    domain: str
    known_failure_modes: list[str] = field(default_factory=list)
    preferred_method: str = "sim"       # "sim" | "formal"
    formal_first: bool = False          # go straight to proof (costly, decisive)
    difficulty: float = 0.5             # 0 easy … 1 hard (prior only)
    notes: str = ""

    def explain(self) -> str:
        fm = f"{len(self.known_failure_modes)} known failure modes"
        return f"{self.domain}: {fm}, prefers {self.preferred_method}" + \
               (" (formal-first)" if self.formal_first else "")


# --------------------------------------------------------------------------- #
# Specialist                                                                  #
# --------------------------------------------------------------------------- #
class Specialist(Worker):
    """A Worker that may only act inside its owned property class.

    Ownership is enforced in `propose` (it never bids outside its class) and in
    `execute` (a defensive check, so a mis-routed task cannot be worked on).
    """

    def __init__(self, name, archetype, kern, formal, sim,
                 prop_class: PropertyClass, memory: SemanticMemory, nvec=20000,
                 static=None):
        super().__init__(name, archetype, kern, formal, sim, nvec=nvec, static=static)
        self.prop_class = prop_class
        self.memory = memory

    # -- ownership ---------------------------------------------------------- #
    def owns(self, phi: str) -> bool:
        return self.prop_class.owns(phi)

    # -- deliberation: M_s shapes the plan, never the risk ------------------- #
    def propose(self, ks, board, ledger):
        from board import ACTION_COST
        weights = board.weights()
        cands = [t for t in board.actionable(ks)
                 if self.owns(t.phi) and t.phi not in self.skip]
        if not cands:
            return None
        best = max(cands, key=lambda t: self.k.utility(ks, weights, t.phi)[0])
        phi = best.phi
        if best.kind == "structural" and self.static is not None:
            if not ledger.can_afford("static"):
                return None
            return Proposal(self, phi, "static",
                            self.k.utility(ks, weights, phi)[0], ACTION_COST["static"])
        n = ks.n_eff(phi)

        # Semantic memory + archetype decide the METHOD only.
        explore_budget = 2 if self.memory.difficulty > 0.6 else 4
        can_sim = self.sim.covers(phi)      # only sample what the TB checks
        if self.memory.formal_first or self.archetype == "skeptic" or not can_sim:
            method = "formal" if ledger.can_afford("formal") else "sim"
        elif self.memory.preferred_method == "formal" and n >= 1:
            method = "formal" if ledger.can_afford("formal") else "sim"
        else:
            method = "sim" if (n < explore_budget and ledger.can_afford("sim")) \
                     else ("formal" if ledger.can_afford("formal") else "sim")
        if not ledger.can_afford(method):
            return None
        return Proposal(self, phi, method, self.k.utility(ks, weights, phi)[0],
                        ACTION_COST[method])

    def execute(self, ks, board, phi, method):
        if not self.owns(phi):
            raise ValueError(
                f"{self.name} does not own {phi} (class '{self.prop_class.name}')")
        return super().execute(ks, board, phi, method)


def unowned_properties(board, specialists):
    """Properties no specialist owns — an organisational coverage gap.

    Reported rather than silently skipped: an unowned obligation is exactly the
    kind of thing that quietly never gets verified.
    """
    return [t.phi for t in board.tasks.values()
            if not any(s.owns(t.phi) for s in specialists if hasattr(s, "owns"))]
