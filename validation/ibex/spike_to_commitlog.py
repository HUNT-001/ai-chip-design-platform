#!/usr/bin/env python3
"""Spike --log-commits output -> commit-log JSONL (schema 2.1.0), the ISS side.

The repo's own parser (`AGENT_F.coverage_pipeline.parse_spike_commit_log`)
reads the log, and `align_to_entry` starts it at the ELF entry.  This is the
same path tests/test_comparator_mutation_table.py takes.  The records are
shaped by the schema, the same way validation/ibex/trace_to_commitlog.py
shapes the Ibex side:

* `regs` is the register write, `csrs` the CSR writes, and `priv` Spike's
  privilege.  A trapping record has no `priv`: Spike prints none on an
  exception line.
* `trap` is an object on an exception line and null otherwise.  Its `tvec` is
  the last mtvec Spike logged before the trap, mode bits cleared, and absent
  when the log holds no mtvec write before it (#184).
* `mem_writes` appears only on a store, with its size taken from funct3.
  Spike prints no size.
* Spike prints no `tval`, so a trap carries `0x00000000`, as the mutation
  table's does.

Usage: spike_to_commitlog.py SPIKE_LOG ELF [-o OUT.jsonl]
"""
import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import AGENT_F.coverage_pipeline as cp  # noqa: E402

_STORE_OPCODE = 0b0100011


def convert(log_text: str, elf: Path) -> List[Dict[str, Any]]:
    entries = cp.with_trap_vectors(cp.parse_spike_commit_log(log_text))
    aligned, reason = cp.align_to_entry(entries, cp.elf_entry_pc(elf))
    if reason is not None:
        raise ValueError(f"{elf}: cannot align the Spike log: {reason}")
    out: List[Dict[str, Any]] = []
    for seq, e in enumerate(aligned):
        word = int(e["instr"], 16) if e["instr"] else 0
        rec: Dict[str, Any] = {
            "schema_version": "2.1.0", "hart": 0, "seq": seq, "cycle": None,
            "pc": f"0x{int(e['pc'], 16):08x}", "instr": f"0x{word:08x}", "instr_width": 4,
            "priv": e["priv"], "trap": None,
            "regs": dict(e["regs"]), "csrs": dict(e["csrs"]), "_src": "iss",
        }
        if e["priv"] is None:   # an exception line prints no privilege
            del rec["priv"]
        if e["trap"]:
            rec["trap"] = {"cause": e["trap"]["cause"], "tval": "0x00000000",
                           "epc": e["trap"]["epc"]}
            if "tvec" in e["trap"]:
                rec["trap"]["tvec"] = e["trap"]["tvec"]
        if (word & 0x7f) == _STORE_OPCODE and not e["trap"]:
            size = 1 << ((word >> 12) & 3)
            rec["mem_writes"] = [{"addr": f"0x{int(m['addr'], 16):08x}", "size": size,
                                  "data": f"0x{int(m['data'], 16):0{2 * size}x}"}
                                 for m in e["mem"] if m["op"] == "store"]
        out.append(rec)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("log")
    ap.add_argument("elf")
    ap.add_argument("-o", "--output", default="-")
    args = ap.parse_args(argv)
    recs = convert(Path(args.log).read_text(encoding="utf-8"), Path(args.elf))
    text = "".join(json.dumps(r) + "\n" for r in recs)
    if args.output == "-":
        sys.stdout.write(text)
    else:
        Path(args.output).write_text(text, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
