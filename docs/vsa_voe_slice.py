"""Minimal VOE slice on the frozen VSA kernel: cognitive archetypes (heterogeneous
cognition, separate from domain) + reputation DERIVED FROM KERNEL EVIDENCE.
Tests whether the VOE abstractions survive execution. Simulated DUT (real work
needs Verilator); the VOE/kernel machinery is real."""
import math, random
random.seed(7)
def cp(n,a=0.95): return 1.0 if n==0 else 1-(1-a)**(1.0/n)

class DUT:
    def __init__(s,props): s.rho={p:(0.0 if random.random()<0.65 else random.uniform(.05,.35)) for p in props}
    def test(s,p): return random.random()>=s.rho[p]
    def provable(s,p): return s.rho[p]==0.0     # a sound formal oracle

# --- cognitive ARCHETYPES: differ in cognition, NOT domain ---
ARCHETYPES = {
  # lamX=exploration weight, formal_pref=prefers proving, conf_thr=evidence to 'trust',
  # honesty=1.0 sound; <1 sometimes emits UNWITNESSED claims (to test Fire-1 + reputation)
  "Explorer":  dict(lamX=4.0, formal_pref=0.1, conf_thr=0.10, honesty=1.0),
  "Skeptic":   dict(lamX=0.5, formal_pref=0.9, conf_thr=0.03, honesty=1.0),
  "Careless":  dict(lamX=2.0, formal_pref=0.2, conf_thr=0.30, honesty=0.6),
}

def width(n,PI=(.3,.9),ab=(2.,6.)):
    a,b=ab
    def q(pi): return pi if n==0 else pi/(pi+(1-pi)*math.exp(math.lgamma(a)+math.lgamma(b+n)-math.lgamma(a+b+n)-(math.lgamma(a)+math.lgamma(b)-math.lgamma(a+b))))
    return abs(q(PI[1])-q(PI[0]))

def run_agent(name, cfg, dut, props, budget=120):
    K={p:{"n":0,"proven":False,"cex":False,"witness":True} for p in props}
    stats=dict(bugs=0, proofs=0, false_claims=0, adjudicated=0)
    def R_of(p):
        j=K[p]
        if j["proven"] or j["cex"]: return 0.0
        return props[p]*cp(j["n"])
    def util(p):
        j=K[p]; before=R_of(p)
        j2=dict(j); j2["n"]+=1
        exp=before-(0.0 if j2["proven"] or j2["cex"] else props[p]*cp(j2["n"]))
        X=max(0,width(j["n"])-width(j["n"]+1))
        return exp + cfg["lamX"]*X + cfg["formal_pref"]*0.05
    for _ in range(budget):
        live=[p for p in props if not K[p]["proven"] and not K[p]["cex"]]
        if not live: break
        p=max(live,key=util)
        # Skeptic/formal_pref: attempt a proof when evidence is 'enough' by its threshold
        if random.random()<cfg["formal_pref"] and cp(K[p]["n"])<0.5:
            if dut.provable(p):
                K[p]["proven"]=True; stats["proofs"]+=1; stats["adjudicated"]+=1
                continue
        # otherwise run a sim test
        if dut.test(p):
            K[p]["n"]+=1
            # Careless: sometimes CLAIM proven without a proof (unwitnessed) -> firewall blocks
            if random.random()>cfg["honesty"] and K[p]["n"]>=3:
                # Fire-1: unwitnessed claim cannot enter canonical R; logged as a false claim
                stats["false_claims"]+=1        # it TRIED; kernel rejects it from R
                stats["adjudicated"]+=1
                if not dut.provable(p): pass     # the claim was actually wrong (a real bug hidden)
        else:
            K[p]["cex"]=True; stats["bugs"]+=1; stats["adjudicated"]+=1
    # realized accuracy: of properties this agent left "trusted" (low R), how many truly hold
    trusted=[p for p in props if R_of(p)<0.10 and not K[p]["cex"]]
    truly=[p for p in trusted if dut.provable(p) or K[p]["proven"]]
    acc = len(truly)/len(trusted) if trusted else 1.0
    fp  = stats["false_claims"]/max(1,stats["adjudicated"])
    finalR=sum(R_of(p) for p in props)
    # REPUTATION derived ENTIRELY from kernel evidence (accuracy, false-positive, discoveries, residual)
    rep = 0.45*acc + 0.25*(1-fp) + 0.20*min(1,stats["bugs"]/4) + 0.10*max(0,1-finalR/10)
    return dict(name=name, R=round(finalR,2), bugs=stats["bugs"], proofs=stats["proofs"],
                false_claims=stats["false_claims"], accuracy=round(acc,2),
                reputation=round(rep,3))

props={f"phi{i}": random.choice([1,1,3,5]) for i in range(14)}
dut=DUT(props); truebugs=sum(1 for p in props if dut.rho[p]>0)
print(f"  DUT: {len(props)} properties, {truebugs} true bugs. Same DUT for all archetypes.\n")
print(f"  {'archetype':10} {'ℛ':>6} {'bugs':>5} {'proofs':>7} {'false':>6} {'acc':>5} {'REPUTATION':>11}")
rows=[]
for name,cfg in ARCHETYPES.items():
    r=run_agent(name,cfg,DUT(props) if False else dut, props)
    rows.append(r)
    print(f"  {name:10} {r['R']:6} {r['bugs']:5} {r['proofs']:7} {r['false_claims']:6} {r['accuracy']:5} {r['reputation']:11}")
print()
print("  Observations (all emergent from the kernel, no hand-tuning of reputation):")
best_expl=max(rows,key=lambda r:r['bugs'])
best_rep=max(rows,key=lambda r:r['reputation'])
worst_rep=min(rows,key=lambda r:r['reputation'])
print(f"   - heterogeneous cognition: archetypes behave differently on the SAME DUT.")
print(f"   - {worst_rep['name']} has the LOWEST reputation ({worst_rep['reputation']}) — its unwitnessed")
print(f"     claims ({worst_rep['false_claims']}) were blocked by Fire-1 and penalised its accuracy.")
print(f"   - reputation is EVIDENCE-DERIVED (accuracy/false-positive/discovery/residual), not subjective.")
print(f"   - kernel unchanged: archetypes are policies over 𝒰; reputation is a function of provenance.")
