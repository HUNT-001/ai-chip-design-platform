# VSA v1.0 — Attack Sheet (for external review)

Organised **by dependency**, not by document section. A reviewer should know
whether breaking a claim *collapses the framework* or merely *improves an
implementation*. Break Tier 0 → the foundation falls. Break Tier 3 → we fix an
engineering detail. Each item states the claim, how to attack it, and the blast
radius.

Legend: **status** — ✓validated (committed script) · ⊘provisional · ⚠honest-limit.

---

## Tier 0 — If this falls, everything falls

| # | Claim | How to attack | If it breaks |
|---|---|---|---|
| 0.1 | **Ontology closure (provisional).** Six roles {World, Knowledge, Possibility, Memory, Value, Accountability} form a complete basis. ⊘ | Exhibit a mathematical object an accountable verifier must maintain that reduces to *none* of the six roles (and is not a mere representation of one). | New primitive ⇒ re-derive algebra, laws, everything downstream. |
| 0.2 | **Typed-judgment ownership `E ⊢ K`.** Unjustified knowledge is unrepresentable; accountability is a type discipline, not a stored object. ✓ (enforced at construction in `vsa_reference.py`) | Show a knowledge item that must be held *without* a warrant, or a warrant that cannot be attached to its proposition. | Accountability reverts to a bolt-on; §3 collapses; witnesses become optional (unsound). |
| 0.3 | **State-algebra consistency.** Merge is a partial commutative monoid; forgetting is risk-monotone; equivalence is warrant-typed. | Construct two knowledge states whose merge is order-dependent, or a forgetting step that *lowers* risk, or evidence equivalent under the rule but inducing different risk. | The grammar is inconsistent ⇒ the algebra is not well-defined. |

## Tier 1 — Core mathematical guarantees

| # | Claim | How to attack | If it breaks |
|---|---|---|---|
| 1.1 | **Prior-robustness (Safe-1).** `ℛ` is valid over all admissible priors; no prior lowers it. ✓ (`vws_stress_tests.py`) | Find a belief representation + prior that makes the *robust* `ℛ` smaller with no new evidence. | `ℛ` becomes gameable — reverts to the entropy/confidence family we rejected. |
| 1.2 | **Struct-5 basis invariance.** `ℛ` is a measure on the failure σ-algebra; refinement composes by inclusion–exclusion and is representation-independent. ✓ (`vws_struct5_validation.py`) | Exhibit two spec descriptions inducing the *same* failure σ-algebra but different `ℛ`, that cannot be reconciled by normalising to a partition basis. | Verification depends on description, not meaning — the invariance principle fails. |
| 1.3 | **`ℛ` representation theorem.** Under the stated axioms, `ℛ` is unique up to `α` (weighted Clopper–Pearson). ✓ (tightness: `0.0198≤0.05`) | Produce a functional satisfying all axioms that is *not* the CP form; or show an axiom is not natural. | Uniqueness lost ⇒ `ℛ` is a choice, not a consequence. |
| 1.4 | **Exploration `X` is framework-native.** `X` = expected reduction of admissible-uncertainty (credal width), derived from VSA's own primitives, not Shannon information. ✓ (`vws_exploration_validation.py`) | Show `X` reduces to information gain, or is not derivable from the credal structure, or that width does not collapse with evidence. | Exploration must be *imported* — the foundation no longer derives its own optimisation criterion. |

## Tier 2 — AI properties

| # | Claim | How to attack | If it breaks |
|---|---|---|---|
| 2.1 | **Utility formulation.** `𝒰 = value(Δℛ) + λ_X·X − costs`; `ℛ` is one input, not the objective. | Give a case where maximising `𝒰` is clearly wrong yet no term is missing, or where exploit/explore are not separable axes. | The decision layer is mis-specified. |
| 2.2 | **Generation–adjudication firewall (Fire-1).** Created/hallucinated content only proposes; nothing enters sign-off `ℛ` without a witness. ✓ (hallucination demo) | Construct a created belief that reaches `ℛ` *without* re-adjudication against `S`. | Unsound content can certify — the AI-safety boundary fails. |
| 2.3 | **Memory cannot certify.** `M` guides the planner but never lowers `ℛ`. ✓ (falls out of Safe-1) | Show transferred/remembered knowledge that legitimately lowers robust `ℛ` with no fresh evidence. | Reuse could substitute for verification — a dangerous continual-learning failure. |
| 2.4 | **Closed-loop dynamics + approximation bound.** Every engine acts on `Ŝ≈S`; decision error is bounded by `d(Ŝ,S)`. ✓ (compression d=0 demo) | Exhibit a bounded `d(Ŝ,S)` with unbounded decision error, or a needed reasoning mode the loop cannot express. | The AI-integration theory is unsound. |

## Tier 3 — Engineering assumptions

| # | Claim | How to attack | If it breaks |
|---|---|---|---|
| 3.1 | **Cross-commit safety ⟺ COI soundness.** ✓ (0.000 vs 0.497) | Show a sound COI that still lets stale evidence certify a post-commit bug. | Fix the COI engine (implementation), not the theory. |
| 3.2 | **Effective sample size `n_eff`.** Redundant/correlated tests must not inflate `n_eff`. ⚠ | Defeat the `n_eff` estimator with adversarially correlated stimulus. | Improve the estimator (coverage-as-effective-N); theory unaffected. |
| 3.3 | **Credal-set implementation.** The admissible prior set `Π` is chosen well enough for `X`/robust bounds. ⚠ | Show a reasonable `Π` choice that makes `X` or `ℛ` misleading. | Tune `Π`; a calibration exercise, not a foundational flaw. |
| 3.4 | **Runtime efficiency / distribution match.** `O(|A|·c_EIG)` planning; stimulus ≈ deployment. ⚠ | Force intractable planning at chip scale, or a distribution gap sim cannot cover. | Engineering + the honest sim-conditional limit (§8). |

---

## Two boundaries no attack can "win" (they are stated limits, not defects)

- **Specification completeness** — `ℛ` bounds risk over the *given* `Φ`; a bug
  with no property is invisible. `H`/`Γ` search for missing properties but cannot
  self-certify completeness. Breaking this is not breaking VSA; it is restating a
  declared boundary (§8).
- **Distribution transfer** — inductive `ℛ` is stimulus-conditional; only
  deductive knowledge is distribution-free. Same status.

## Reviewer guidance

Spend effort **top-down**. A successful Tier-0 attack is a genuine refutation and
we would report it as such. A Tier-3 attack is welcome but improves an
implementation. Everything marked ✓ ships with a committed, re-runnable script
(`docs/vws_*_validation.py`, `docs/vsa_reference.py`); attack those first — they
make the strongest claims and are the easiest to check.
