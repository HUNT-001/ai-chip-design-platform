#!/usr/bin/env python3
"""Spike --log-commits output -> commit-log JSONL (schema 2.2.0), the ISS side.

The repo's own parser (`AGENT_F.coverage_pipeline.parse_spike_log`) reads the
log, and `align_to_entry` starts it at the ELF entry.  This is the same path
tests/test_comparator_mutation_table.py takes.  The records are shaped by the
schema, the same way validation/ibex/trace_to_commitlog.py shapes the Ibex
side.  The log the CLI writes opens with a fields_reported header (#177,
docs/dut_commit_log.md section 4) listing exactly FIELDS_REPORTED, and every
record carries every listed field:

* `regs` is the register write, `{}` when there is none.
* `csrs` is the CSR writes Spike logs, `{}` when there are none.  Those are
  the explicit writes only: Spike logs no implicit trap-entry write (mepc,
  mcause, mstatus, ...), so a trapping record's `{}` means "none logged".
* `mem_writes` is the store, its size taken from funct3 (Spike prints none),
  and null on every other record.
* `priv` is the privilege Spike prints on a commit line: the mode the
  instruction ran in, stashed before it executes.  An exception line prints
  none, so a trapping record takes the mode of the line before it, which is the
  mode the trap was taken from, unless that line is an mret/sret/dret/mnret
  (which change the mode) or itself a trap.  Then it is not derivable and the
  converter raises ValueError naming the epc.  It never writes null.
* `trap` is an object on an exception line and null otherwise.  Its `tvec` is
  the last mtvec Spike logged before the trap, mode bits cleared (#184).  With
  no mtvec write before the trap the converter raises ValueError naming the
  epc rather than invent one.  Spike prints no `tval`, so a trap carries
  `0x00000000`, as the mutation table's does.

`cycle` is not reported: an ISS has none.  A line the parser does not
recognise (an interrupt line, for one) makes the converter raise, so nothing
is dropped silently.

`convert` returns the records; `declared_log` puts the header in front of
them, and that is the log.

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

SCHEMA_VERSION = "2.2.0"
#: What Spike's commit log prints, and so all a record carries (see the docstring).
FIELDS_REPORTED = ("csrs", "mem_writes", "priv", "regs", "trap")

_STORE_OPCODE = 0b0100011
#: Instructions that leave the privilege mode different from the one they ran in.
_XRET = {0x30200073: "mret", 0x10200073: "sret", 0x7b200073: "dret", 0x70200073: "mnret"}


def _trap_priv(entries: List[Dict[str, Any]], i: int) -> str:
    """The mode the trap at entries[i] was taken from, or ValueError."""
    epc = entries[i]["trap"]["epc"]
    prev = entries[i - 1] if i > 0 else None
    if prev is None:
        why = "it is the first line of the log"
    elif prev["trap"]:
        why = "the line before it is a trap too"
    elif prev["instr"] and int(prev["instr"], 16) in _XRET:
        why = f"the line before it is an {_XRET[int(prev['instr'], 16)]}, which changes the mode"
    elif prev["priv"] is None:
        why = "the line before it prints no privilege"
    else:
        return prev["priv"]
    raise ValueError(f"trap at epc {epc}: Spike prints no privilege on an exception line "
                     f"and it cannot be derived: {why}")


def convert(log_text: str, elf: Path) -> List[Dict[str, Any]]:
    parsed = cp.parse_spike_log(log_text)
    if parsed.unclassified:
        raise ValueError(f"{len(parsed.unclassified)} Spike line(s) this converter cannot "
                         f"represent, the first: {parsed.unclassified[0]!r}")
    entries = cp.with_trap_vectors(parsed.entries)
    entries = [{**e, "priv": _trap_priv(entries, i)} if e["trap"] else e
               for i, e in enumerate(entries)]
    aligned, reason = cp.align_to_entry(entries, cp.elf_entry_pc(elf))
    if reason is not None:
        raise ValueError(f"{elf}: cannot align the Spike log: {reason}")
    out: List[Dict[str, Any]] = []
    for seq, e in enumerate(aligned):
        if not e["instr"]:
            raise ValueError(f"{e['pc']}: no instruction word in the log for this record")
        word = int(e["instr"], 16)
        rec: Dict[str, Any] = {
            "schema_version": SCHEMA_VERSION, "hart": 0, "seq": seq,
            "pc": f"0x{int(e['pc'], 16):08x}", "instr": f"0x{word:08x}", "instr_width": 4,
            "priv": e["priv"], "trap": None,
            "regs": dict(e["regs"]), "csrs": dict(e["csrs"]), "mem_writes": None,
            "_src": "iss",
        }
        if e["trap"]:
            if "tvec" not in e["trap"]:
                raise ValueError(f"trap at epc {e['trap']['epc']}: no mtvec write is logged "
                                 f"before it, so mtvec is its reset value, which the log "
                                 f"does not show; no tvec can be given")
            rec["trap"] = {"cause": e["trap"]["cause"], "tval": "0x00000000",
                           "tvec": e["trap"]["tvec"], "epc": e["trap"]["epc"]}
        elif (word & 0x7f) == _STORE_OPCODE:
            size = 1 << ((word >> 12) & 3)
            stores = [m for m in e["mem"] if m["op"] == "store"]
            if not stores:
                raise ValueError(f"{rec['pc']}: a store with no store logged on its line")
            rec["mem_writes"] = [{"addr": f"0x{int(m['addr'], 16):08x}", "size": size,
                                  "data": f"0x{int(m['data'], 16):0{2 * size}x}"}
                                 for m in stores]
        out.append(rec)
    return out


def header_record() -> Dict[str, Any]:
    return {"_header": {"schema_version": SCHEMA_VERSION,
                        "fields_reported": list(FIELDS_REPORTED)}}


def declared_log(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The log as the CLI writes it: the header, then the records."""
    return [header_record(), *records]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("log")
    ap.add_argument("elf")
    ap.add_argument("-o", "--output", default="-")
    args = ap.parse_args(argv)
    recs = declared_log(convert(Path(args.log).read_text(encoding="utf-8"), Path(args.elf)))
    text = "".join(json.dumps(r) + "\n" for r in recs)
    if args.output == "-":
        sys.stdout.write(text)
    else:
        Path(args.output).write_text(text, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
