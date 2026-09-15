"""Characterise the sat_mac benchmark — is it admissible as STOCHASTIC?

Experiment 15 established a gate that runs before any metric: an experiment must
demonstrate that the phenomenon it intends to measure EXISTS in the chosen
environment. For STOCHASTIC experiments that means Var(O | a) > 0, measured on
the actual board under the declared stimulus distribution.

This script produces that evidence for the new benchmark. It is deliberately
NOT an experiment — it computes no efficiency, compares no policies, and
promotes nothing. It answers one question:

    across independent seeds, does the same action sometimes catch the bug and
    sometimes miss it?

WHAT ADMISSION REQUIRES, and why each part is here:

    0 < P(detect) < 1   the defining property. P = 0 means the TB cannot see
                        the bug at all (a broken checker); P = 1 means the board
                        is a deterministic classifier, which is exactly the
                        state that made Experiment 15 NOT EVALUABLE.

    positive control    the GOOD DUT must pass on every seed. A testbench that
                        fails spuriously cannot be trusted to refute, and a
                        P(detect) measured with a flaky checker is meaningless.

    reproducibility     the same seed must give the same verdict. Without that,
                        "P(detect)" is not a property of the benchmark.

HONESTY NOTE ON THE RARITY. The corner is (-128) x (-128), the single asymmetric
case of an 8x8 signed multiply — one operand pair in 65536. That rarity is a
property of two's-complement arithmetic, not a number chosen to land the
statistics somewhere convenient. The vector count stays at the project default
of 20000; tuning THAT to manufacture variance was considered and refused, and is
recorded as refused in the capability ledger.

Expected, from the arithmetic alone: P(detect) = 1 - (1 - 1/65536)^N_enabled,
which for ~20000 enabled vectors is about 0.26. The point of this script is that
the expectation is not taken on trust.

    python characterise.py [--seeds N]
"""
import os, statistics, sys

HERE = os.path.dirname(os.path.abspath(__file__))
for p in ("voe", "phase3"):
    sys.path.insert(0, os.path.join(HERE, "..", p))

from eligibility import STOCHASTIC, admit
from evidence_channels import SimChannel

RTL = os.path.join(HERE, "rtl")
SIM = os.path.join(HERE, "sim")
SOURCES = [os.path.join(RTL, f) for f in
           ("sat_mac.sv", "sat_mac_mut.sv", "satmac_wrap.sv", "satmac_wrap_mut.sv")]
SOURCES.append(os.path.join(SIM, "tb_satmac.sv"))

DEFAULT_SEEDS = 40


def channel(mock):
    return SimChannel(mock=mock, sources=SOURCES, top="tb_satmac",
                      defines_for=lambda bug: [
                          f"DUT={'satmac_wrap_mut' if bug else 'satmac_wrap'}"],
                      covers=r"^satmac\.",
                      mock_finds_bug=True)

def paired_corner_and_detect(ch, seeds, base=1000):
    """Count corner occurrences and detections on the SAME seeds.

    The first version counted corners on seeds 2000+ and detections on seeds
    1000+ — different samples, so the two rates were never comparable, and the
    8-seed corner count expected only ~2.4 events. Far too noisy to tell
    1/65536 from 1/33400, and compared against the wrong thing anyway.

    Pairing them also permits a much stronger check than rate agreement:
    detection should occur IF AND ONLY IF a campaign contains at least one
    corner. The counterexample traces show every failing run begins at the
    corner and the later mismatches are the accumulator having diverged, so any
    campaign that detects WITHOUT a corner, or contains a corner WITHOUT
    detecting, means the mutant is not the narrow thing it is documented to be.
    """
    import re
    rows = []
    for s in range(seeds):
        # corner count comes from the GOOD DUT: same stimulus, no divergence.
        g = ch.run(inject_bug=False, seed=base + s, nvec=20000, phi="satmac.acc")
        m = re.search(r"STIM corner_hits=(\d+) of (\d+)", g.raw or "")
        corners = int(m.group(1)) if m else None
        mm = re.search(r"STIM a_hits=(\d+) b_hits=(\d+)", g.raw or "")
        marg = (int(mm.group(1)), int(mm.group(2))) if mm else (None, None)
        rows.append((base + s, corners, None, marg))
        rows[-1] = (base + s, corners, None, marg)
        d = ch.run(inject_bug=True, seed=base + s, nvec=20000, phi="satmac.acc")
        rows[-1] = (base + s, corners, d.status == "counterexample", marg)
    return rows


def report_pairing(rows):
    ok = [r for r in rows if r[1] is not None]
    if not ok:
        print("  no STIM lines — rebuild the testbench")
        return
    tot_c = sum(r[1] for r in ok)
    tot_v = 20000 * len(ok)
    det = sum(1 for r in ok if r[2])
    print(f"  corners       : {tot_c} over {tot_v} vectors "
          f"-> 1 in {tot_v / max(tot_c, 1):.0f}  (arithmetic: 1 in 65536)")
    print(f"  detections    : {det}/{len(ok)}")
    # the strong check: detection <=> at least one corner in that campaign
    bad_det = [r[0] for r in ok if r[2] and r[1] == 0]
    bad_mis = [r[0] for r in ok if not r[2] and r[1] > 0]
    print(f"  detected with NO corner : {bad_det if bad_det else 'none'}")
    print(f"  corner but NOT detected : {bad_mis if bad_mis else 'none'}")
    # marginals: is a single draw non-uniform, or are the two correlated?
    ma = [r[3][0] for r in ok if len(r) > 3 and r[3][0] is not None]
    mb = [r[3][1] for r in ok if len(r) > 3 and r[3][1] is not None]
    if ma and mb:
        print(f"  P(a == -128)  : 1 in {tot_v / max(sum(ma), 1):.0f}   "
              f"(uniform: 1 in 256)")
        print(f"  P(b == -128)  : 1 in {tot_v / max(sum(mb), 1):.0f}   "
              f"(uniform: 1 in 256)")
        exp_joint = sum(ma) * sum(mb) / tot_v
        print(f"  joint if INDEPENDENT : {exp_joint:.1f} expected, "
              f"{tot_c} observed")
        if exp_joint > 0 and tot_c > 2 * exp_joint:
            print("  -> the draws are CORRELATED: the marginals cannot explain")
            print("     the joint rate. $urandom() calls are not independent.")
        elif ma and sum(ma) > 1.5 * tot_v / 256:
            print("  -> a MARGINAL is non-uniform; the low byte of $urandom()")
            print("     is not flat over the signed 8-bit range.")
    iff_ok = not bad_det and not bad_mis
    if iff_ok:
        print("  -> detection occurs IFF a corner occurs. The mutant is as")
        print("     narrow as documented, and P(detect) is exactly the")
        print("     probability a campaign contains the corner.")
    else:
        print("  -> MISMATCH between corners and detections. The mutant is NOT")
        print("     the narrow thing it is documented to be; do not use this")
        print("     benchmark quantitatively until that is explained.")
    # Poisson z on the joint. Before the stimulus fix this was 25 vs 12.2
    # (3.6 sigma, structural); after it is a far smaller excess. Report the
    # number rather than a verdict word, and let the threshold be explicit.
    indep_ok = True
    if ma and mb and exp_joint > 0:
        z = (tot_c - exp_joint) / (exp_joint ** 0.5)
        indep_ok = abs(z) < 2.0
        print(f"  joint vs independence : {z:+.2f} sigma "
              f"-> {'consistent' if indep_ok else 'INCONSISTENT'}")
        # Poisson interval on the rate, so its PRECISION is visible too
        lo, hi = tot_c - 1.96 * tot_c ** 0.5, tot_c + 1.96 * tot_c ** 0.5
        if lo > 0:
            print(f"  corner rate 95% CI    : 1 in {tot_v / hi:.0f} .. "
                  f"1 in {tot_v / lo:.0f}   (known to ~{hi / lo:.1f}x)")
    return iff_ok and indep_ok


def first_mismatches(seeds=12, mock=False):
    """Print the operands the mutant is actually caught on.

    Reading these settled a question guessing could not: every failing campaign
    BEGINS at (-128,-128), and the mismatches after it are the accumulator
    having diverged from that one hit. So the mutant is narrow exactly as
    documented, and the Verilog width/signedness theory was wrong. Read the
    counterexample rather than reason about the language rules — the lesson this
    project keeps relearning.
    """
    import re
    ch = channel(mock)
    shown = 0
    for s in range(seeds):
        ev = ch.run(inject_bug=True, seed=1000 + s, nvec=20000, phi="satmac.acc")
        if ev.status != "counterexample":
            continue
        for line in (ev.raw or "").splitlines():
            m = re.match(r"MISMATCH i=(\d+) a=(-?\d+) b=(-?\d+) dut=(-?\d+) ref=(-?\d+)",
                         line.strip())
            if m:
                i, a, b, dut, ref = m.groups()
                corner = (a == "-128" and b == "-128")
                print(f"    seed {1000+s}  i={i:>6}  a={a:>5} b={b:>5}  "
                      f"dut={dut:>7} ref={ref:>7}  "
                      f"{'<-- THE CORNER' if corner else '<-- NOT the corner'}")
                shown += 1
        if shown >= 8:
            break
    if not shown:
        print("    no MISMATCH lines captured")


def main():
    mock = "--mock" in sys.argv
    seeds = DEFAULT_SEEDS
    if "--seeds" in sys.argv:
        seeds = int(sys.argv[sys.argv.index("--seeds") + 1])

    ch = channel(mock)
    print("=== characterising sat_mac as a STOCHASTIC benchmark ===")
    print("    corner    : (-128) x (-128), 1 operand pair in 65536")
    print("    stimulus  : uniform signed 8-bit, 20000 vectors (project default)")
    print(f"    seeds     : {seeds}   mode = {'MOCK' if mock else 'REAL'}")
    print("    NOTE: mock cannot characterise anything — it returns a fixed")
    print("          verdict by construction. Use the real run.\n")

    # ---- positive control FIRST: the good DUT must pass on every seed -------
    good = []
    for s in range(seeds):
        ev = ch.run(inject_bug=False, seed=1000 + s, nvec=20000, phi="satmac.acc")
        good.append(ev.status)
    pos_ok = set(good) == {"pass"}
    print(f"  positive control (good DUT) : {sorted(set(good))}  "
          f"-> {'PASS' if pos_ok else 'FAIL'}")
    if not pos_ok:
        print("\n  The testbench does not reliably pass on correct RTL, so any")
        print("  P(detect) measured with it is meaningless. Fix the checker")
        print("  before characterising anything. NOT ADMITTED.")
        return

    # ---- the measurement: how often does a campaign catch the mutant? -------
    hits, statuses = 0, []
    for s in range(seeds):
        ev = ch.run(inject_bug=True, seed=1000 + s, nvec=20000, phi="satmac.acc")
        statuses.append(ev.status)
        if ev.status == "counterexample":
            hits += 1
    p = hits / seeds
    # Wald interval is poor near 0/1; this is a rough guide, not a claim.
    se = (p * (1 - p) / seeds) ** 0.5 if 0 < p < 1 else 0.0
    print(f"  mutant caught in            : {hits}/{seeds} campaigns")
    print(f"  P(detect)                   : {p:.3f}  +/- {1.96 * se:.3f} (95%, rough)")
    print(f"  distinct outcomes           : {sorted(set(statuses))}")

    # ---- reproducibility: same seed, same verdict ---------------------------
    a = ch.run(inject_bug=True, seed=1007, nvec=20000, phi="satmac.acc").status
    b = ch.run(inject_bug=True, seed=1007, nvec=20000, phi="satmac.acc").status
    repro = (a == b)
    print(f"  reproducible (seed 1007)    : {a} == {b} -> "
          f"{'PASS' if repro else 'FAIL'}")

    # ---- stimulus-distribution control, when prediction and measurement part -
    # The arithmetic says P(detect) = 1-(1-1/65536)^20000 = 0.263. Inferring the
    # corner rate FROM the detection rate assumes uniform stimulus; if the two
    # disagree, that assumption is what to check first — not the conclusion.
    predicted = 1.0 - (1.0 - 1.0 / 65536.0) ** 20000
    if not mock:
        # UNCONDITIONAL. An earlier version ran this only when P(detect) looked
        # wrong, so the benchmark's headline property — the corner rate — went
        # unmeasured whenever the derived statistic happened to look fine. A
        # check that runs only on suspicion never confirms the healthy case, and
        # this project has now hit that pattern in four separate places.
        print(f"\n  STIMULUS CHARACTERISATION (always run; arithmetic predicts "
              f"P(detect) = {predicted:.3f})")
        stim_ok = report_pairing(paired_corner_and_detect(ch, seeds))
        p_ok = abs(p - predicted) <= 1.96 * se
        if not p_ok:
            print("\n  P(detect) still differs from the arithmetic beyond the")
            print("  interval. Operands the mutant is caught on:")
            first_mismatches(seeds=6, mock=mock)
        print()
        if stim_ok and p_ok:
            print("  STIMULUS CHARACTERISED. Marginals uniform, joint consistent")
            print("  with independence, detection occurs iff the corner occurs,")
            print("  and P(detect) agrees with the arithmetic. The benchmark's")
            print("  documented property matches what it does, so the")
            print("  quantitative block is LIFTED.")
        else:
            print("  NOT fully characterised. Admission stands (0 < P < 1), but")
            print("  do NOT use this benchmark quantitatively: a rate that is")
            print("  not what the model says means the stimulus distribution is")
            print("  not what was declared.")
        print("\n  Operands the mutant is actually caught on (the failure")
        print("  begins AT the corner; later lines are the accumulator having")
        print("  diverged from that single hit, not separate detections):")
        first_mismatches(seeds=6, mock=mock)

    n_distinct = len(set(statuses))
    elig = admit("sat_mac board", STOCHASTIC, distinct_outcomes=n_distinct,
                 controls_armed=pos_ok and repro)
    print()
    print(elig.render())

    print("\n=== what this obliges ===")
    if elig.eligible:
        print(f"  ADMITTED as a STOCHASTIC benchmark. P(detect) = {p:.3f} is")
        print("  non-degenerate, so seeds are SAMPLES here rather than")
        print("  repetition, and a committed noise criterion is meaningful for")
        print("  the first time in this project.")
        print("\n  This unlocks the question class that has been blocked:")
        print("  seed sensitivity, adaptive sampling, probabilistic diagnosis,")
        print("  and — only now — whether posterior/belief machinery earns its")
        print("  complexity. Those were untestable on a deterministic corpus,")
        print("  so H-uncertainty's remaining revisit conditions become askable")
        print("  rather than merely unanswered.")
        print("\n  It does NOT say the benchmark is representative. One rare")
        print("  corner in one arithmetic unit is a starting point, not a suite.")
    elif p == 0.0:
        print("  NOT ADMITTED. The testbench never catches the mutant, so it is")
        print("  not checking what it claims to. That is a broken checker, not")
        print("  a rare bug — diagnose the TB before touching the DUT.")
    elif p == 1.0:
        print("  NOT ADMITTED. The mutant is caught on every seed, so this board")
        print("  is another deterministic classifier and would fail the same")
        print("  eligibility gate that made Experiment 15 NOT EVALUABLE. The")
        print("  corner is not rare enough under this stimulus.")
    else:
        print("  NOT ADMITTED — see the reasons above.")




if __name__ == "__main__":
    main()
