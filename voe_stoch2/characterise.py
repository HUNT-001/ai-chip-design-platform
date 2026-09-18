"""S1 — characterise pfifo as a STOCHASTIC benchmark.

This is the second board, and its purpose is generalisation, not novelty. The
sat_mac characterisation established that a non-degenerate stochastic regime
EXISTS somewhere in this corpus. It could not establish that stochastic
verification behaviour is a property of verification rather than a property of
that one design. One board never can — that is the Experiment 10 failure mode,
where policy L looked good on the full board and lost on the held-out one.

Like voe_stoch/characterise.py this is deliberately NOT an experiment. It
computes no efficiency, compares no policies and promotes nothing. It answers
one question about one design, and it answers it under a configuration that was
hashed before any campaign ran.

WHAT IS COMMITTED, AND WHY IT IS A GRID RATHER THAN A NUMBER
-------------------------------------------------------------
The temptation this project already refused once, by name, in Experiment 15:
shrink the vector count until the bug is sometimes missed. That tunes the
measuring apparatus until the hypothesis becomes testable, and it invalidates
whatever the apparatus then reports.

sat_mac avoided it by inheriting a campaign length (20000) that predated the
question. pfifo cannot inherit that: 20000 cycles on a six-deep FIFO is roughly
five hundred coverings of its control state space, which is not a campaign, it
is a soak test. Any single number I chose here would be a number I chose, and
"I picked it for structural reasons" is exactly what someone tuning the
instrument would also say.

So nothing is picked. A GRID of campaign lengths is committed in advance and the
ENTIRE curve is reported, including the points where the board is degenerate.
There is no post-hoc selection available, because there is no selection: the
deliverable is P(detect) as a function of campaign length, not a value at one
length. The grid spans 64 to 4096 cycles, bracketing the design's ~42-state
control space from about 1.5 coverings to about 100, and it was written down
before the RTL was ever simulated.

That the curve is the deliverable is not a workaround — it is a better
measurement. sat_mac's corner is memoryless, so its curve is exactly
1 - (1 - 1/65536)^N. pfifo's requires an occupancy random walk to be in the
right place when a rare control event arrives, and then requires the corruption
to survive to an output, so its curve should have a different shape: a
dead zone while the FIFO is still filling, then a rise governed by a hitting
time rather than by independent trials. Two point estimates of P(detect) could
coincide by accident. Two curve shapes agreeing would be a much stronger claim,
and two disagreeing is the generalisation evidence S2 needs.

    python characterise.py [--seeds N] [--mock]
"""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
for p in ("voe", "phase3"):
    sys.path.insert(0, os.path.join(HERE, "..", p))

from binomial import clopper_pearson, non_degenerate
from eligibility import STOCHASTIC, admit
from evidence_channels import SimChannel
from preregistration import Commitment

RTL = os.path.join(HERE, "rtl")
SIM = os.path.join(HERE, "sim")
SOURCES = [os.path.join(RTL, f) for f in
           ("pfifo.sv", "pfifo_mut.sv", "pfifo_wrap.sv", "pfifo_wrap_mut.sv")]
SOURCES.append(os.path.join(SIM, "tb_pfifo.sv"))

COMMIT_PATH = os.path.join(HERE, "commit_pfifo_s1.json")

# ---- THE COMMITTED CONFIGURATION ----------------------------------------- #
# Everything here is frozen before the first campaign and hashed. Changing any
# of it after seeing results changes the digest, and every run prints whether
# the digest still matches the file.
GRID = (64, 128, 256, 512, 1024, 2048, 4096)
SEED_BASE = 5000
DEFAULT_SEEDS = 24
MIN_EACH = 2          # >= 2 detections AND >= 2 misses at an admitted point

SPEC = {
    "design": "pfifo (depth 6, 16-bit, flush + flush_but_first)",
    "mutant": "missing pointer wrap on the flush_but_first write-pointer update",
    "campaign_grid_cycles": list(GRID),
    "seed_base": SEED_BASE,
    "seeds": DEFAULT_SEEDS,
    "stimulus_rates": {
        "push": "10/16", "pop": "8/16",
        "flush_but_first": "1/256", "flush": "1/1024",
        "source": "one $urandom() draw, separated bit lanes",
    },
    "admission": {
        "min_detections": MIN_EACH,
        "min_misses": MIN_EACH,
        "at_least_one_grid_point": True,
        "positive_control": "good DUT passes on every seed at every grid point",
        "reproducibility": "same seed, same campaign length, same verdict",
        "instrumentation": "no campaign may detect without reaching the corner",
    },
    "interval": "Clopper-Pearson 95%, shared implementation (voe/binomial.py)",
}

NOTES = (
    "Committed before any pfifo campaign ran. The grid exists so that no single "
    "campaign length can be selected after seeing detection rates; the whole "
    "curve is reported, degenerate points included. Grid range was set from the "
    "design's control state space (~42 states: 7 occupancies x 6 read-pointer "
    "positions), spanning roughly 1.5 to 100 coverings. Shrinking vectors until "
    "the bug is sometimes missed was refused in Experiment 15 and is refused "
    "here; the defence is that there is nothing to shrink TO, because every "
    "point is published.\n"
    "Stimulus rates model a prefetch buffer: an eager producer, a slower "
    "consumer, branch-misprediction-rate flush_but_first, rarer full flush. "
    "They were chosen for realism and are MEASURED by the instrumentation "
    "counters rather than assumed."
)


def channel(mock, nvec):
    """A FRESH channel per campaign length.

    Campaign length is compiled in via -DNVEC, so each grid point is a different
    binary. SimChannel keys its build cache on (variant, nvec) — it did not
    until this experiment needed it, and a shared channel would otherwise have
    built once at 64 cycles and re-run that same binary for every later point,
    producing a perfectly flat curve and a confident wrong conclusion. A fresh
    channel per point makes that impossible rather than merely fixed.
    """
    return SimChannel(mock=mock, sources=SOURCES, top="tb_pfifo",
                      defines_for=lambda bug: [
                          f"DUT={'pfifo_wrap_mut' if bug else 'pfifo_wrap'}"],
                      covers=r"^pfifo\.",
                      mock_finds_bug=True)


def trigger_count(ev):
    """How many times the stimulus reached the mutated line's precondition.

    Read from the instrumentation counters, which come from observation taps and
    are never consulted by the checker. Counting the corner directly is what
    separates 'the bug is rare' from 'the testbench cannot see the bug' — two
    states with identical detection rates and completely different meanings.
    """
    import re
    m = re.search(r"STIM trigger=(\d+) fbf=(\d+) full=(\d+) of (\d+)",
                  ev.raw or "")
    return tuple(int(g) for g in m.groups()) if m else None


def sweep(mock, seeds, rows_detail=None):
    """Run the committed grid. Returns rows of per-campaign-length results."""
    rows = []
    if rows_detail is None:
        rows_detail = {}
    for nvec in GRID:
        ch = channel(mock, nvec)
        hits = 0
        trig_total = fbf_total = full_total = 0
        good_status, detect_without_trigger = [], []
        for s in range(seeds):
            seed = SEED_BASE + s
            # GOOD variant first, same seed: identical stimulus, no divergence,
            # so its counters describe the stimulus rather than the bug's
            # effects. Counting corners on one seed set and detections on
            # another produced two incomparable samples on sat_mac and was read
            # as evidence about the DUT; pairing removes that entirely.
            g = ch.run(inject_bug=False, seed=seed, nvec=nvec, phi="pfifo.order")
            good_status.append(g.status)
            # Keep the DETAIL of the first non-pass, not just its status. A
            # control that reports FAIL without saying why sends the reader to
            # the wrong place: a Verilator build error and a genuine checker
            # disagreement both arrive as "not pass", and they have nothing to
            # do with each other. The first version of this script printed only
            # the grid points where the control failed, which was enough to stop
            # the run and not enough to act on.
            if g.status != "pass" and rows_detail.get(nvec) is None:
                rows_detail[nvec] = (g.status, g.detail,
                                     (g.raw or "").strip().splitlines()[-6:])
            tc = trigger_count(g)
            trig, fbf, full = tc[:3] if tc else (0, 0, 0)
            trig_total += trig
            fbf_total += fbf
            full_total += full

            d = ch.run(inject_bug=True, seed=seed, nvec=nvec, phi="pfifo.order")
            caught = (d.status == "counterexample")
            hits += caught
            if caught and trig == 0:
                detect_without_trigger.append(seed)
        rows.append({
            "nvec": nvec, "hits": hits, "seeds": seeds,
            "good": sorted(set(good_status)),
            "trigger": trig_total, "fbf": fbf_total, "full": full_total,
            "bad_detect": detect_without_trigger,
        })
        print(f"    n={nvec:<6} done", flush=True)
    return rows


def main():
    mock = "--mock" in sys.argv
    seeds = DEFAULT_SEEDS
    if "--seeds" in sys.argv:
        seeds = int(sys.argv[sys.argv.index("--seeds") + 1])

    spec = dict(SPEC, seeds=seeds)
    commit = Commitment("pfifo S1 characterisation", COMMIT_PATH, spec,
                        NOTES).commit()

    print("=== S1: characterising pfifo as a STOCHASTIC benchmark ===")
    print("    design    : depth-6 FIFO, flush + flush_but_first")
    print("    mutant    : one line — the flush_but_first write pointer does")
    print("                not wrap, so it can land on an addressable slot the")
    print("                read pointer will never visit")
    print(f"    grid      : {', '.join(str(g) for g in GRID)} cycles")
    print(f"    seeds     : {seeds} (from {SEED_BASE})   "
          f"mode = {'MOCK' if mock else 'REAL'}")
    print(commit.render())
    if not commit.intact:
        print("\n  The committed configuration was modified after it was hashed.")
        print("  Nothing measured under it can be reported. INVALID.")
        return
    if mock:
        print("\n    NOTE: mock returns a fixed verdict by construction and")
        print("          cannot characterise anything. Use --real.")

    print("\n  running the committed grid...")
    detail = {}
    rows = sweep(mock, seeds, detail)

    # ---- positive control, across the WHOLE grid ---------------------------
    bad_ctrl = [r["nvec"] for r in rows if r["good"] != ["pass"]]
    print(f"\n  positive control (good DUT passes everywhere) : "
          f"{'PASS' if not bad_ctrl else 'FAIL at ' + str(bad_ctrl)}")
    if bad_ctrl:
        # Say WHICH failure this is before saying what it means. Build errors
        # and checker disagreements are both "not pass" and are diagnosed in
        # completely different places.
        for nvec in bad_ctrl[:2]:
            st, det, tail = detail.get(nvec, ("?", "", []))
            print(f"\n    n={nvec}: status={st}  detail={det}")
            for ln in tail:
                print(f"      | {ln}")
        statuses = {r for nvec in bad_ctrl
                    for r in [detail.get(nvec, ("?",))[0]]}
        if statuses == {"error"}:
            print("\n  Every control run ERRORED rather than failing. That is a")
            print("  harness fault, not a checker disagreement: the DUT never")
            print("  ran, or produced no parseable SIM_RESULT line. Diagnose the")
            print("  build before forming any opinion about the design.")
        print("\n  The testbench does not reliably pass on correct RTL, so every")
        print("  P(detect) below was measured with an unvalidated checker and")
        print("  none of it means anything. Fix the bench, not the DUT.")
        print("  NOT ADMITTED.")
        return

    # ---- reproducibility ---------------------------------------------------
    ch = channel(mock, 1024)
    a = ch.run(inject_bug=True, seed=SEED_BASE + 7, nvec=1024,
               phi="pfifo.order").status
    b = ch.run(inject_bug=True, seed=SEED_BASE + 7, nvec=1024,
               phi="pfifo.order").status
    repro = (a == b)
    print(f"  reproducible (seed {SEED_BASE + 7}, n=1024)               : "
          f"{a} == {b} -> {'PASS' if repro else 'FAIL'}")

    # ---- the curve ---------------------------------------------------------
    print(f"\n  {'cycles':>7} {'hits':>7} {'P(detect)':>10} "
          f"{'95% CI':>18} {'corner':>8} {'fbf':>7} {'admit':>7}")
    admitted = []
    for r in rows:
        k, n = r["hits"], r["seeds"]
        p = k / n
        lo, hi = clopper_pearson(k, n)
        ok, why = non_degenerate(k, n, MIN_EACH)
        if ok:
            admitted.append(r)
        print(f"  {r['nvec']:>7} {k:>3}/{n:<3} {p:>10.3f} "
              f"  [{lo:.3f}, {hi:.3f}] {r['trigger']:>8} {r['fbf']:>7} "
              f"{'yes' if ok else 'no':>7}")

    # ---- instrumentation coherence ----------------------------------------
    # The strong check: a campaign cannot detect this mutant without having
    # reached the mutated line, because everywhere else the two designs are
    # bit-identical. A detection with a zero corner count would mean the mutant
    # differs somewhere it is not documented to differ.
    bad = sorted({s for r in rows for s in r["bad_detect"]})
    print(f"\n  detected with NO corner reached : "
          f"{bad if bad else 'none'}")
    instr_ok = not bad
    if instr_ok:
        print("    -> coherent. Everywhere except the mutated line the two")
        print("       designs are bit-identical, so a detection without a")
        print("       corner would mean the difference is not where it is")
        print("       documented to be — and the benchmark would be measuring")
        print("       something other than what it claims.")
    else:
        print("    -> INCOHERENT. Campaigns detect a difference without having")
        print("       reached the mutated line. The mutant is not the narrow")
        print("       thing it is documented to be. Do not use this board.")

    # ---- admission ---------------------------------------------------------
    elig = admit("pfifo board", STOCHASTIC,
                 distinct_outcomes=2 if admitted else 1,
                 controls_armed=(not bad_ctrl) and repro and instr_ok)
    print()
    print(elig.render())

    print("\n=== what this obliges ===")
    if elig.eligible:
        pts = ", ".join(str(r["nvec"]) for r in admitted)
        print(f"  ADMITTED as a STOCHASTIC benchmark at: {pts} cycles.")
        print("  At those campaign lengths the same action genuinely sometimes")
        print("  catches the bug and sometimes does not, so seeds are SAMPLES")
        print("  and a committed noise criterion is meaningful.")
        print("\n  This is the SECOND such board, which is the point. It does")
        print("  NOT by itself say the two boards are different stochastic")
        print("  regimes — that is S2's question, and two designs that happened")
        print("  to share a detection curve would be one regime measured twice.")
        print("  The Stochastic Generalization Gate needs BOTH admission here")
        print("  and a demonstrated difference there. H stays closed until then.")
    else:
        print("  NOT ADMITTED. No grid point has both >= 2 detections and")
        print("  >= 2 misses.")
        print("\n  The honest response is NOT to extend the grid until a point")
        print("  qualifies — that is the refused move wearing a different hat.")
        print("  If every point is degenerate, this design under this stimulus")
        print("  is a deterministic classifier, the gate does not pass, and the")
        print("  next step is a different mutant or a different design, chosen")
        print("  and committed before it is measured.")


if __name__ == "__main__":
    main()
