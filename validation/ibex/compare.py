#!/usr/bin/env python3
"""Run both comparators on one Ibex/Spike pair and say what each concluded (#175).

The orchestrator's comparator (`SpikeISS._compare_results`) gets the logs raw,
as a real run hands them over; it cuts Spike's tohost spin itself (#165).
AGENT_D's CLI gets them twice: raw, and cut by the same rule
(`ava_patched.align_program_end`), because AGENT_D has no cut of its own and
Spike retires `j .` thousands of times after the program ends.

Usage: compare.py RTL.jsonl ISS.jsonl [--json]
"""
import argparse
import asyncio
import copy
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ava_patched import SpikeISS, align_program_end  # noqa: E402

CLI = REPO_ROOT / "AGENT_D" / "compare_commitlogs.py"
LINK_SCRIPT = Path(__file__).resolve().parent / "programs" / "link.ld"


def sim_ctrl_halt_address(link_script: Path = LINK_SCRIPT) -> str:
    """The address whose write halts Ibex's simulation, as the programs are built.

    link.ld places `tohost` on Ibex simple_system's SimCtrl halt word
    (SIM_CTRL_BASE 0x20000 + SIM_CTRL_CTRL 0x8 in Ibex's simple_system_regs.h;
    tests/test_program_end_declared_halt.py checks the two agree against the
    cloned tree).  It is read here, not written into the comparator (#178).
    """
    m = re.search(r"^\s*tohost\s*=\s*(0x[0-9a-fA-F]+)\s*;", link_script.read_text(encoding="utf-8"), re.M)
    if m is None:
        raise RuntimeError(f"{link_script}: no `tohost = 0x...;` to take the halt address from")
    return f"0x{int(m.group(1), 16):08x}"


#: What the Ibex testbench attests about its own log: it stopped because the
#: program wrote to SimCtrl's halt word.  The comparator checks this against the
#: log (ava_patched.declared_stop_problems); an Ibex log that does not end on
#: that store fails with a finding.
IBEX_RTL_ENDED = {"reason": "halt_write", "addr": sim_ctrl_halt_address()}


def load(path: Path) -> List[Dict[str, Any]]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def cut(rtl, iss, rtl_ended=None):
    """Both logs up to the program's end (#165); `rtl_ended` as in #178."""
    r, i, summary, finding = align_program_end(
        copy.deepcopy(rtl), copy.deepcopy(iss), rtl_ended)
    return r, i, summary, finding


def orchestrator(rtl, iss, run_dir: Path, rtl_ended=None) -> Dict[str, Any]:
    """`rtl_ended` is the DUT's declaration of why its log stopped, carried with
    the log the way a real run would carry it; None declares nothing."""
    sim = SpikeISS(timeout=30, run_dir=run_dir)
    rtl_results = {"commit_log": copy.deepcopy(rtl), "instructions": len(rtl)}
    if rtl_ended is not None:
        rtl_results["rtl_ended"] = copy.deepcopy(rtl_ended)
    c = asyncio.run(sim._compare_results(
        rtl_results,
        {"commit_log": copy.deepcopy(iss), "instructions": len(iss)}))
    verdict = "mismatch" if c["bugs"] else ("incomplete" if c.get("unobserved") else "pass")
    return {"verdict": verdict,
            "findings": [f"{b.get('kind')}: {b.get('description')}" for b in c["bugs"]],
            "unobserved": list(c.get("unobserved", [])),
            "fields_not_compared": dict(c.get("fields_not_compared", {})),
            "program_end": c.get("program_end"),
            "warnings": list(c.get("warnings", []))}


def agent_d(rtl, iss, workdir: Path, extra: Sequence[str] = ()) -> Dict[str, Any]:
    r, i, rep = workdir / "rtl.jsonl", workdir / "iss.jsonl", workdir / "report.json"
    for path, log in ((r, rtl), (i, iss)):
        path.write_text("".join(json.dumps(e) + "\n" for e in log), encoding="utf-8")
    if rep.exists():
        rep.unlink()
    # --all alone keeps the default --max-mismatches 1, so ask for every one.
    proc = subprocess.run([sys.executable, str(CLI), str(r), str(i), "--all",
                           "--max-mismatches", "0", "--quiet",
                           "--bug-report", str(rep), *extra],
                          cwd=REPO_ROOT, capture_output=True, text=True, timeout=300)
    out: Dict[str, Any] = {"exit": proc.returncode,
                           "verdict": {0: "pass", 1: "mismatch", 3: "incomplete"}.get(
                               proc.returncode, f"error({proc.returncode})")}
    if rep.exists():
        d = json.loads(rep.read_text(encoding="utf-8"))
        out["incomplete_reasons"] = d.get("incomplete_reasons", [])
        out["not_compared"] = d.get("not_compared", {})
        out["mismatches"] = [
            f"{m.get('mismatch_type') or m.get('type')} step={m.get('step')} "
            f"field={m.get('field')}: {m.get('description') or m.get('message')}"
            for m in d.get("mismatches", [])][:20]
        out["n_mismatches"] = len(d.get("mismatches", []))
    else:
        out["stderr"] = proc.stderr[-800:]
    return out


HALT_ADDR = IBEX_RTL_ENDED["addr"]   # SimCtrl halt on Ibex, tohost on Spike


def is_halt_store(e: Dict[str, Any]) -> bool:
    return any(w.get("addr") == HALT_ADDR for w in (e.get("mem_writes") or []))


def cut_at_halt(rtl, iss):
    """Both logs up to and including the store that halts the program.

    Ibex's SimCtrl ends the simulation on that store, so its trace stops
    there.  Spike runs on until it polls tohost.  This is a second view, kept
    apart from the raw one, so the fields can be judged separately from the
    end-of-test difference.  It cuts only at a store to HALT_ADDR at the same
    index and PC on both sides, and returns None otherwise.
    """
    k = next((n for n, e in enumerate(rtl) if is_halt_store(e)), None)
    if k is None or k >= len(iss) or iss[k].get("pc") != rtl[k].get("pc") or not is_halt_store(iss[k]):
        return None
    return rtl[:k + 1], iss[:k + 1], {"halt_index": k, "rtl_dropped": len(rtl) - k - 1,
                                      "iss_dropped": len(iss) - k - 1}


def compare_pair(rtl, iss, agent_d_extra: Sequence[str] = ()) -> Dict[str, Any]:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        rc, ic, summary, finding = cut(rtl, iss, IBEX_RTL_ENDED)
        res = {
            "rtl_entries": len(rtl), "iss_entries": len(iss),
            "orchestrator": orchestrator(rtl, iss, td / "orch", IBEX_RTL_ENDED),
            "agent_d_raw": agent_d(rtl, iss, td, agent_d_extra),
            "cut": {"summary": summary, "finding": finding.description if finding else None,
                    "rtl_entries": len(rc), "iss_entries": len(ic)},
            "agent_d_cut": agent_d(rc, ic, td, agent_d_extra),
        }
        h = cut_at_halt(rtl, iss)
        res["halt_cut"] = None if h is None else h[2]
        if h is not None:
            res["orchestrator_at_halt"] = orchestrator(h[0], h[1], td / "orch_halt")
            res["agent_d_at_halt"] = agent_d(h[0], h[1], td, agent_d_extra)
        return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("rtl")
    ap.add_argument("iss")
    ap.add_argument("-o", "--output", help="write the JSON here (the orchestrator logs to stdout)")
    args = ap.parse_args(argv)
    res = compare_pair(load(Path(args.rtl)), load(Path(args.iss)))
    text = json.dumps(res, indent=2, default=str)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
