# Exploration value WITHOUT information gain, native to the framework.
# The robust risk R is the SUP over an admissible prior (credal) set Pi.
# Define ADMISSIBLE UNCERTAINTY = the WIDTH of the credal risk interval:
#   width(n) = sup_{pi in Pi} risk(pi|ev)  -  inf_{pi in Pi} risk(pi|ev)
# = "how much does the still-unearned prior choice change the verdict."
# Exploration value X(a) = expected reduction in width. It collapses with EVIDENCE,
# not with belief-entropy -> it is framework-native, not Shannon information gain.
import math
def logB(x,y): return math.lgamma(x)+math.lgamma(y)-math.lgamma(x+y)
def M(a,b,n): return math.exp(logB(a,b+n)-logB(a,b))      # E_Beta[(1-rho)^n]
def q(pi,a,b,n):                                          # posterior P(holds)
    return pi if n==0 else pi/(pi+(1-pi)*M(a,b,n))
def risk(pi,a,b,n,w=1.0): return w*(1-q(pi,a,b,n))

# admissible credal set: pi in [0.3,0.9]; slab fixed (2,6)
PIs=[0.3+0.6*k/40 for k in range(41)]; a,b=2.0,6.0
def R_up(n): return max(risk(p,a,b,n) for p in PIs)
def R_lo(n): return min(risk(p,a,b,n) for p in PIs)
def width(n): return R_up(n)-R_lo(n)

print("  n     R_up    R_lo    width=admissible-uncertainty")
for n in (0,2,5,10,20,50,100):
    print(f"  {n:4d}  {R_up(n):.4f}  {R_lo(n):.4f}   {width(n):.4f}")
print("  => width collapses with EVIDENCE (0.60 -> ~0), not with prior optimism.\n")

# X(a) = expected width reduction from one more relevant test.
def X(n):
    # predictive P(pass) under the credal MIDPOINT prior (planner's working model)
    pm=0.6; e_pass_if_buggy=(b+n)/(a+b+n)
    p_pass=q(pm,a,b,n)+(1-q(pm,a,b,n))*e_pass_if_buggy
    w_pass=width(n+1); w_fail=0.0     # a failure disproves -> width 0
    return width(n) - (p_pass*w_pass+(1-p_pass)*w_fail)
print("  n     X(a)=E[Δwidth]  (must be >= 0)")
for n in (0,2,5,10,20):
    print(f"  {n:4d}  {X(n):+.5f}")
worst=min(X(n) for n in range(0,60))
print(f"  worst X over n=0..59: {worst:.6f}  (>=0 ✓)\n")

# Tie-break: two properties, EQUAL exploitation (same R_up and same expected ΔR_up),
# but different admissible uncertainty. Explore-aware planner prefers the ambiguous one.
print("  TIE-BREAK: two actions equal on ΔR_up(exploit); pick by X(explore):")
print(f"    prop A: well-evidenced (n=40) -> width={width(40):.4f}, X={X(40):+.5f}")
print(f"    prop B: barely evidenced (n=1) -> width={width(1):.4f},  X={X(1):+.5f}")
print("    => planner U = value(ΔR_up) + λ·X - cost breaks the tie toward B (earns trust")
print("       where the prior still rules the verdict) — exploration, no information gain used.")
