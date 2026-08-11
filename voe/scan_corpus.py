"""Scan the corpus for modules that can serve as held-out evaluation designs.

Held-out designs are the precondition for any honest claim that a policy
improved: `promote()` refuses to certify a policy on a design it was developed
against, so without fresh designs the organisation can never legitimately learn
anything. This finds them, using the platform's own corpus-hardened parser.

Selection criteria (deliberately conservative — a candidate must be verifiable
without a research project of its own):

    * parses cleanly, no parse warnings
    * no package imports (`import x::*`) — self-contained
    * no submodule instances — a leaf block
    * modest size, with real outputs to constrain

    python scan_corpus.py [--limit N]
"""
from __future__ import annotations
import os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
CORPUS = os.path.join(ROOT, "corpus")

_IMPORT = re.compile(r"^\s*import\s+\w+::", re.M)


def candidates(max_lines=400, limit=None):
    from AGENT_H import rtl_graph as rg
    out = []
    for dirpath, _dirs, files in os.walk(CORPUS):
        for fn in files:
            if not fn.endswith((".sv", ".v")):
                continue
            path = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(path) > 40000:
                    continue
                with open(path, errors="ignore") as f:
                    src = f.read()
            except OSError:
                continue
            if _IMPORT.search(src):
                continue                       # needs a package
            nlines = src.count("\n") + 1
            if nlines > max_lines or nlines < 20:
                continue
            try:
                mods = rg.parse_module(src, path)
            except Exception:
                continue
            if len(mods) != 1:
                continue                       # one module per file keeps it simple
            m = mods[0]
            if m.instances or m.parse_warnings:
                continue                       # leaf blocks only
            outs = [p for p in m.ports if p.direction == "output"]
            ins = [p for p in m.ports if p.direction == "input"]
            if not outs or not ins:
                continue
            has_clk = any(re.search(r"\bclk", p.name, re.I) for p in ins)
            out.append({
                "module": m.name, "path": os.path.relpath(path, ROOT),
                "lines": nlines, "in": len(ins), "out": len(outs),
                "signals": len(m.signals), "assigns": len(m.assigns),
                "sequential": has_clk,
                "always": len(m.always_blocks),
            })
            if limit and len(out) >= limit:
                return out
    return out


def main():
    lim = None
    if "--limit" in sys.argv:
        lim = int(sys.argv[sys.argv.index("--limit") + 1])
    rows = candidates(limit=lim)
    rows.sort(key=lambda r: (not r["sequential"], r["lines"]))
    print(f"  {len(rows)} self-contained leaf modules found\n")
    print(f"  {'module':28s} {'lines':>5} {'in':>3} {'out':>3} {'sig':>4} "
          f"{'seq':>4}  path")
    for r in rows[:40]:
        print(f"  {r['module'][:28]:28s} {r['lines']:5d} {r['in']:3d} {r['out']:3d} "
              f"{r['signals']:4d} {'yes' if r['sequential'] else ' no':>4}  {r['path']}")


if __name__ == "__main__":
    main()
