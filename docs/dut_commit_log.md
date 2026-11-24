# DUT commit log

This is what a DUT's commit log must contain for AVA to compare it against the
ISS. It also covers what each producer in the tree can and cannot report, and
how to check a log before you compare it.

The schema is `AGENT_A/commitlog.schema.json` (2.2.0). The validator is
`AGENT_A/commitlog_validator.py`. Anything marked **planned (PR 4 of
#177)** is not implemented yet. Do not rely on it.

## 1. Two formats

A DUT log reaches AVA in one of two forms.

**COMMIT text lines on the simulator's stdout.** The orchestrator's RTL run
(`ava_patched.py`, `SpikeISS._simulate_rtl`) reads these. The format is one line per
retired instruction:

```
COMMIT pc=0x80000010 instr=0x00c58533 rd=x10 val=0x00000001
```

`rd=… val=…` is optional. The line is matched by `_DUT_COMMIT_RE`
(`AGENT_F/coverage_pipeline.py`), or by `_parse_dut_log_fallback` in
`ava_patched.py` when `coverage_pipeline` is unavailable. Text outside the
match on a line is ignored, and so are lines that do not match. Each match
becomes an entry with `pc`, `instr`, `rd` and `rd_val`.

The format can carry nothing else: no stores, no CSRs, no privilege and no
trap. Each of those is absent from every entry. In comparator terms that means
"not reported" (section 3), not "none happened". COMMIT text is not JSONL, so it
cannot carry a header (section 4).

**Canonical JSONL.** One JSON object per line, one record per retired
instruction, in program order, as the schema defines it. AGENT_D's CLI
(`AGENT_D/compare_commitlogs.py`) reads this format. The Ibex validation
converters (`validation/ibex/trace_to_commitlog.py`,
`validation/ibex/spike_to_commitlog.py`) and the Spike parser
(`AGENT_C/spike_parser.py`) write it. Everything below is about this format.

## 2. Canonical JSONL fields

**Required on every record.** These seven fields are the record's identity, and
the schema's `required` lists exactly them:

| field | meaning |
|---|---|
| `schema_version` | `"2.2.0"`; `"2.1.0"` only in a headerless (legacy) log |
| `hart` | hart id, 0 for single-core |
| `seq` | retirement sequence number, from 0, +1 per record |
| `pc` | `0x` + 8 lowercase hex digits |
| `instr` | `0x` + 8 hex digits (or 4 for `instr_width` 2) |
| `instr_width` | 4, or 2 for a compressed parcel |
| `_src` | `rtl`, `iss`, `sail` or `manual` |

**Optional: a producer reports these or leaves them out.** These are every
other record property, and the only names a header may list:

| field | what it is | compared by |
|---|---|---|
| `regs` | register delta `{"x10": "0x…"}`, at most one entry, never `x0` | both comparators |
| `csrs` | CSR delta `{"mepc": "0x…"}` | AGENT_D |
| `mem_writes` | stores `[{addr, size, data}]`, or null | both (as "stores") |
| `priv` | `M`/`S`/`U`/`D` | AGENT_D ("privilege") |
| `trap` | `{cause, tval, tvec, epc, …}` or null | AGENT_D |
| `mem_reads` | loads, off by default | no |
| `cycle`, `perf_counters`, `clk_domain`, `reset_event`, `power_state`, `mem_seq`, `mem_order_tag`, `fp_regs`, `disasm`, `_run_id` | annotations for other agents | no, ignored by design |

The orchestrator does not compare `instr`, `csrs` or `priv` at all (#159). It
compares `trap` only when at least one side's log has a header (section 4).
Between two headerless logs it still does not compare traps.

Before 2.2.0 the schema also required `regs`, `csrs` and `priv`. Both
comparators already treated an absent `regs`/`csrs` as not reported (#164,
#176), and a real Ibex log has no `csrs` or `priv`. In 2.2.0 those three are
optional, and a header (section 4) says whether a producer reports them.

## 3. Reported versus absent (#164, #176)

An empty value is a report. An absent key is not.

- `"mem_writes": []` and `"csrs": {}` mean "reported, none here". A missing
  key (or null, for `regs`/`csrs`) means "this producer did not say".
- **Exactly one side reports a field:** AGENT_D exits 3 with
  `incomplete_reasons: ["field_not_reported:<field>:<side>"]`. This covers
  register write, `csrs`, privilege, store address/data/size and trap
  cause/tval/epc. The orchestrator does the same for stores and puts the reason
  in `unobserved`. Exit 3 is never a pass.
- **Neither side reports it:** the field is counted per entry in `not_compared`
  (AGENT_D) or `fields_not_compared` (orchestrator). It is not treated as equal.
- `--skip-fields` (AGENT_D) records a deliberate gap in `config.skip_fields`.
  For example, `mem_size` against Spike, or `csr_writes` against Ibex.

**The gap this format closes is `trap` (#177).** The schema text said "null (or
omitted)" means no trap, and AGENT_D reads an absent `trap` as `False`
(`AGENT_D/compare_commitlogs.py`, `CommitEntry.from_dict`). So a producer that
cannot see traps looks like a core that never traps. A headerless log has no way
to say otherwise. A header can (section 4), and AGENT_D honours it: under a
header that does not list `trap`, the trap channel is not reported.

## 4. `fields_reported`: the log header (schema 2.2.0)

A log may open with one header record. It is the first non-blank line of the
JSONL:

```json
{"_header": {"schema_version": "2.2.0", "fields_reported": ["cycle", "priv", "trap", "regs", "csrs", "mem_writes"]}}
```

The header is a line of the log, not a sidecar file or a CLI argument. Copying,
cutting or attaching a log therefore keeps its declaration. The schema defines
the header as `$defs.log_header`. `_header` holds exactly `schema_version`
(`"2.2.0"`) and `fields_reported`, a list of distinct names from the optional
fields in section 2. Any other key, an unknown name, an identity field such as
`pc`, a comparator label such as `stores`, `privilege` or `csr_writes`, or a
duplicate is a malformed header. Only one header is allowed, and only as the
first line. Every record under a header is 2.2.0.

The four rules:

1. **The list is authoritative.** A listed field missing from any record makes
   the log malformed. A field the list leaves out is not reported. Under a
   header, null is a value and absence is not. A producer that lists `trap`
   writes `"trap": null` on every record where no trap is taken. A record with
   no `trap` key is an error, not "no trap".
2. **A field one side lists and the other does not** gives exit 3 with
   `field_not_reported:<field>:<side>`. This is the existing behaviour, and the
   header makes it cover `trap` too. AGENT_D does this (PR 2 of #177). The
   orchestrator does it for `regs`, stores and `trap`, the fields it compares
   (PR 3 of #177), and puts the reason in `unobserved`.
3. **A field neither side lists** is recorded in the verdict as
   `fields_not_compared`. AGENT_D does this (PR 2 of #177), and so does the
   orchestrator (PR 3 of #177). The orchestrator's `fields_not_compared`
   counts the entries for each field.
4. **A record that carries a field the list does not name** makes the log
   malformed, even when its value is null. A producer may not claim less than
   it emits.

The validator (section 8) enforces the header's shape and rules 1 and 4. Rules
2 and 3 are comparator behaviour. AGENT_D reads the header and enforces all
four: a log that breaks its header is malformed and exits 2, as unreadable
input (`AGENT_D/README.md`, section 6). The orchestrator reads it in
`SpikeISS._compare_results` (`AGENT_A/commitlog_header.py`) and enforces the same
four rules. The exit code for a malformed log is the one difference: the
orchestrator exits **3**, incomplete, because its convention is that anything it
cannot verify is incomplete. The reason in `unobserved` names the side, the line
and the field, for example `malformed_log:rtl:line7:regs`. It is never a pass and
never a finding. Nothing is compared, so a mismatch earlier in the log is not
reported either. `tests/test_comparator_differential.py` holds the two tools to
the same outcome on every case and maps AGENT_D's 2 to the orchestrator's 3.

Under a header, a producer that cannot see traps leaves `trap` out of the list
and off every record. The trap channel is then named as not reported instead of
passing as "no traps".

## 5. The declared halt write (#178, #180, #181)

Spike keeps retiring the program's terminal self-loop (`j .`) until it next
polls `tohost`, so its log ends in thousands of copies of one commit. A
testbench that halts on a store, as Ibex's SimCtrl does, stops before it retires
that loop at all. The RTL log is then shorter than the ISS log, and an
undeclared early end is a finding: `instruction_count_mismatch` in the
orchestrator, `LENGTHMISMATCH` (exit 1) in AGENT_D. It has to be. A core that
stops a few instructions short of a short program would otherwise pass. The
orchestrator's 5% length tolerance alone would let it through.

The DUT side can declare why its log stopped:

```json
{"reason": "halt_write", "addr": "0x00020008"}
```

The orchestrator takes this as `rtl_results["rtl_ended"]`
(`ava_patched.align_program_end`, `declared_stop_problems`). AGENT_D takes it as
`--rtl-ended '<json>'` (`CompareConfig.rtl_ended`). It is checked, never
trusted:

- the last RTL entry must be a store to `addr`;
- the ISS's terminal self-loop must start exactly where the RTL log ends, with
  only the loop and its repeats after it.

A declaration that fails a check is a finding of its own. An accepted early end
is recorded in `program_end`. The address always comes from the declaration,
never from the comparator. For Ibex, `validation/ibex/compare.py` reads it from
the programs' link script.

In 2.2.0 the declaration still travels beside the log, not in the header. The
header defines no such key and rejects one.

## 6. What Spike cannot provide

From `spike --log-commits -l` as `AGENT_F/coverage_pipeline.parse_spike_commit_log`
reads it (recorded examples: `tests/fixtures/spike/`):

- **Store size.** Spike prints the address and data of a store but not its
  width. Compare with `--skip-fields mem_size`. The Ibex validation converter
  derives the size from funct3.
- **`tval`.** It is not printed on an exception line. The converters write
  `0x00000000`.
- **`tvec`.** `mtvec` is not printed at a trap, and the schema's `trap_object`
  requires it. The converted trap records therefore fail the schema today
  (#184).
- **The privilege of a trapping instruction.** Spike prints an exception line
  instead of a commit line, and that line has no privilege digit.
  `validation/ibex/spike_to_commitlog.py` writes `priv: null` there (#184).
- **`cycle` and perf counters.** An ISS has neither. It emits null, or omits
  them.
- Spike also retires the terminal self-loop until it polls `tohost` (section 5).

Spike does report register writes, CSR writes, privilege on every commit line,
stores, loads, and trap cause and epc.

## 7. What Ibex's text trace cannot provide

From `ibex_tracer.sv`'s format strings, as `validation/ibex/trace_to_commitlog.py`
reads them:

- **CSR values.** The tracer prints the CSR name in the disassembly, never the
  value written. Records have no `csrs`.
- **Privilege.** No record has `priv`.
- **Trap cause, tval, epc.** A trapped instruction is marked `-->` and an
  interrupt `==>`, with nothing else. The converter refuses such a line rather
  than guessing.
- **Perf counters.** No record has `perf_counters`.

It does report the register write, store address and data (sub-word data
appears masked, as `store:0x??????NN`), loads (not logged by default), cycle and
disassembly.

## 8. How to validate a DUT log

```bash
python3 AGENT_A/commitlog_validator.py dut.commitlog.jsonl          # summary + errors
python3 AGENT_A/commitlog_validator.py dut.commitlog.jsonl --json   # full report
```

| exit | meaning |
|---|---|
| 0 | valid |
| 1 | malformed: each error names its line |
| 3 | could not validate: the log is unreadable, or `jsonschema` is missing. This is not a pass |

From Python: `validate_file(path)`, `validate_lines(lines)` or
`validate_records(objs)` in `AGENT_A/commitlog_validator.py`. Each returns
`{"valid", "errors", "fields_reported", "records", "schema_version"}`.

The validator checks:

- every record against the record schema (Draft 2020-12, via `jsonschema`);
- the header's shape, and that it is the only header and comes first;
- that every record under a header is 2.2.0;
- rules 1 and 4.

A line that is not JSON is an error, and so is a log with no records. Blank
lines are skipped. The validator does not check `seq` continuity or compare
anything. That is the comparators' job.

These readers read the header (PR 2 and 3 of #177). They skip it, never count
it as an instruction or a record, and hold the records to it:

- AGENT_D (`AGENT_D/compare_commitlogs.py`): see section 4.
- the orchestrator (`ava_patched.SpikeISS._compare_results`): see section 4.
  The "RTL=n ISS=m" counts and the program-end alignment (section 5) see
  records only.
- `AGENT_C/run_iss.validate_commitlog`: `seq` is counted from the first
  record, and records under a header must be 2.2.0.
- `AGENT_A.semantic_analyzer`: `validate_record` accepts a well-formed header
  as the first record, and `SemanticAnalyzer.analyze` holds the log to it.

All four share `AGENT_A/commitlog_header.py`, except AGENT_D, which keeps its
own copy of the field lists so that it runs from any directory.
`tests/test_orchestrator_fields_reported.py` holds the copy to the schema.

The AGENT_H, AGENT_I and AGENT_K verifiers read `rtl_commit.jsonl` and
`iss_commit.jsonl` from a run directory. They do not read the header yet. A
pipeline run never hands them a commit log at all (#161). Run on their own
against a headered log, they would read the header as a record.

## 9. Legacy logs and deprecation

A log with no header is a legacy log. It is valid in 2.2.0, with records at
2.1.0 or 2.2.0. It is checked against the record schema only, and the report
says `fields_reported: "undeclared"`. Comparators keep today's per-field
inference for it (section 3). AGENT_D records `"fields_reported": {"rtl": …,
"iss": …}` in its result, with `"undeclared (legacy)"` for a headerless side.
The orchestrator records the same in its comparison and in the run's metadata.
Neither reader runs the strict validator on a legacy log (#184).

Every producer in the tree writes headerless 2.1.0 logs today:
`AGENT_C/spike_parser.py` and both `validation/ibex` converters. Moving them to
a header is **planned (PR 4 of #177)**.

**Deprecation.** One release after headers are supported end to end, an
undeclared log will be treated as incomplete (exit 3) rather than inferred.
This cutoff is documented here and not implemented.
