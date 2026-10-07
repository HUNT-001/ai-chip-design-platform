r"""Run S2 exactly as committed in prereg_s2_shape.json. Apply that rule only.

This script has no parameters. Every number it uses -- N, alpha, the per-board
censoring horizon, window and critical value, the seed bases -- is read out of
the committed JSON, and it refuses to run if that file's digest does not verify.
There is deliberately no --seeds, no --window and no --alpha flag: a runner that
can be re-pointed at a different rule is not a pre-registered analysis, and the
one thing this file exists to prevent is a threshold chosen after the Lambdas
are on screen.

WHAT IS OBSERVED. The index of first detection, cold start, one campaign per
seed:

    dfifo    tdet from the testbench's STIM line -- the first push accepted at
             full. -1 means no detection inside the campaign.
    sat_mac  the i of the first MISMATCH line. The testbench prints the first
             three mismatches, so the earliest is always present; a campaign
             with no MISMATCH and SIM_RESULT PASS did not detect.

Both indices are 0-based in their testbenches and both are converted to a
1-based count of vectors, T = index + 1, because the geometric null the
statistic divides by has support {1, 2, ...}. Off by one here would shift every
hazard by a vector, so it is stated rather than assumed.

CENSORING. A campaign that never detects is NOT a missing sample and not a
sample of zero. It is the observation "T > C", which is exactly what the
right-censored denominator D / sum(min(T_i, C)) is for. Those seeds contribute C
to the exposure and nothing to D.

A parse failure is an instrument defect, not a censored sample, and aborts.

    /usr/bin/python3 run_s2_shape.py
"""
from __future__ import annotations

import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, os.path.join(_ROOT, "voe"), os.path.join(_ROOT, "phase3"),
           _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from evidence_channels import SimChannel
from preregistration import Commitment

PREREG = os.path.join(_HERE, "prereg_s2_shape.json")
RESULTS = os.path.join(_HERE, "results_s2_shape.json")

_DFIFO_RTL = os.path.join(_HERE, "rtl")
_DFIFO_SIM = os.path.join(_HERE, "sim")
_SAT_RTL = os.path.join(_ROOT, "voe_stoch", "rtl")
_SAT_SIM = os.path.join(_ROOT, "voe_stoch", "sim")

_STIM_TDET = re.compile(r"STIM q0=-?\d+ thit=-?\d+ tdet=(-?\d+)")
_MISMATCH = re.compile(r"MISMATCH i=(\d+)")
_SIM_PASS = re.compile(r"SIM_RESULT PASS")


def dfifo_channel(mock=False):
    sources = [os.path.join(_DFIFO_RTL, f) for f in
               ("dfifo.sv", "dfifo_mut.sv", "dfifo_wrap.sv",
                "dfifo_wrap_mut.sv")]
    sources.append(os.path.join(_DFIFO_SIM, "tb_dfifo.sv"))
    return SimChannel(mock=mock, sources=sources, top="tb_dfifo",
                      defines_for=lambda bug: [
                          f"DUT={'dfifo_wrap_mut' if bug else 'dfifo_wrap'}"],
                      covers=r"^dfifo\.", mock_finds_bug=True)


def sat_channel(mock=False):
    sources = [os.path.join(_SAT_RTL, f) for f in
               ("sat_mac.sv", "sat_mac_mut.sv", "satmac_wrap.sv",
                "satmac_wrap_mut.sv")]
    sources.append(os.path.join(_SAT_SIM, "tb_satmac.sv"))
    return SimChannel(mock=mock, sources=sources, top="tb_satmac",
                      defines_for=lambda bug: [
                          f"DUT={'satmac_wrap_mut' if bug else 'satmac_wrap'}"],
                      covers=r"^satmac\.", mock_finds_bug=True)


def parse_dfifo(raw: str):
    m = _STIM_TDET.search(raw or "")
    if not m:
        return None
    t = int(m.group(1))
    return None if t < -1 else (t + 1 if t >= 0 else 0)


def parse_sat(raw: str):
    m = _MISMATCH.search(raw or "")
    if m:
        return int(m.group(1)) + 1
    return 0 if _SIM_PASS.search(raw or "") else None


def campaign(name, chan, n, seed_base, nvec, parse):
    """One cold-start campaign per seed. Returns the list of T, 0 = censored."""
    out = []
    for k in range(n):
        seed = seed_base + k
        ev = chan.run(inject_bug=True, seed=seed, nvec=nvec)
        if ev.status == "error":
            sys.exit(f"ABORT: {name} seed {seed} errored: {ev.detail}")
        t = parse(ev.raw)
        if t is None:
            sys.exit(f"ABORT: {name} seed {seed} produced no parseable "
                     f"detection index. Instrument defect, not a censored "
                     f"sample.")
        out.append(t)
        sys.stderr.write(f"\r  {name}: {k + 1}/{n} seeds")
        sys.stderr.flush()
    sys.stderr.write("\n")
    return out


def statistic(T, w, c):
    """Lambda, S and D exactly as committed. T_i = 0 means censored (T > C)."""
    s = sum(1 for t in T if 0 < t <= w)
    d = sum(1 for t in T if 0 < t <= c)
    risk = sum(min(t, w) if t > 0 else w for t in T)
    expo = sum(min(t, c) if t > 0 else c for t in T)
    if d == 0 or risk == 0:
        return None, s, d, risk, expo
    return (s / risk) / (d / expo), s, d, risk, expo


def main() -> int:
    c = Commitment.load(PREREG)
    print("=== S2 shape comparison: running the committed rule ===")
    print(c.render())
    if not c.intact:
        print("ABORT: the pre-registration digest does not verify. "
              "Nothing was run.")
        return 1

    spec = c.spec
    n = spec["N"]
    alpha = spec["alpha"]
    ex = spec["arms"]["exclusion_dfifo"]
    fit = spec["arms"]["fit_sat_mac"]
    print(f"    N = {n} seeds per board, alpha = {alpha:g}, cold start")
    print(f"    dfifo   C = {ex['C']:,}  W = {ex['W']:,}  reject if Lambda "
          f"<= {ex['critical_Lambda']}  seed_base {spec['seed_base']['dfifo']}")
    print(f"    sat_mac C = {fit['C']:,}  W = {fit['W']:,}  reject if Lambda "
          f"<= {fit['critical_Lambda']}  seed_base "
          f"{spec['seed_base']['sat_mac']}")
    print()

    boards = [
        ("dfifo", dfifo_channel(), spec["seed_base"]["dfifo"], ex, parse_dfifo),
        ("sat_mac", sat_channel(), spec["seed_base"]["sat_mac"], fit,
         parse_sat),
    ]
    results = {}
    for name, chan, base, arm, parse in boards:
        T = campaign(name, chan, n, base, arm["C"], parse)
        lam, s, d, risk, expo = statistic(T, arm["W"], arm["C"])
        if lam is None:
            rejected = False
            verdict = "NOT COMPUTABLE (D = 0)"
        else:
            rejected = lam <= arm["critical_Lambda"]
            verdict = "REJECTED" if rejected else "NOT REJECTED"
        results[name] = dict(lam=lam, s=s, d=d, rejected=rejected,
                             verdict=verdict, crit=arm["critical_Lambda"],
                             C=arm["C"], W=arm["W"], seed_base=base,
                             risk=risk, exposure=expo, T=T)

    print()
    print("results")
    print(f"  {'board':<9} {'Lambda':>9} {'crit':>8} {'S':>4} {'D':>5} "
          f"{'of N':>5}  verdict")
    for name in ("dfifo", "sat_mac"):
        r = results[name]
        lam = "n/a" if r["lam"] is None else f"{r['lam']:.4f}"
        print(f"  {name:<9} {lam:>9} {r['crit']:>8} {r['s']:>4} {r['d']:>5} "
              f"{n:>5}  {r['verdict']}")

    print()
    dfifo_rej = results["dfifo"]["rejected"]
    sat_rej = results["sat_mac"]["rejected"]
    passed = dfifo_rej and not sat_rej
    with open(RESULTS, "w") as f:
        json.dump({"prereg_digest": f"sha256:{c.digest}",
                   "prereg_intact": c.intact,
                   "N": n, "alpha": alpha, "condition": "cold start",
                   "gate_met": passed,
                   "gate_rule": spec["gate_passes_iff"],
                   "boards": results}, f, indent=2, sort_keys=True)
        f.write("\n")
    print(f"  wrote {os.path.relpath(RESULTS, _ROOT)}")
    print(f"  gate passes iff : null NOT rejected on sat_mac AND rejected on "
          f"dfifo")
    if passed:
        print(f"  GATE: MET")
        return 0
    if dfifo_rej and sat_rej:
        shape = "(2) both boards excluded"
    elif not dfifo_rej and not sat_rej:
        shape = "(1) both boards fit"
    else:
        shape = "(3) the roles invert"
    print(f"  GATE: NOT MET -- losing shape {shape}. H stays closed.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
