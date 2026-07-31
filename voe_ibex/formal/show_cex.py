"""Print the counterexample inputs from an sby trace.vcd.

Usage:
    python3 show_cex.py ibex_alu_prove_shift/engine_0/trace.vcd

Dumps the value of every top-level harness signal (op/a/b and the compared
outputs) at each time step, so a failing assertion can be diagnosed from the
concrete inputs rather than guessed at.
"""
import re, sys
from collections import defaultdict

def main(path):
    ids, widths, cur = {}, {}, {}
    hist = defaultdict(list)
    want = ("op", "a", "b", "result", "g_res", "cmp", "g_cmp", "is_eq", "clk")
    with open(path) as f:
        in_defs = True
        t = 0
        for line in f:
            line = line.strip()
            m = re.match(r"\$var\s+\w+\s+(\d+)\s+(\S+)\s+(\S+)", line)
            if m and in_defs:
                w, sid, name = int(m.group(1)), m.group(2), m.group(3)
                ids[sid] = name
                widths[sid] = w
                continue
            if line.startswith("$enddefinitions"):
                in_defs = False
                continue
            if line.startswith("#"):
                if cur:
                    for n, v in cur.items():
                        hist[t].append((n, v))
                t = int(line[1:])
                continue
            m = re.match(r"^b([01xz]+)\s+(\S+)$", line)
            if m:
                val, sid = m.group(1), m.group(2)
                n = ids.get(sid)
                if n:
                    cur[n] = val
                continue
            m = re.match(r"^([01xz])(\S+)$", line)
            if m:
                val, sid = m.group(1), m.group(2)
                n = ids.get(sid)
                if n:
                    cur[n] = val
    for n, v in sorted(cur.items()):
        base = n.split("[")[0]
        if base in want or any(w == base for w in want):
            try:
                dec = int(v, 2)
                print(f"  {n:24s} = {v}  (0x{dec:X}, {dec})")
            except ValueError:
                print(f"  {n:24s} = {v}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(1)
    main(sys.argv[1])
