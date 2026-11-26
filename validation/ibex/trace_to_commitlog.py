#!/usr/bin/env python3
"""Ibex tracer text log -> commit-log JSONL (AGENT_A/commitlog.schema.json 2.2.0).

Input is the file `rtl/ibex_tracer.sv` writes (`trace_core_<hartid>.log`).
Each line after the header is one retired instruction:

    time \\t cycle \\t pc \\t insn \\t decoded \\t register-and-memory contents

`decoded` itself is "<mnemonic>\\t<operands>" for most instructions, so the
first four and the last tab field are fixed and the decoded text is whatever
lies between them.

What the trace reports, and so what a record carries: pc, instr, the rd write
(`regs`, x0 dropped), stores (`mem_writes`, size from funct3, data masked to
it), cycle, and the disassembly.  What it does not report stays ABSENT from
the record: `csrs` (the tracer prints a CSR's name, never its value), `priv`,
`trap` (a trap is marked with "-->" but carries no cause, tval or epc), and
`perf_counters`.  Absent means "not reported"; it is never filled with an
empty value or a guess, so a comparator can tell the two apart.

The log the CLI writes opens with a fields_reported header (#177,
docs/dut_commit_log.md section 4) listing exactly FIELDS_REPORTED.  Under it
every record carries every listed field: a non-store has `"mem_writes": null`
("reported, none here") and an instruction that writes no register has
`"regs": {}`.  `convert_lines` and `convert_file` return the records;
`declared_log` puts the header in front of them, and that is the log.

Loads are not logged (`mem_reads` is off by default in the schema).

The tracer prints `auipc` in its CHERIoT capability form,
"x8=0x00110080+0x000000000", even with CHERIoT off.  The integer half is the
write.  A non-zero capability half is refused.

The converter refuses -- raises ValueError naming the PC -- rather than
skipping anything it cannot represent honestly: a trap or interrupt line, a
non-zero write to x0, non-zero capability metadata, a store opcode without
store data, store data narrower than its funct3 width, or a line that does
not parse.

Usage: trace_to_commitlog.py TRACE [-o OUT.jsonl]
"""
import argparse
import json
import re
import sys
from typing import Any, Dict, Iterable, List

SCHEMA_VERSION = "2.2.0"
#: What the tracer prints, and so all a record carries (see the docstring).
FIELDS_REPORTED = ("cycle", "disasm", "mem_writes", "regs")
HEADER = "Time\tCycle\tPC\tInsn\tDecoded instruction\tRegister and memory contents"

# " x5=0x00000001", or the CHERIoT form " x8=0x00110080+0x000000000" that the
# tracer uses for auipc even with CHERIoT off (decode_cheriot_auipcc_insn).
_RD = re.compile(r"(?<![\w:])x(\d{1,2})=0x([0-9a-fA-F]{8})(?:\+0x([0-9a-fA-F]{9}))?(?![\w+])")
_PA = re.compile(r"\bPA:0x([0-9a-fA-F]{8})\b")
_STORE = re.compile(r"\bstore:0x([0-9a-fA-F?]{8})\b")

_STORE_OPCODE = 0b0100011
_STORE_BYTES = {0b000: 1, 0b001: 2, 0b010: 4}


def _store(pc: str, word: int, contents: str) -> Dict[str, Any]:
    funct3 = (word >> 12) & 0b111
    if funct3 not in _STORE_BYTES:
        raise ValueError(f"{pc}: store opcode with funct3={funct3:03b}, not sb/sh/sw")
    size = _STORE_BYTES[funct3]
    pa, data = _PA.search(contents), _STORE.search(contents)
    if pa is None or data is None:
        raise ValueError(f"{pc}: store instruction without an address and store data: {contents!r}")
    printed = data.group(1)
    # The tracer replaces the bytes it did not write with '?'.  The digits the
    # store width needs must all be real.
    kept = printed[-2 * size:]
    if "?" in kept:
        raise ValueError(f"{pc}: {size}-byte store but the trace printed only {printed!r}")
    value = int(kept, 16)
    return {"addr": f"0x{int(pa.group(1), 16):08x}", "size": size,
            "data": f"0x{value:0{2 * size}x}"}


def convert_line(text: str, seq: int) -> Dict[str, Any]:
    fields = text.split("\t")
    if len(fields) < 6:
        raise ValueError(f"trace line {seq}: expected at least 6 tab fields, got "
                         f"{len(fields)}: {text!r}")
    _time, cycle_s, pc_s, insn_s = (f.strip() for f in fields[:4])
    decoded = "\t".join(fields[4:-1])
    contents = fields[-1]
    try:
        cycle = int(cycle_s)
        pc_v = int(pc_s, 16)
        word = int(insn_s, 16)
    except ValueError as exc:
        raise ValueError(f"trace line {seq}: unparseable field in {text!r}") from exc
    pc = f"0x{pc_v:08x}"
    if len(insn_s) not in (4, 8):
        raise ValueError(f"{pc}: instruction field {insn_s!r} is neither 4 nor 8 hex digits")
    width = len(insn_s) // 2
    if decoded.startswith(("-->", "==>")):
        kind = "trap" if decoded.startswith("-->") else "interrupt"
        raise ValueError(f"{pc}: the trace marks a {kind} here but gives no cause, tval or "
                         f"epc; this channel cannot report it")

    regs: Dict[str, str] = {}
    for n_s, val_s, cap_s in _RD.findall(contents):
        n, val = int(n_s), int(val_s, 16)
        if cap_s and int(cap_s, 16) != 0:
            raise ValueError(f"{pc}: x{n} written with non-zero capability metadata "
                             f"0x{cap_s}; this schema has no field for it")
        if n == 0:
            if val != 0:
                raise ValueError(f"{pc}: the trace shows x0 written with 0x{val:08x}")
            continue
        if regs:
            raise ValueError(f"{pc}: more than one register write in {contents!r}")
        regs[f"x{n}"] = f"0x{val:08x}"

    rec: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "hart": 0,
        "seq": seq,
        "cycle": cycle,
        "pc": pc,
        "instr": f"0x{word:0{2 * width}x}",
        "instr_width": width,
        "disasm": " ".join(decoded.split())[:128],
        "regs": regs,
        "_src": "rtl",
    }
    if width == 4 and (word & 0x7f) == _STORE_OPCODE:
        rec["mem_writes"] = [_store(pc, word, contents)]
    elif _STORE.search(contents):
        raise ValueError(f"{pc}: store data on a non-store instruction: {contents!r}")
    else:
        rec["mem_writes"] = None    # listed in the header: null is "no store here"
    return rec


def header_record() -> Dict[str, Any]:
    return {"_header": {"schema_version": SCHEMA_VERSION,
                        "fields_reported": list(FIELDS_REPORTED)}}


def declared_log(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The log as the CLI writes it: the header, then the records."""
    return [header_record(), *records]


def convert_lines(lines: Iterable[str]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for raw in lines:
        text = raw.rstrip("\r\n")
        if not text.strip() or text == HEADER:
            continue
        out.append(convert_line(text, len(out)))
    return out


def convert_file(path: str) -> List[Dict[str, Any]]:
    with open(path, encoding="utf-8") as fh:
        return convert_lines(fh)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("trace")
    ap.add_argument("-o", "--output", default="-")
    args = ap.parse_args(argv)
    recs = declared_log(convert_file(args.trace))
    text = "".join(json.dumps(r) + "\n" for r in recs)
    if args.output == "-":
        sys.stdout.write(text)
    else:
        with open(args.output, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
