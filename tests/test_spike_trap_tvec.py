"""Converted Spike trap records meet the schema: tvec from mtvec, no null priv (#184).

Written from AGENT_A/commitlog.schema.json 2.2.0 and the recorded Spike logs in
tests/fixtures/spike/ (see PROVENANCE.md), not from the converters:

* `$defs.trap_object` requires `tvec`, defined as "mtvec at the moment the trap is
  taken (BASE field, mode stripped)".  Spike prints no mtvec on an exception line,
  but it prints every CSR write on the commit line that makes it.  The mtvec in
  force at a trap is therefore the last mtvec write logged before the exception
  line, with the two MODE bits cleared.  When the log holds no mtvec write before
  the trap, mtvec is whatever the reset value was, which the log does not show:
  the converter must then leave tvec out rather than invent one, and the record
  stays invalid.
* `priv` is "M"/"S"/"U"/"D", never null.  Spike prints the privilege digit only on
  a commit line, and a trapping instruction gets an exception line instead, so
  the trapping record carries no `priv` key at all (optional since 2.2.0).

csr_trap.log, checked by hand:
  line 16  `core 0: 3 0x80000008 (0x30529073) c773_mtvec 0x80000070`  the only
           mtvec write; MODE = 0b00 (direct), BASE = 0x80000070
  line 42  `core 0: exception trap_machine_ecall, epc 0x8000003c`     the trap
  line 44  `core 0: 0x80000070 (0x342024f3) csrr s1, mcause`          the first
           handler instruction is at BASE, which agrees
So the trapping record's tvec is 0x80000070.

Converters: validation/ibex/spike_to_commitlog.py, and the mutation table's own
`_canonical` (tests/test_comparator_mutation_table.py), which builds its trap
records the same way.  The mutation-table records are comparator inputs, not
schema logs (no instr_width/_src, `mem_writes: []` on every record), so only
their trap object is held to `$defs.trap_object` here.
"""
import hashlib
import importlib.util
import json
from pathlib import Path

import jsonschema
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SPIKE_FIX = REPO_ROOT / "tests" / "fixtures" / "spike"
IBEX_FIX = REPO_ROOT / "validation" / "ibex" / "fixtures"
SCHEMA = json.loads((REPO_ROOT / "AGENT_A" / "commitlog.schema.json").read_text(encoding="utf-8"))

#: by hand from csr_trap.log, see the module docstring
CSR_TRAP_TVEC = "0x80000070"
CSR_TRAP_EPC = "0x8000003c"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


S2C = _load("tvec_spike_to_commitlog", REPO_ROOT / "validation" / "ibex" / "spike_to_commitlog.py")
CV = _load("tvec_commitlog_validator", REPO_ROOT / "AGENT_A" / "commitlog_validator.py")

CSR_TRAP_LOG = (SPIKE_FIX / "csr_trap.log").read_text(encoding="utf-8")
CSR_TRAP_ELF = SPIKE_FIX / "csr_trap.elf"
MTVEC_WRITE = "core   0: 3 0x80000008 (0x30529073) c773_mtvec 0x80000070"
EXCEPTION = "core   0: exception trap_machine_ecall, epc 0x8000003c"


def _jsonl(records):
    return "".join(json.dumps(r) + "\n" for r in records)


def _traps(records):
    return [r for r in records if r.get("trap")]


def _trap_validator():
    sub = dict(SCHEMA["$defs"]["trap_object"])
    sub["$defs"] = SCHEMA["$defs"]
    return jsonschema.Draft202012Validator(sub)


def test_the_hand_checked_lines_are_in_the_fixture():
    """Anti-vacuity: the evidence the expected tvec rests on is really there."""
    lines = CSR_TRAP_LOG.splitlines()
    assert lines[15] == MTVEC_WRITE
    assert lines[41] == EXCEPTION
    assert lines[43].startswith("core   0: 0x80000070 (0x342024f3) csrr")
    assert sum("_mtvec " in l for l in lines) == 1
    assert sum("exception" in l for l in lines) == 1


# ── validation/ibex/spike_to_commitlog.py ────────────────────────────────────

def test_the_converted_csr_trap_log_is_valid():
    recs = S2C.convert(CSR_TRAP_LOG, CSR_TRAP_ELF)
    assert len(_traps(recs)) == 1, "the fixture's one trap must be converted"
    rep = CV.validate_records(recs)
    assert rep["valid"] is True, rep


def test_the_trap_carries_the_mtvec_in_force():
    (t,) = _traps(S2C.convert(CSR_TRAP_LOG, CSR_TRAP_ELF))
    assert t["pc"] == CSR_TRAP_EPC
    assert t["trap"]["epc"] == CSR_TRAP_EPC
    assert t["trap"]["tvec"] == CSR_TRAP_TVEC


def test_no_record_has_a_null_priv_and_the_trap_has_none():
    recs = S2C.convert(CSR_TRAP_LOG, CSR_TRAP_ELF)
    assert [r["seq"] for r in recs if "priv" in r and r["priv"] is None] == []
    (t,) = _traps(recs)
    assert "priv" not in t, "Spike prints no privilege on an exception line"
    assert all(r.get("priv") in ("M", "S", "U", "D") for r in recs if r is not t)


def test_mtvec_mode_bits_are_stripped():
    log = CSR_TRAP_LOG.replace(MTVEC_WRITE, MTVEC_WRITE[:-1] + "1")   # vectored
    (t,) = _traps(S2C.convert(log, CSR_TRAP_ELF))
    assert t["trap"]["tvec"] == CSR_TRAP_TVEC


def test_no_mtvec_write_before_the_trap_means_no_tvec():
    """The rule must not apply: mtvec is its reset value, which the log does not
    show.  No tvec is invented, and the log stays invalid."""
    log = CSR_TRAP_LOG.replace(MTVEC_WRITE + "\n", "")
    (t,) = _traps(S2C.convert(log, CSR_TRAP_ELF))
    assert "tvec" not in t["trap"]
    assert CV.validate_records(S2C.convert(log, CSR_TRAP_ELF))["valid"] is False


def test_an_mtvec_write_after_the_trap_does_not_count():
    log = CSR_TRAP_LOG.replace(MTVEC_WRITE + "\n", "")
    log = log.replace(EXCEPTION + "\n", EXCEPTION + "\n" + MTVEC_WRITE + "\n", 1)
    (t,) = _traps(S2C.convert(log, CSR_TRAP_ELF))
    assert "tvec" not in t["trap"]


def test_a_trap_that_ends_the_log_still_takes_the_logged_mtvec():
    """The rule reads mtvec, not the next PC, so a log that stops at the trap
    (no handler instruction after it) gives the same tvec."""
    cut = CSR_TRAP_LOG[:CSR_TRAP_LOG.index(EXCEPTION) + len(EXCEPTION) + 1]
    recs = S2C.convert(cut, CSR_TRAP_ELF)
    assert recs[-1]["trap"]["tvec"] == CSR_TRAP_TVEC


#: sha256 of each non-trapping log's converted JSONL, recorded on main at f15766a
#: before the #184 fix: output with no trap in it must not move by one byte.
UNCHANGED = {
    "spike/rv32im_smoke": "4e3f283ee71b26833199685f85e45b992e63df717516e347e654fbbd753140b8",
    "ibex-spike/arith_branch": "de7c25e4e65621040c91a5938be6a69dd72bd039ab9d690e97d0551963e481b7",
    "ibex-spike/csr": "e7c03c1b406a5869fcfe624adf454a00686597755cdaf4809aa877e402d3e79a",
    "ibex-spike/loadstore": "18e445cc5c34edbb1d868b9299777f4b86285eadd708b82eea238aaf2b96bd02",
    "ibex-spike/muldiv": "8e9f785b7b01967fb12f9610f44f0944ea6fdda357fa3fb18e2e8ff9500af51d",
}


def _source(name):
    kind, stem = name.split("/")
    if kind == "spike":
        return SPIKE_FIX / f"{stem}.log", SPIKE_FIX / f"{stem}.elf"
    return IBEX_FIX / f"{stem}.spike.log", IBEX_FIX / f"{stem}.elf"


def test_every_non_trapping_spike_fixture_is_listed():
    """Anti-vacuity: a new fixture without a trap is held to the byte check."""
    names = {f"spike/{p.stem}" for p in SPIKE_FIX.glob("*.elf") if p.stem != "csr_trap"}
    names |= {f"ibex-spike/{p.stem}" for p in IBEX_FIX.glob("*.elf")}
    assert names == set(UNCHANGED)


@pytest.mark.parametrize("name", sorted(UNCHANGED))
def test_a_log_without_a_trap_converts_byte_for_byte_as_before(name):
    log, elf = _source(name)
    recs = S2C.convert(log.read_text(encoding="utf-8"), elf)
    assert _traps(recs) == []
    assert hashlib.sha256(_jsonl(recs).encode("utf-8")).hexdigest() == UNCHANGED[name]


# ── the mutation table's converter ───────────────────────────────────────────

def test_the_mutation_tables_trap_record_carries_tvec():
    mt = _load("tvec_mutation_table", REPO_ROOT / "tests" / "test_comparator_mutation_table.py")
    (t,) = _traps(mt._canonical("csr_trap", 30))
    assert t["trap"]["tvec"] == CSR_TRAP_TVEC
    assert t["priv"] is not None
    errs = [e.message for e in _trap_validator().iter_errors(t["trap"])]
    assert errs == []
