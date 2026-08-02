"""Print the counterexample state from an sby trace.vcd, per time step.

Usage:
    python3 show_cex.py ibex_alu_prove_shift/engine_0/trace.vcd

Signals are reported with their FULL scope path (an earlier version keyed on the
bare name, so identically-named signals in different scopes collided and the
printed state did not correspond to the failing step). Every time step is shown
so the state at the failing step is unambiguous.
"""
import re, sys


def main(path):
    ids, scope = {}, []
    cur, steps = {}, []
    t = None
    with open(path) as f:
        for line in f:
            s = line.strip()
            m = re.match(r"\$scope\s+\w+\s+(\S+)", s)
            if m:
                scope.append(m.group(1)); continue
            if s.startswith("$upscope"):
                if scope: scope.pop()
                continue
            m = re.match(r"\$var\s+\w+\s+(\d+)\s+(\S+)\s+([^\s$]+)", s)
            if m:
                ids[m.group(2)] = ".".join(scope + [m.group(3)])
                continue
            if s.startswith("#"):
                if t is not None:
                    steps.append((t, dict(cur)))
                t = int(s[1:])
                continue
            m = re.match(r"^b([01xz]+)\s+(\S+)$", s)
            if m and m.group(2) in ids:
                cur[ids[m.group(2)]] = m.group(1); continue
            m = re.match(r"^([01xz])(\S+)$", s)
            if m and m.group(2) in ids:
                cur[ids[m.group(2)]] = m.group(1)
    if t is not None:
        steps.append((t, dict(cur)))

    want = re.compile(r"\b(op|a|b|result|g_res|cmp|g_cmp|is_eq)$")
    for t, st in steps:
        print(f"\n--- time #{t} ---")
        for n in sorted(st):
            if want.search(n) and n.count(".") <= 2:
                v = st[n]
                try:
                    print(f"  {n:34s} = {v}  (0x{int(v,2):X})")
                except ValueError:
                    print(f"  {n:34s} = {v}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(1)
    main(sys.argv[1])
