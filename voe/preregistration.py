"""Pre-registration — fix the decision rule before the data exists.

Every defect in this platform's measurement history was caught AFTER a number
had been reported: a metric that rewarded proving nothing, a statistic
incapable of detecting variance, a verdict that contradicted itself in the
adjacent paragraph, a comparison that flipped sign when one seed changed. Each
was found by looking harder at a result already in hand.

That is the one control the system did not have. Pre-registration acts before
the fact:

    1. the criteria are written to disk and hashed BEFORE any campaign runs
    2. the analysis re-reads them, verifies the hash, and applies exactly those
    3. a result that does not meet the committed rule is reported as NOT MET,
       never re-litigated with a rule chosen afterwards

The temptation this defends against is specific and was live in this project:
`H over G` was +2.2% against a spread of 0.036, having already flipped sign
once. "Run it twenty more times" would eventually produce a run where it looks
decisive, and nothing in the code would have objected.

This is the same discipline the kernel applies to knowledge — a claim needs a
witness that existed before the claim — applied to the experiment itself.
"""
from __future__ import annotations
import hashlib, json, os, time
from dataclasses import dataclass, asdict, field


@dataclass
class Criteria:
    """The decision rule, fixed in advance."""
    question: str                      # what is being decided, in one sentence
    treatment: str                     # the arm that must prove itself
    control: str                       # what it must beat
    min_seeds: int = 12                # independent simulation seeds
    min_design_families: int = 3       # structurally distinct DUT families
    min_effect: float = 0.05           # practical threshold (relative)
    noise_multiple: float = 2.0        # must clear this many std to count
    max_ci_width: float = 0.10         # if the interval is wider, UNDERPOWERED
    promote_if_met: bool = True

    def rule(self) -> str:
        return (f"promote '{self.treatment}' over '{self.control}' only if: "
                f">= {self.min_seeds} seeds and >= {self.min_design_families} "
                f"design families; relative gain > {self.min_effect:.0%}; "
                f"absolute gain > {self.noise_multiple:g}x the larger std; "
                f"and the CI width <= {self.max_ci_width:.2f} "
                f"(otherwise UNDERPOWERED, not a negative result)")


@dataclass
class Preregistration:
    criteria: Criteria
    path: str
    committed_at: float = 0.0
    digest: str = ""
    notes: str = ""

    # -- before the data ---------------------------------------------------- #
    def commit(self):
        """Write the criteria and hash them. Must be called BEFORE running."""
        if os.path.exists(self.path):
            return self.load(self.path)          # never silently overwrite
        self.committed_at = time.time()
        body = json.dumps({"criteria": asdict(self.criteria),
                           "committed_at": self.committed_at,
                           "notes": self.notes}, sort_keys=True, indent=2)
        self.digest = hashlib.sha256(body.encode()).hexdigest()[:16]
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w") as f:
            f.write(body + f"\n// sha256:{self.digest}\n")
        return self

    @classmethod
    def load(cls, path):
        with open(path) as f:
            text = f.read()
        body, _, tail = text.rpartition("// sha256:")
        body = body.rstrip("\n")
        recorded = tail.strip()
        actual = hashlib.sha256(body.encode()).hexdigest()[:16]
        d = json.loads(body)
        p = cls(Criteria(**d["criteria"]), path, d["committed_at"], actual,
                d.get("notes", ""))
        p._intact = (actual == recorded)
        return p

    @property
    def intact(self) -> bool:
        return getattr(self, "_intact", True)

    # -- after the data ----------------------------------------------------- #
    def decide(self, treat_mean, treat_std, ctrl_mean, ctrl_std,
               n_seeds, n_families):
        """Apply exactly the committed rule. Returns (verdict, reasons)."""
        c = self.criteria
        reasons = []
        if not self.intact:
            return "INVALID", ["the pre-registration file was modified after commit"]
        if n_seeds < c.min_seeds:
            reasons.append(f"only {n_seeds} seeds, need >= {c.min_seeds}")
        if n_families < c.min_design_families:
            reasons.append(f"only {n_families} design families, "
                           f"need >= {c.min_design_families}")
        spread = max(treat_std, ctrl_std)
        # 95% interval half-width on the mean, roughly
        ci = 2.0 * spread / max(1, n_seeds) ** 0.5
        rel = (treat_mean - ctrl_mean) / ctrl_mean if ctrl_mean else 0.0
        abs_gain = treat_mean - ctrl_mean

        if reasons:
            return "UNDERPOWERED", reasons
        if 2 * ci > c.max_ci_width:
            return "UNDERPOWERED", [f"CI width {2*ci:.3f} > {c.max_ci_width:.2f}"]
        if rel <= c.min_effect:
            return "NOT MET", [f"relative gain {rel:+.1%} <= {c.min_effect:.0%}"]
        if abs_gain <= c.noise_multiple * spread:
            return "NOT MET", [f"absolute gain {abs_gain:.3f} <= "
                               f"{c.noise_multiple:g}x std ({spread:.3f})"]
        return "MET", [f"relative gain {rel:+.1%}; absolute {abs_gain:.3f} > "
                       f"{c.noise_multiple:g}x{spread:.3f}; CI width {2*ci:.3f}"]
