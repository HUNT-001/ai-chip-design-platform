"""The orchestrator's Spike parser against real Spike output (#158).

Every input here is a recorded fixture in tests/fixtures/spike/ (see
PROVENANCE.md there): real gcc, real Spike 1.1.1-dev, nothing hand-written.
Expected values are copied from those logs, line by line.

  (a) commit lines yield pc, instr, rd/rd_val, stores, CSR writes, privilege,
      and the trap, not just the -l disassembly lines;
  (b) accounting: every line of a real log is classified or explicitly
      ignored, and a line the parser does not know is reported, never dropped;
  (c) PARSED_NOT_COMPARED ratchet: every field the parser emits that
      CommitLogComparator does not compare is on the list, and nothing else is;
  (d) Spike's boot ROM is dropped by aligning on the ELF entry PC, and a log
      that never reaches the entry makes the run incomplete, with the reason.
"""
import asyncio
import collections
import copy
import struct
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import AGENT_F.coverage_pipeline as cp
import ava_patched
from ava_patched import AVA, CommitLogComparator, SpikeISS

FIX = REPO_ROOT / "tests" / "fixtures" / "spike"
SMOKE_LOG = FIX / "rv32im_smoke.log"
CSR_LOG = FIX / "csr_trap.log"
SMOKE_ELF = FIX / "rv32im_smoke.elf"


def _text(path):
    return path.read_text(encoding="utf-8")


def _first(entries, pc):
    return next((e for e in entries if e.get("pc") == pc), None)


# ── (a) fields ──────────────────────────────────────────────────────────────

# (fixture, pc, field, expected) -- each copied from the recorded log.
EXPECTED = [
    (CSR_LOG, "0x80000004", "instr",  "0x07028293"),   # 3 0x80000004 (0x07028293) x5  0x80000070
    (CSR_LOG, "0x80000004", "rd",     "x5"),
    (CSR_LOG, "0x80000004", "rd_val", "0x80000070"),
    (CSR_LOG, "0x80000004", "priv",   "M"),
    (CSR_LOG, "0x80000008", "csrs",   {"mtvec": "0x80000070"}),            # c773_mtvec 0x80000070
    (CSR_LOG, "0x80000080", "csrs",   {"mstatus": "0x00000080",            # mret: two CSR writes
                                       "mstatush": "0x00000000"}),
    (CSR_LOG, "0x80000034", "mem",    [{"op": "store", "addr": "0x80000090",
                                        "data": "0x00005a5a"}]),            # mem 0x80000090 0x00005a5a
    (CSR_LOG, "0x0000100c", "rd_val", "0x80000000"),                       # x5  0x80000000 mem 0x00001018
    (CSR_LOG, "0x0000100c", "mem",    [{"op": "load", "addr": "0x00001018"}]),
    (CSR_LOG, "0x8000003c", "instr",  "0x00000073"),                       # ecall: no commit line,
    (CSR_LOG, "0x8000003c", "trap",   {"name": "trap_machine_ecall",       # only an exception line
                                       "exception_code": 11, "epc": "0x8000003c"}),
    (SMOKE_LOG, "0x8000001c", "mem",  [{"op": "store", "addr": "0x80000130",
                                        "data": "0x00000011"}]),   # sw t2, 0(a0)
]


def _matches(got, want):
    if isinstance(want, dict) and isinstance(got, dict) and "name" in want:
        return all(got.get(k) == v for k, v in want.items())   # trap: these keys at least
    return got == want


def test_real_commit_lines_yield_every_field():
    parsed = {p: cp.parse_spike_commit_log(_text(p)) for p in (SMOKE_LOG, CSR_LOG)}
    lost = []
    for path, pc, field, want in EXPECTED:
        entry = _first(parsed[path], pc)
        got = None if entry is None else entry.get(field)
        if not _matches(got, want):
            lost.append(f"{path.name} {pc} {field}: want {want!r}, got {got!r}")
    # one entry per retired instruction, plus one per trapped instruction
    for path, want in ((SMOKE_LOG, 5000), (CSR_LOG, 5020 + 1)):
        if len(parsed[path]) != want:
            lost.append(f"{path.name}: {len(parsed[path])} entries, want {want}")
    assert lost == [], "fields lost from real Spike lines:\n  " + "\n  ".join(lost)


# ── (b) accounting ──────────────────────────────────────────────────────────

ACCOUNTED = {
    SMOKE_LOG: {"commit": 5000, "disasm": 73},
    CSR_LOG:   {"commit": 5020, "disasm": 33, "exception": 1, "symbol": 1},
}


def test_every_line_of_a_real_log_is_accounted_for():
    classify = getattr(cp, "classify_spike_line", None)
    assert classify is not None, "the parser has no line accounting: unmatched lines vanish"
    for path, want in ACCOUNTED.items():
        lines = _text(path).splitlines()
        got = collections.Counter(classify(line) for line in lines)
        assert dict(got) == want, f"{path.name}: {dict(got)}"
        assert sum(got.values()) == len(lines)


def test_a_line_the_parser_does_not_know_is_reported_not_dropped():
    parse = getattr(cp, "parse_spike_log", None)
    assert parse is not None, "the parser has no line accounting: unmatched lines vanish"
    real = _text(SMOKE_LOG).splitlines()
    commit = next(line for line in real if cp.classify_spike_line(line) == "commit")
    unknown = [commit + " v1 0x00000000",            # a real commit line, one unknown token more
               commit.split(":", 1)[1].strip()]      # the same line without its "core N:" prefix
    for line in unknown:
        assert cp.classify_spike_line(line) == "unknown", line
    log = parse("\n".join(real[:20] + unknown))
    assert log.unclassified == unknown
    assert len(log.entries) == sum(1 for line in real[:20]
                                   if cp.classify_spike_line(line) == "commit")


# ── (c) PARSED_NOT_COMPARED ratchet ─────────────────────────────────────────

def _perturb(value):
    """A different value of the same shape: change the first leaf."""
    if isinstance(value, dict):
        k = next(iter(value))
        return {**value, k: _perturb(value[k])}
    if isinstance(value, list):
        return [_perturb(value[0])] + value[1:]
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1
    if isinstance(value, str) and value.startswith("0x"):
        return f"0x{int(value, 16) ^ 1:0{len(value) - 2}x}"
    if value in ("M", "S", "U"):
        return "U" if value != "U" else "M"
    return f"{value}_changed"


def _comparator_reads(field, log):
    """True if changing `field` on one side of a real log makes the comparator
    report something.  Tried on the first entry where the field carries a value."""
    for i, entry in enumerate(log):
        if entry.get(field) not in (None, "", [], {}):
            rtl = copy.deepcopy(log)
            rtl[i][field] = _perturb(entry[field])
            return bool(CommitLogComparator().compare(rtl, copy.deepcopy(log)))
    return False


def test_every_parsed_field_the_comparator_ignores_is_listed():
    listed = getattr(cp, "PARSED_NOT_COMPARED", None)
    assert listed is not None, "no PARSED_NOT_COMPARED list: parsed-but-ignored fields are invisible"
    log = cp.parse_spike_commit_log(_text(CSR_LOG))
    emitted = {k for e in log for k in e}
    assert emitted, "nothing parsed: the ratchet would be vacuous"
    ignored = {f for f in emitted if not _comparator_reads(f, log)}
    assert "pc" not in ignored, "the probe itself is broken: a PC change must be caught"
    assert set(listed) == ignored, (
        f"not listed but ignored: {sorted(ignored - set(listed))}; "
        f"listed but compared or not emitted: {sorted(set(listed) - ignored)}")


# ── (d) boot ROM and the ELF entry ──────────────────────────────────────────

def test_the_boot_rom_is_dropped_by_aligning_on_the_elf_entry():
    entry_pc = cp.elf_entry_pc(SMOKE_ELF)
    assert entry_pc == 0x80000000                     # readelf, PROVENANCE.md
    log = cp.parse_spike_commit_log(_text(SMOKE_LOG))
    aligned, reason = cp.align_to_entry(log, entry_pc)
    assert reason is None
    assert aligned[0]["pc"] == "0x80000000"
    dropped = log[:len(log) - len(aligned)]
    assert [e["pc"] for e in dropped] == ["0x00001000", "0x00001004", "0x00001008",
                                          "0x0000100c", "0x00001010"]
    lo, hi = cp.SPIKE_BOOT_ROM
    assert all(lo <= int(e["pc"], 16) < hi for e in dropped)


def _elf_with_entry(tmp_path, entry):
    """The real smoke ELF with only e_entry rewritten (ELF32 little-endian)."""
    data = bytearray(SMOKE_ELF.read_bytes())
    assert data[:4] == b"\x7fELF" and data[4] == 1 and data[5] == 1
    struct.pack_into("<I", data, 24, entry)
    out = tmp_path / "moved_entry.elf"
    out.write_bytes(bytes(data))
    return out


def _run_against_recorded_spike(tmp_path, monkeypatch, elf):
    """A full run whose ISS output is the recorded smoke log. Only the process
    is replaced (CI has no Spike): parsing, alignment and the verdict are real."""
    async def _rtl(self, tb_suite, semantic_map):
        return {"cycles": 10, "instructions": 1, "coverage_data": {}, "state_snapshots": [],
                "performance": {"ipc": 0.1, "branch_predictions": 0, "branch_correct": 0},
                "commit_log": [{"pc": "0x80000000", "instr": "0x00a00293"}], "seed": 1}

    async def _process(self, cmd, cwd, timeout_msg, launch_msg):
        return types.SimpleNamespace(returncode=0), b"", SMOKE_LOG.read_bytes()

    original = AVA._generate_tb_suite

    async def _tb_suite(self, *args, **kwargs):
        suite = await original(self, *args, **kwargs)
        suite["elf_path"] = str(elf)
        self.spike_iss.spike_binary = sys.executable    # "installed": the process is replaced
        return suite

    monkeypatch.setattr(SpikeISS, "_simulate_rtl", _rtl)
    monkeypatch.setattr(SpikeISS, "_run_simulator", _process)
    monkeypatch.setattr(AVA, "_generate_tb_suite", _tb_suite)
    ava = AVA(enable_llm=False, timeout=60, target_coverage=90.0,
              run_base_dir=str(tmp_path / "runs"), enable_database=False)
    result = asyncio.run(ava.generate_suite(
        rtl_spec="module riscv_core(input wire clk, input wire rst);\n"
                 "  reg [31:0] fetch_pc;\nendmodule\n",
        microarch="in_order", seed=42, save_results=False))
    return result, result["extended_reports"][ava_patched.VERDICT_KEY].get("incomplete", [])


def test_a_log_that_never_reaches_the_entry_pc_is_incomplete(tmp_path, monkeypatch):
    elf = _elf_with_entry(tmp_path, 0x80000200)       # inside RAM, never executed
    result, incomplete = _run_against_recorded_spike(tmp_path, monkeypatch, elf)
    assert any("entry_pc" in label and "0x80000200" in label for label in incomplete), incomplete
    assert result["status"] == "incomplete"
    assert result["initial_results"]["bugs"] == [], "an unaligned log must not be compared"


def test_the_real_entry_pc_is_found_in_the_real_log(tmp_path, monkeypatch):
    """The control for the test above: same harness, the ELF as recorded."""
    _, incomplete = _run_against_recorded_spike(tmp_path, monkeypatch, SMOKE_ELF)
    assert not any("entry_pc" in label for label in incomplete), incomplete
