# Struct-5 as an INVARIANCE PRINCIPLE, tested:
# Model the failure space as a finite set of ATOMS (distinct violating behaviours).
# A property = the subset of atoms that violate it. Refining P into {P_i} means
# fail(P) = union_i fail(P_i). Risk should be a MEASURE on the failure sigma-algebra:
#   R(P) = weighted count of UNRESOLVED atoms in fail(P), each atom counted ONCE.
# Claim: measure-risk is INVARIANT under how you refine (basis-independent);
#        naive additive-over-properties is NOT (it double-counts shared atoms).
import random
random.seed(1)

ATOMS = list(range(20))                      # 20 distinct failure behaviours
resolved = set(random.sample(ATOMS, 8))      # 8 have been ruled out by evidence
w = 1.0
def unresolved(S): return [a for a in S if a not in resolved]

# Property P fails on this atom set:
failP = set(range(0,12))                     # atoms 0..11

# --- two DIFFERENT refinements of the SAME property (same fail set, different partitions/covers)
# refinement X: three OVERLAPPING sub-properties whose union = failP
X = [set(range(0,6)), set(range(4,10)), set(range(8,12))]     # overlaps at 4,5,8,9
# refinement Y: a DIFFERENT overlapping cover, union = failP
Y = [set(range(0,8)), set(range(6,12)), set(range(2,5))]      # different overlaps
assert set().union(*X)==failP and set().union(*Y)==failP

def R_measure(cover):    # measure: atoms in the UNION, counted once
    return w*len(unresolved(set().union(*cover)))
def R_naive(cover):      # naive additive over sub-properties: double counts shared atoms
    return w*sum(len(unresolved(s)) for s in cover)

print("  property P, fail atoms 0..11, 8 atoms resolved globally")
print(f"  MEASURE risk:  refinement X = {R_measure(X)},  refinement Y = {R_measure(Y)}   (equal?  {R_measure(X)==R_measure(Y)})")
print(f"  NAIVE  risk:   refinement X = {R_naive(X)},  refinement Y = {R_naive(Y)}   (equal?  {R_naive(X)==R_naive(Y)})")
print(f"  unrefined P direct measure = {w*len(unresolved(failP))}")
print()
print("  => MEASURE risk is basis-invariant (same for X, Y, and direct) — coordinate-free.")
print("     NAIVE additive is basis-DEPENDENT (X != Y) — the double-counting bug.")
print()

# --- the composition rule F: measure via inclusion-exclusion on a cover, OR just partition first
def R_incl_excl(cover):
    # Moebius / inclusion-exclusion over the cover reproduces the measure exactly
    from itertools import combinations
    total=0.0
    n=len(cover)
    for k in range(1,n+1):
        s=1 if k%2==1 else -1
        for combo in combinations(cover,k):
            inter=set.intersection(*combo)
            total+= s*len(unresolved(inter))
    return w*total
print(f"  inclusion-exclusion on cover X = {R_incl_excl(X)}  (matches measure {R_measure(X)}? {R_incl_excl(X)==R_measure(X)})")
print(f"  inclusion-exclusion on cover Y = {R_incl_excl(Y)}  (matches measure {R_measure(Y)}? {R_incl_excl(Y)==R_measure(Y)})")
print()
print("  THEOREM (Struct-5): define R as a measure on the failure sigma-algebra of Phi.")
print("  Then R(P)=F(R(P1..Pn)) with F = inclusion-exclusion (Moebius) composition,")
print("  which is INVARIANT to the refinement/cover. If Phi is a PARTITION basis (disjoint")
print("  atoms), F collapses to plain addition. Verification depends on the MEANING")
print("  (failure sigma-algebra) of the spec, not its description. QED-by-construction + validated.")
