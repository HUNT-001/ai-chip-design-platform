"""
Rigorous numerical validation of the VWS mathematical foundation.
Model: spike-and-slab belief over per-property violation rate rho_j.
  theta*_j = 1 (property holds)  <=>  rho_j = 0.
  prior: holds w.p. pi (spike at 0); else rho ~ Beta(a,b) on (0,1].
  evidence: each RELEVANT test independently reveals a violation w.p. rho.
Closed-form posterior after n passing relevant tests:
  q_n = pi / ( pi + (1-pi) * E_Beta[(1-rho)^n] ),
        E_Beta(a,b)[(1-rho)^n] = B(a, b+n)/B(a,b).
"""
import math, random
random.seed(20260726)

def logB(x, y):                         # log Beta function
    return math.lgamma(x) + math.lgamma(y) - math.lgamma(x + y)

def slab_moment(a, b, n):               # E_Beta(a,b)[(1-rho)^n] = B(a,b+n)/B(a,b)
    return math.exp(logB(a, b + n) - logB(a, b))

def q_after_passes(pi, a, b, n):        # posterior P(holds) after n passing tests
    if n == 0: return pi
    r = slab_moment(a, b, n)
    return pi / (pi + (1 - pi) * r)

def H2(p):
    if p <= 0.0 or p >= 1.0: return 0.0
    return -p*math.log2(p) - (1-p)*math.log2(1-p)

# ── TEST 1: reduction to the classical rule of three ────────────────────────
# Flat prior on rho (pi->0, slab Beta(1,1)=uniform): posterior on rho after n
# zero-failure tests is Beta(1, n+1). 95th percentile upper bound ~ 3/n.
def rule_of_three_check():
    out=[]
    for n in (50, 100, 300, 1000):
        # posterior rho ~ Beta(1, n+1); P(rho > x) = (1-x)^(n+1). 95% UB: solve (1-x)^(n+1)=0.05
        x95 = 1 - 0.05**(1.0/(n+1))
        out.append((n, round(x95, 5), round(3.0/n, 5)))
    return out

# ── TEST 2: Bayesian calibration (well-specified) ───────────────────────────
def calibration(pi=0.6, a=1.5, b=6.0, ntests=40, trials=200000):
    bins = {}   # bin -> [held, total] among all-passed properties with q in (0,1)
    for _ in range(trials):
        holds = random.random() < pi
        rho = 0.0 if holds else random.betavariate(a, b)
        # run relevant tests until a failure or ntests
        failed = False
        passes = 0
        for _ in range(ntests):
            if random.random() < rho:
                failed = True; break
            passes += 1
        if failed:
            q = 0.0
        else:
            q = q_after_passes(pi, a, b, passes)
        if 0.02 < q < 0.999:
            k = int(q*10)
            bins.setdefault(k, [0,0])
            bins[k][0] += 1 if holds else 0
            bins[k][1] += 1
    rows=[]
    max_err=0.0
    for k in sorted(bins):
        held,tot = bins[k]
        if tot < 200: continue
        emp = held/tot; mid=(k+0.5)/10
        max_err=max(max_err, abs(emp-mid))
        rows.append((f"[{k/10:.1f},{(k+1)/10:.1f})", tot, round(emp,3), round(mid,3)))
    return rows, max_err

# ── TEST 3: EIG >= 0 and expected-uncertainty monotonicity ──────────────────
def eig_nonneg(pi=0.5, a=2.0, b=5.0, trials=20000):
    # action: run 1 more relevant test on a property currently at n passes.
    worst=0.0
    for _ in range(trials):
        n = random.randint(0, 60)
        q = q_after_passes(pi, a, b, n)      # current P(holds)
        U = H2(q)
        # predictive: given belief, P(next test passes)
        # = P(holds)*1 + P(buggy)*E[1-rho | buggy, n passes]
        # posterior over rho in slab after n passes ~ Beta(a, b+n); E[1-rho]=(b+n)/(a+b+n)
        p_hold = q
        e_pass_if_buggy = (b+n)/(a+b+n)
        p_pass = p_hold*1.0 + (1-p_hold)*e_pass_if_buggy
        q_pass = q_after_passes(pi, a, b, n+1)   # if it passes
        q_fail = 0.0                              # if it fails -> disproven
        EU = p_pass*H2(q_pass) + (1-p_pass)*H2(q_fail)
        eig = U - EU
        worst=min(worst, eig)
    return worst

# ── TEST 4: greedy EIG/cost planner vs random vs round-robin ────────────────
def planner_compare(m=40, budget=1500, trials=40):
    def make_world():
        props=[]
        for j in range(m):
            pi = random.uniform(0.3,0.9)
            a = random.uniform(1,3); b=random.uniform(3,12)
            holds = random.random() < pi
            rho = 0.0 if holds else random.betavariate(a,b)
            w = random.choice([1,1,1,3,5])          # criticality
            cost = random.choice([1,1,2,4])         # test cost
            props.append(dict(pi=pi,a=a,b=b,holds=holds,rho=rho,w=w,cost=cost,n=0,dead=False,disproven=False))
        return props
    def q_of(p):
        if p['disproven']: return 0.0
        return q_after_passes(p['pi'],p['a'],p['b'],p['n'])
    def U_of(props):
        return sum(p['w']*H2(q_of(p)) for p in props)
    def eig_cost(p):
        if p['disproven']: return 0.0
        q=q_of(p); U=p['w']*H2(q)
        e_pass_if_buggy=(p['b']+p['n'])/(p['a']+p['b']+p['n'])
        p_pass=q+(1-q)*e_pass_if_buggy
        qp=q_after_passes(p['pi'],p['a'],p['b'],p['n']+1)
        EU=p['w']*(p_pass*H2(qp)+(1-p_pass)*0.0)
        return (U-EU)/p['cost']
    def run(policy):
        props=make_world(); U0=U_of(props); spent=0; rr=0
        while spent<budget:
            live=[p for p in props if not p['disproven']]
            if not live: break
            if policy=='greedy':
                p=max(props,key=eig_cost)
                if eig_cost(p)<=1e-12: break
            elif policy=='random':
                p=random.choice([x for x in props if not x['disproven']])
            else:  # round robin
                p=props[rr%m]; rr+=1
                if p['disproven']:
                    continue
            # run one relevant test
            spent+=p['cost']
            if (not p['holds']) and random.random()<p['rho']:
                p['disproven']=True
            else:
                p['n']+=1
        return U_of(props), U0
    import statistics as st
    res={pol:[] for pol in ('greedy','random','roundrobin')}
    for _ in range(trials):
        for pol in res:
            Uf,U0=run(pol); res[pol].append(Uf)   # lower final U = more knowledge
    return {pol: round(st.mean(v),3) for pol,v in res.items()}

# ── TEST 5: convergence U_t -> 0 under greedy (full budget) ─────────────────
def convergence(m=25):
    r = planner_compare(m=m, budget=100000, trials=15)
    return r

print("=== TEST 1: rule-of-three reduction (n, Bayes 95% UB, 3/n) ===")
for row in rule_of_three_check(): print("  ", row)
print("\n=== TEST 2: calibration (well-specified) ===")
rows,err = calibration()
for r in rows: print("   bin",r[0],"n=",r[1]," empirical_hold=",r[2]," predicted=",r[3])
print("   MAX calibration error:", round(err,4))
print("\n=== TEST 3: min EIG over 20k random states (must be >= 0) ===")
print("   worst EIG =", round(eig_nonneg(),8))
print("\n=== TEST 4: planner final uncertainty (lower=better) ===")
print("  ", planner_compare())
print("\n=== TEST 5: greedy w/ large budget -> U near 0 ===")
print("  ", convergence())
def slab_moment(a,b,n): return math.exp(logB(a,b+n)-logB(a,b))
def q_after(pi,a,b,n):
    if n==0: return pi
    return pi/(pi+(1-pi)*slab_moment(a,b,n))

# Proper calibration: bin by posterior q, compare EMPIRICAL hold-rate to MEAN q in bin.
# Spread q by (a) variable evidence budget per property and (b) variable prior pi.
def calibration(trials=1200000):
    bins={k:[0.0,0.0,0] for k in range(10)}   # bin -> [sum_q, held, total]
    for _ in range(trials):
        pi=random.uniform(0.2,0.85)
        a=random.uniform(1,3); b=random.uniform(2,10)
        holds = random.random()<pi
        rho = 0.0 if holds else random.betavariate(a,b)
        ntests=random.randint(0,50)            # variable evidence -> q spreads
        passes=0; failed=False
        for _ in range(ntests):
            if random.random()<rho: failed=True; break
            passes+=1
        q = 0.0 if failed else q_after(pi,a,b,passes)
        k=min(9,int(q*10))
        bins[k][0]+=q; bins[k][1]+= 1 if holds else 0; bins[k][2]+=1
    print("  bin      n       mean_q   emp_hold   |err|")
    maxerr=0
    for k in range(10):
        s,h,t=bins[k]
        if t<500: continue
        mq=s/t; emp=h/t; e=abs(mq-emp); maxerr=max(maxerr,e)
        print(f"  {k/10:.1f}-{(k+1)/10:.1f}  {t:8d}  {mq:.3f}    {emp:.3f}     {e:.3f}")
    print("  MAX |mean_q - empirical| =", round(maxerr,4),
          "(calibrated if small across all populated bins)")
calibration()
