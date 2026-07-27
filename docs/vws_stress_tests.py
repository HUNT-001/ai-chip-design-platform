import math, random, statistics as st
random.seed(5)
def UB(n, alpha=0.95): return 1-(1-alpha)**(1.0/(n+1))

print("###### ATTACK 1: redundancy / effective sample size ######")
n_raw=2000
print(f"  naive RVR (all {n_raw} tests counted): w*UB = {5*UB(n_raw):.4f}  -> 'SAFE'")
print(f"  honest RVR (effective coverage of bug region = 0): w*UB = {5*UB(0):.4f}  -> UNSAFE")
print("  BREAK: gamed by redundant/irrelevant tests. FIX: n = EFFECTIVE independent")
print("  coverage of phi's failure space (= what functional coverage should measure).\n")

print("###### ATTACK 2: distribution shift (test dist != deployment) ######")
rho_test=1e-7; rho_deploy=1e-3; n=500000
print(f"  RVR under test stimulus (rho~{rho_test:g}): w*UB(n)= {5*UB(n):.6f} -> 'SAFE'")
print(f"  true deployment risk (rho={rho_deploy:g}): w*rho = {5*rho_deploy:.6f}")
print(f"  underestimate factor = {rho_deploy/UB(n):.0f}x")
print("  BREAK: bound valid only for the sampled distribution. FIX: label RVR sim-CONDITIONAL;")
print("  distribution-free assurance needs formal (UB=0) or workload-representative stimulus.\n")

print("###### ATTACK 3: cost-greedy planner neglects expensive-critical ######")
def run(policy, trials=200, budget=400):
    left=[]
    for _ in range(trials):
        props=[dict(w=50,n=0,cost=40)]+[dict(w=1,n=0,cost=1) for _ in range(30)]
        spent=0
        while spent<budget:
            if policy=='ratio':
                p=max(props,key=lambda p:(p['w']*(UB(p['n'])-UB(p['n']+1)))/p['cost'])
            else:
                crit=[p for p in props if p['w']>=10 and p['w']*UB(p['n'])>2.0]
                p=crit[0] if crit else max(props,key=lambda p:p['w']*UB(p['n']))
            spent+=p['cost']; p['n']+=1
        left.append(props[0]['w']*UB(props[0]['n']))
    return round(st.mean(left),3)
print(f"  residual risk left on the expensive-critical prop (w=50,cost=40):")
print(f"    RVR/cost greedy    = {run('ratio')}   <-- neglected")
print(f"    safety-constrained = {run('safety')}   <-- capped first")
print("  BREAK: ratio objective under-serves expensive-but-critical (deep uarch).")
print("  FIX: constrained form min cost s.t. RVR_j<=cap_j for all critical j.\n")

print("###### ATTACK 4: specification gap (unknown-unknowns) ######")
print("  RVR sums over the properties you WROTE. RVR=0 over Phi is compatible with a")
print("  catastrophic bug nobody specified (Spectre had no property).")
print("  FUNDAMENTAL LIMIT (shared by coverage & entropy): RVR bounds SPECIFIED risk only.")
print("  Mitigation: a spec-completeness term from mutation testing / assertion mining,")
print("  which RVR itself cannot certify.")

# ===== second wave: weight-gaming, non-stationarity, multiple-comparisons, axiom check =====
def UB_cp(n, alpha=0.95):      # frequentist Clopper-Pearson zero-failure bound (prior-free)
    return 1.0 if n==0 else 1-(1-alpha)**(1.0/n)

print("###### ATTACK 5: criticality-weight gaming ######")
print("  RVR=Σ w_j UB_j. Weights are assumed. Lower a hard property's weight -> RVR drops,")
print("  zero verification done.")
w_true=8; w_gamed=2; n=0
print(f"  critical prop, no evidence: honest RVR=w_true*UB={w_true*UB_cp(n):.2f}, "
      f"gamed RVR=w_gamed*UB={w_gamed*UB_cp(n):.2f}  (75% 'reduction' by relabelling)")
print("  VERDICT: real, but weaker than prior-gaming — weights encode CONSEQUENCE (auditable,")
print("  externally set: ASIL/DO-254 levels), not belief. FIX: weights fixed by a safety")
print("  authority separate from the verifier; RVR reported PER criticality class, never")
print("  aggregated across classes (can't hide a critical prop in a total).\n")

print("###### ATTACK 6: non-stationarity / commit — reduces to COI SOUNDNESS ######")
def commit_experiment(coi_sound, trials=4000):
    # property was verified (n=200 passes). A commit introduces a bug (true rho jumps 0->0.2).
    # The change DOES affect the property, but COI analysis may miss it.
    escapes=0
    for _ in range(trials):
        n_before=200
        # commit: does COI flag this property as needing re-verification?
        if coi_sound:
            flagged=True                      # sound = over-approx = never misses a real dep
        else:
            flagged=random.random()<0.5       # unsound = under-approx = misses half
        n_after = 0 if flagged else n_before  # sound resets evidence; unsound trusts stale
        rvr = 8*UB_cp(n_after)
        # 'escape' = we ship (rvr low) but the bug is real
        if rvr < 1.0:                          # tape-out threshold
            escapes += 1
    return escapes/trials
print(f"  P(ship a real post-commit bug):  sound COI = {commit_experiment(True):.3f}   "
      f"unsound COI = {commit_experiment(False):.3f}")
print("  VERDICT: cross-commit safety ⟺ COI SOUNDNESS. Sound (over-approx) COI => stale")
print("  evidence always invalidated when it might matter => safe (RVR only over-estimates).")
print("  AVA's cone_of_influence claims soundness; the whole burden reduces to that one property.\n")

print("###### ATTACK 8: multiple comparisons — summing confidence bounds is invalid ######")
for m in (1,10,100,1000):
    print(f"  m={m:4d} properties each at 95%: P(ALL per-prop bounds hold) = 0.95^m = {0.95**m:.3g}")
print("  BREAK: RVR-as-a-SIMULTANEOUS-95%-bound is false for many properties (family-wise error).")
print("  FIX (and it forces the aggregation axiom): interpret RVR as EXPECTED weighted escaped-")
print("  bug COUNT. Expectation is linear => additive EXACTLY, for dependent props too, no")
print("  multiple-comparisons problem. (Union-bound/Bonferroni is the alternative if you insist")
print("  on a simultaneous bound.)\n")

print("###### AXIOM CHECK: is the CP zero-failure bound a VALID + TIGHTEST α-upper bound? ######")
alpha=0.95
def coverage(ub_fn, trials=400000):
    bad=0; tot=0
    for _ in range(trials):
        rho=random.random()*0.15                      # true unknown rate
        n=random.randint(1,80)
        # observe n Bernoulli(rho); only zero-failure runs are 'passing evidence'
        if all(random.random()>=rho for _ in range(n)):
            tot+=1
            if rho > ub_fn(n): bad+=1                  # true rate exceeded our bound => bound failed
    return bad/tot                                     # should be <= 1-alpha = 0.05
cp   = coverage(lambda n: UB_cp(n,alpha))
tight= coverage(lambda n: 0.5*UB_cp(n,alpha))         # a deliberately TIGHTER (smaller) bound
print(f"  CP bound      exceed-rate = {cp:.4f}   (valid iff <= {1-alpha})")
print(f"  tighter bound exceed-rate = {tight:.4f}   (INVALID: under-covers => unsafe)")
print("  => the CP zero-failure bound is valid; anything tighter under-covers. It is the")
print("     TIGHTEST valid distribution-free α-bound => forced by the axioms (uniqueness).")
