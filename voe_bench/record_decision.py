"""Write the H-vs-G decision into institutional memory, with revisit conditions.

Run once after the pre-registered experiment. The point is not bookkeeping: the
`revisit_if` list is what turns a rejection into a research programme, because it
states precisely what would have to change for the answer to change.
"""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "voe"))

from institutional_memory import Ledger, Record, promotion_verdict

LEDGER = os.path.join(HERE, "capability_ledger.json")


def main():
    led = Ledger(LEDGER)
    if any(r.treatment == "H-uncertainty" for r in led.records):
        print("  already recorded:\n")
        print(led.render())
        return

    # measured: G 1.256 +/- 0.000, H 1.291 +/- 0.038, probe cost 1.00 -> 2.75
    benefit = (1.291 - 1.256) / 1.256          # +2.7%
    spread = 0.038 / 1.256                     # relative uncertainty
    complexity = (2.75 - 1.00) / 1.00 * 0.02   # extra probing, damped to a rate
    ok, why = promotion_verdict(benefit, 0.05, spread, complexity)

    rec = led.add(Record(
        hypothesis="Explicit Bayesian belief over verification regimes plus a "
                   "priced Value of Diagnosis selects actions better than a "
                   "one-line diagnostic heuristic.",
        treatment="H-uncertainty",
        control="G-diagnostic",
        evidence="12 independent seeds, 6 design families, 20 obligations. "
                 "G 1.256 +/- 0.000; H 1.291 +/- 0.038 (+2.7% relative). "
                 "Pre-registered threshold 5%; H's worst run 1.229 fell below "
                 "G's mean. Probing cost 1.00 -> 2.75.",
        decision="PROMOTED" if ok else "REJECTED",
        reason=why + ". The simpler policy already meets the requirement, so the "
               "additional machinery is not paid for by the evidence.",
        complexity_note="~3x probing spend; a belief model, sampler and VoD "
                        "calculation to maintain and to keep correct.",
        revisit_if=[
            "LONG-HORIZON boards: the best first action has low immediate value "
            "but unlocks later value, so a one-step expected-value rule cannot "
            "see the chain",
            "COUPLED obligations: one action affects many properties at once, so "
            "per-obligation greedy choices can be globally poor",
            "NON-STATIONARY environments: an RTL commit changes which channel "
            "pays, faster than a realised-rate heuristic re-learns",
            "LARGE action spaces: thousands of candidate stimuli, assertions or "
            "decompositions, where a heuristic becomes too coarse to rank",
            "MULTI-STEP diagnosis: probe -> interpret -> probe again, which "
            "requires reasoning about future information rather than one probe",
        ]))
    print("=== recorded ===\n")
    print(rec.render())
    print("\n=== what would re-open this ===")
    for cond, who in led.revisit_triggers().items():
        print(f"  - {cond.split(':')[0]}  ({', '.join(who)})")
    print("\n  An organisation that remembers only its successes relearns its")
    print("  failures. These conditions are also the next research programme:")
    print("  each one names a regime where the simple policy should break.")


if __name__ == "__main__":
    main()
