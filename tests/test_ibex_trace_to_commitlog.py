"""validation/ibex/trace_to_commitlog.py: Ibex tracer text -> commit-log JSONL (#175).

Written from two sources, not from the converter:

* the tracer's own format strings in lowRISC Ibex `rtl/ibex_tracer.sv` at
  041098fe (the commit validation/ibex/PROVENANCE.md pins):

    header   "Time\\tCycle\\tPC\\tInsn\\tDecoded instruction\\tRegister and memory contents\\n"
    line     "%15t\\t%d\\t%h\\t%s\\t%s\\t" time, cycle, pc, insn, decoded
             then " %s:0x%08x" per rs read, " %s=0x%08x" for the rd write,
             " PA:0x%08x" and " store:0x??????%02x" / " store:0x????%04x" /
             " store:0x%08x" / " load:0x%08x" for memory, then "\\n"
    names    reg_addr_to_str: " x%0d" below 10, "x%0d" from 10
    insn     "%8h", or "    %4h" for a compressed parcel
    decoded  "<mnemonic>\\t<operands>", prefixed "-->" on a trap, "==>" on an interrupt

* AGENT_A/commitlog.schema.json 2.1.0.

The trace has no CSR values, no privilege mode, no trap cause and no perf
counters.  Those fields must be absent from every record -- not empty, not
guessed -- so a comparator can tell "not reported" from "reported nothing".
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CONVERTER = REPO_ROOT / "validation" / "ibex" / "trace_to_commitlog.py"
SCHEMA = REPO_ROOT / "AGENT_A" / "commitlog.schema.json"

HEADER = "Time\tCycle\tPC\tInsn\tDecoded instruction\tRegister and memory contents"

#: fields the Ibex text trace cannot report (#175 criterion b)
UNREPORTABLE = ("csrs", "priv", "trap", "perf_counters")


@pytest.fixture(scope="module")
def conv():
    if not CONVERTER.exists():
        pytest.fail(f"{CONVERTER.relative_to(REPO_ROOT)} does not exist")
    spec = importlib.util.spec_from_file_location("ibex_trace_to_commitlog", CONVERTER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def reg(n):
    """reg_addr_to_str: one leading space below x10."""
    return f" x{n}" if n < 10 else f"x{n}"


def rs(n, val):
    return f" {reg(n)}:0x{val:08x}"


def rd(n, val):
    return f" {reg(n)}={'0x%08x' % val}"


def line(pc, insn, decoded, regmem="", time=10, cycle=1, compressed=False):
    """One trace line exactly as printbuffer_dumpline writes it."""
    insn_s = f"    {insn:04x}" if compressed else f"{insn:08x}"
    return f"{time:>15}\t{cycle:>10}\t{pc:08x}\t{insn_s}\t{decoded}\t{regmem}"


ADDI = line(0x100080, 0x00100313, "addi\tx6,x0,1", rs(0, 0) + rd(6, 1), cycle=4)
ADD = line(0x100084, 0x00b50633, "add\tx12,x10,x11", rs(10, 5) + rs(11, 7) + rd(12, 12), cycle=5)
BEQ = line(0x100088, 0x00000063, "beq\tx0,x0,100088", rs(0, 0) + rs(0, 0), cycle=6)
SW = line(0x10008c, 0x00a42023, "sw\tx10,0(x8)",
          rs(8, 0x110000) + rs(10, 0x8899aabb) + " PA:0x00110000 store:0x8899aabb", cycle=7)
SB = line(0x100090, 0x00b400a3, "sb\tx11,1(x8)",
          rs(8, 0x110000) + rs(11, 0x11223344) + " PA:0x00110001 store:0x??????44", cycle=8)
SH = line(0x100094, 0x00c41323, "sh\tx12,6(x8)",
          rs(8, 0x110000) + rs(12, 0xcafef00d) + " PA:0x00110006 store:0x????f00d", cycle=9)
LW = line(0x100098, 0x00042283, "lw\tx5,0(x8)",
          rs(8, 0x110000) + rd(5, 0x8899aabb) + " PA:0x00110000 load:0x8899aabb", cycle=11)
CSRW = line(0x10009c, 0x34051073, "csrrw\tx0,mscratch,x10", rs(10, 0x5a5a1234) + rd(0, 0), cycle=12)
CSRR = line(0x1000a0, 0x34002473, "csrrs\tx8,mscratch,x0", rs(0, 0) + rd(8, 0x5a5a1234), cycle=13)

TRACE = [HEADER, ADDI, ADD, BEQ, SW, SB, SH, LW, CSRW, CSRR]


def records(conv, lines=TRACE):
    return conv.convert_lines(lines)


def by_pc(recs, pc):
    (r,) = [r for r in recs if r["pc"] == pc]
    return r


# ── one record per retired instruction ───────────────────────────────────────

def test_the_header_is_not_an_instruction(conv):
    assert len(records(conv)) == len(TRACE) - 1


def test_records_are_in_trace_order_with_a_gapless_seq(conv):
    recs = records(conv)
    assert [r["seq"] for r in recs] == list(range(len(recs)))
    assert [r["pc"] for r in recs] == [
        "0x00100080", "0x00100084", "0x00100088", "0x0010008c", "0x00100090",
        "0x00100094", "0x00100098", "0x0010009c", "0x001000a0"]


def test_fixed_fields_follow_the_schema(conv):
    r = by_pc(records(conv), "0x00100084")
    assert r["schema_version"] == "2.1.0"
    assert r["hart"] == 0
    assert r["_src"] == "rtl"
    assert r["instr"] == "0x00b50633"
    assert r["instr_width"] == 4
    assert r["cycle"] == 5


def test_a_compressed_parcel_is_four_hex_digits_and_width_two(conv):
    c = line(0x1000a4, 0x4501, "c.li\tx10,0", rd(10, 0), compressed=True)
    (r,) = conv.convert_lines([c])
    assert (r["instr"], r["instr_width"]) == ("0x4501", 2)


def test_the_disassembly_tab_does_not_shift_the_register_column(conv):
    """decoded is "<mnemonic>\\t<operands>", so the line has seven tab fields."""
    r = by_pc(records(conv), "0x00100084")
    assert r["regs"] == {"x12": "0x0000000c"}
    assert r["disasm"].split() == ["add", "x12,x10,x11"]


# ── registers ────────────────────────────────────────────────────────────────

def test_the_rd_write_is_the_register_delta(conv):
    recs = records(conv)
    assert by_pc(recs, "0x00100080")["regs"] == {"x6": "0x00000001"}
    assert by_pc(recs, "0x00100098")["regs"] == {"x5": "0x8899aabb"}


def test_rs_reads_are_never_writes(conv):
    for r in records(conv):
        assert set(r["regs"]) <= {"x5", "x6", "x8", "x12"}, r


def test_an_instruction_without_a_write_reports_an_empty_delta(conv):
    recs = records(conv)
    for pc in ("0x00100088", "0x0010008c", "0x00100090"):
        assert by_pc(recs, pc)["regs"] == {}


def test_a_zero_write_to_x0_is_dropped(conv):
    assert by_pc(records(conv), "0x0010009c")["regs"] == {}


def test_the_capability_form_of_a_write_reports_its_integer_part(conv):
    """decode_cheriot_auipcc_insn sets CD whether or not CHERIoT is enabled, so
    `auipc` is printed as " %s=0x%08x+0x%09x" even on a plain RV32 core.  With
    CHERIoT off the capability half is zero, and the write is the first half."""
    au = line(0x100080, 0x00010417, "auipc\tx8,0x10", " " + reg(8) + "=0x00110080+0x000000000")
    (r,) = conv.convert_lines([au])
    assert r["regs"] == {"x8": "0x00110080"}


def test_a_non_zero_capability_half_is_refused(conv):
    """Non-zero capability metadata is a CHERIoT result this schema cannot hold."""
    au = line(0x100080, 0x00010417, "auipc\tx8,0x10", " " + reg(8) + "=0x00110080+0x000000001")
    with pytest.raises(ValueError, match="capability"):
        conv.convert_lines([au])


def test_a_non_zero_write_to_x0_is_refused_not_dropped(conv):
    """Dropping it would hide exactly the bug the x0 rule exists to catch."""
    bad = line(0x100080, 0x00100013, "addi\tx0,x0,1", rs(0, 0) + rd(0, 1))
    with pytest.raises(ValueError, match="x0"):
        conv.convert_lines([bad])


# ── stores ───────────────────────────────────────────────────────────────────

def test_store_size_comes_from_funct3(conv):
    recs = records(conv)
    assert by_pc(recs, "0x0010008c")["mem_writes"] == [
        {"addr": "0x00110000", "size": 4, "data": "0x8899aabb"}]
    assert by_pc(recs, "0x00100090")["mem_writes"] == [
        {"addr": "0x00110001", "size": 1, "data": "0x44"}]
    assert by_pc(recs, "0x00100094")["mem_writes"] == [
        {"addr": "0x00110006", "size": 2, "data": "0xf00d"}]


@pytest.mark.parametrize("insn,printed,size,data", [
    (0x00b400a3, "0x11223344", 1, "0x44"),     # sb, trace printed the whole register
    (0x00c41323, "0x8899aabb", 2, "0xaabb"),   # sh
    (0x00c41323, "0x??????bb", 2, None),       # sh, but the trace kept only a byte
])
def test_store_data_is_masked_to_the_store_width(conv, insn, printed, size, data):
    st = line(0x100090, insn, "s?\tx11,0(x8)", rs(8, 0) + rs(11, 0) + f" PA:0x00110000 store:{printed}")
    if data is None:
        with pytest.raises(ValueError):
            conv.convert_lines([st])
        return
    (r,) = conv.convert_lines([st])
    assert r["mem_writes"] == [{"addr": "0x00110000", "size": size, "data": data}]


def test_a_store_opcode_without_store_data_is_refused(conv):
    st = line(0x100090, 0x00a42023, "sw\tx10,0(x8)", rs(8, 0) + rs(10, 0) + " PA:0x00110000")
    with pytest.raises(ValueError):
        conv.convert_lines([st])


def test_only_stores_carry_mem_writes(conv):
    """Schema: mem_writes is null or absent for every non-store instruction."""
    for r in records(conv):
        is_store = r["pc"] in ("0x0010008c", "0x00100090", "0x00100094")
        assert ("mem_writes" in r) == is_store, r


def test_loads_are_not_logged_by_default(conv):
    """Schema: mem_reads is optional and off by default."""
    assert "mem_reads" not in by_pc(records(conv), "0x00100098")


# ── what the trace cannot say ────────────────────────────────────────────────

@pytest.mark.parametrize("field", UNREPORTABLE)
def test_a_field_the_trace_cannot_report_is_absent(conv, field):
    for r in records(conv):
        assert field not in r, f"{field} present at {r['pc']}: {r.get(field)!r}"


def test_a_csr_instruction_still_has_no_csrs_field(conv):
    """The tracer prints the CSR name in the disassembly, never its value."""
    r = by_pc(records(conv), "0x001000a0")
    assert "csrs" not in r
    assert r["regs"] == {"x8": "0x5a5a1234"}


@pytest.mark.parametrize("marker", ["-->", "==>"])
def test_a_trap_or_interrupt_line_is_refused(conv, marker):
    """The trace marks a trap but gives no cause, tval or epc."""
    t = line(0x1000a4, 0x00000073, f"{marker}ecall")
    with pytest.raises(ValueError, match="0x001000a4"):
        conv.convert_lines([t])


def test_a_malformed_line_is_refused_not_skipped(conv):
    with pytest.raises(ValueError):
        conv.convert_lines([HEADER, "10\t1\t00100080"])


# ── against the schema ───────────────────────────────────────────────────────

def test_records_meet_the_schema_but_for_the_fields_the_trace_lacks(conv):
    import jsonschema
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    v = jsonschema.Draft202012Validator(schema)
    for r in records(conv):
        errors = sorted(e.message for e in v.iter_errors(r))
        assert errors == ["'csrs' is a required property", "'priv' is a required property"], (
            r["pc"], errors)


def test_the_cli_writes_one_json_line_per_record(conv, tmp_path):
    src, out = tmp_path / "trace_core_00000000.log", tmp_path / "ibex.jsonl"
    src.write_text("\n".join(TRACE) + "\n", encoding="utf-8")
    proc = subprocess.run([sys.executable, str(CONVERTER), str(src), "-o", str(out)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    lines = out.read_text(encoding="utf-8").splitlines()
    assert [json.loads(x) for x in lines] == records(conv)
