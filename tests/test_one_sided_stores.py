"""A store field reported by one side only is not compared; it must not pass (#164).

Rule, from the issue, per compared field:

  * present on both sides            -> compare
  * reported empty ([]) is not absent (None)
  * exactly one side None            -> the run is incomplete, with a reason that
                                        names the field and the side, e.g.
                                        field_not_reported:stores:rtl.  Not a mismatch.
  * both None                        -> recorded in the run metadata as not
                                        compared, not silently equal.

Inputs are the recorded Spike log of rv32im_smoke (tests/fixtures/spike/,
PROVENANCE.md), which contains 2 stores, run through the real parser, the real
alignment and the real verdict.  Only the two simulator processes are replaced.
Spike prints no access size for a store, so the DUT's `size` is not compared.
"""
import asyncio
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import AGENT_F.coverage_pipeline as cp
import ava_patched
from ava_patched import AVA, SpikeISS

FIX = REPO_ROOT / "tests" / "fixtures" / "spike"
SMOKE_ELF = FIX / "rv32im_smoke.elf"
SMOKE_TEXT = (FIX / "rv32im_smoke.log").read_text(encoding="utf-8")


def _aligned(text):
    log = cp.parse_spike_commit_log(text)
    aligned, reason = cp.align_to_entry(log, cp.elf_entry_pc(SMOKE_ELF))
    assert reason is None
    return aligned


def _stores(entry):
    return [m for m in entry["mem"] if m["op"] == "store"]


def _no_store_prefix_text():
    """The recorded log up to, not including, its first store commit line."""
    lines = SMOKE_TEXT.splitlines(keepends=True)
    cut = next(i for i, l in enumerate(lines) if "mem 0x" in l and
               len(l.split("mem ")[1].split()) == 2)
    return "".join(lines[:cut])


def _dut(iss, stores):
    """The DUT parser's shape for the same instructions.  `stores` is None (the
    key is not reported), "match" (reported from the ISS stores) or "different"."""
    out = []
    flipped = False
    for e in iss:
        d = {"pc": e["pc"], "instr": e["instr"], "rd": e["rd"], "rd_val": e["rd_val"]}
        if stores is not None:
            writes = [{"addr": m["addr"], "size": 4, "data": m["data"]} for m in _stores(e)]
            if stores == "different" and writes and not flipped:
                writes[0]["data"] = "0x0badf00d"
                flipped = True
            d["mem_writes"] = writes
        out.append(d)
    return out


def _run(tmp_path, monkeypatch, text, stores):
    iss = _aligned(text)
    end = ava_patched.terminal_self_loop(iss)
    iss_program = iss if end is None else iss[:end + 1]
    dut = _dut(iss_program, stores)

    async def _rtl(self, tb_suite, semantic_map):
        return {"cycles": 10, "instructions": len(dut), "coverage_data": {},
                "state_snapshots": [], "commit_log": dut, "seed": 1,
                "performance": {"ipc": 0.1, "branch_predictions": 0, "branch_correct": 0}}

    async def _process(self, cmd, cwd, timeout_msg, launch_msg):
        return types.SimpleNamespace(returncode=0), b"", text.encode()

    original = AVA._generate_tb_suite

    async def _tb_suite(self, *args, **kwargs):
        suite = await original(self, *args, **kwargs)
        suite["elf_path"] = str(SMOKE_ELF)
        self.spike_iss.spike_binary = sys.executable
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
    verdict = result["extended_reports"][ava_patched.VERDICT_KEY]
    return result, verdict.get("incomplete", []), verdict.get("violations", [])


def _field_labels(incomplete):
    return [l for l in incomplete if l.startswith("field_not_reported")]


def test_the_fixture_really_has_two_stores_and_a_storeless_prefix():
    assert sum(len(_stores(e)) for e in _aligned(SMOKE_TEXT)) == 2
    assert all(not _stores(e) for e in _aligned(_no_store_prefix_text()))
    assert all(e["mem"] is not None for e in _aligned(_no_store_prefix_text())), \
        "the ISS must report [] (not absent) for a program with no stores"


# (a) the DUT log has no store key at all
def test_a_dut_that_reports_no_stores_is_incomplete_and_the_reason_names_field_and_side(
        tmp_path, monkeypatch):
    result, incomplete, violations = _run(tmp_path, monkeypatch, SMOKE_TEXT, None)
    assert _field_labels(incomplete) == ["field_not_reported:stores:rtl"], incomplete
    assert result["status"] == "incomplete"
    assert result["initial_results"]["bugs"] == [], "not reporting is not a mismatch"


# (b) matching stores
def test_a_dut_with_matching_stores_is_not_incomplete_on_stores(tmp_path, monkeypatch):
    result, incomplete, violations = _run(tmp_path, monkeypatch, SMOKE_TEXT, "match")
    assert _field_labels(incomplete) == [], incomplete
    assert result["initial_results"]["bugs"] == []
    meta = result["initial_results"]["metadata"]
    assert "stores" not in (meta.get("fields_not_compared") or {}), meta.get("fields_not_compared")


# (c) a different store
def test_a_different_store_is_a_mismatch_not_a_missing_field(tmp_path, monkeypatch):
    result, incomplete, violations = _run(tmp_path, monkeypatch, SMOKE_TEXT, "different")
    kinds = [b["kind"] for b in result["initial_results"]["bugs"]]
    assert kinds == [ava_patched.MismatchKind.MEMORY.value], kinds
    assert _field_labels(incomplete) == []
    assert result["status"] == "failed"


# (d) anti-vacuity: the ISS reports [] and the DUT reports nothing -> still incomplete
def test_an_empty_iss_list_against_an_absent_dut_key_is_still_incomplete(tmp_path, monkeypatch):
    result, incomplete, _ = _run(tmp_path, monkeypatch, _no_store_prefix_text(), None)
    assert _field_labels(incomplete) == ["field_not_reported:stores:rtl"], incomplete
    assert result["status"] == "incomplete"


def test_a_reported_empty_list_on_both_sides_is_compared_and_clean(tmp_path, monkeypatch):
    """The control for (d): the DUT reports [] for each entry, so nothing is absent."""
    result, incomplete, _ = _run(tmp_path, monkeypatch, _no_store_prefix_text(), "match")
    assert _field_labels(incomplete) == [], incomplete
    assert result["initial_results"]["bugs"] == []


# both None -> recorded as not compared
def test_a_field_neither_side_reports_is_recorded_as_not_compared(tmp_path):
    iss = [{"pc": "0x80000000", "instr": "0x00000013", "rd": "", "rd_val": ""},
           {"pc": "0x80000004", "instr": "0x00000013", "rd": "", "rd_val": ""}]
    rtl = [dict(e) for e in iss]
    sim = SpikeISS(timeout=30, run_dir=tmp_path / "cmp")
    comparison = asyncio.run(sim._compare_results(
        {"commit_log": rtl, "instructions": 2}, {"commit_log": iss, "instructions": 2}))
    assert comparison["bugs"] == []
    assert comparison.get("unobserved") == []
    assert (comparison.get("fields_not_compared") or {}).get("stores") == 2, comparison


def test_a_changed_spike_store_is_read_by_the_comparator_though_the_ratchet_probe_hits_a_load():
    """PARSED_NOT_COMPARED keeps `mem` because Spike loads are still ignored; the
    ratchet's probe lands on a load, so this pins that the stores in `mem` are read."""
    import copy
    from ava_patched import CommitLogComparator
    iss = _aligned(SMOKE_TEXT)
    i = next(i for i, e in enumerate(iss) if _stores(e))
    rtl = copy.deepcopy(iss)
    store = next(m for m in rtl[i]["mem"] if m["op"] == "store")
    store["data"] = "0x0badf00d"
    bugs = CommitLogComparator().compare(rtl, iss)
    assert [b.kind for b in bugs] == [ava_patched.MismatchKind.MEMORY], bugs
    loaded = next(j for j, e in enumerate(iss) if e["mem"] and not _stores(e))
    rtl = copy.deepcopy(iss)
    rtl[loaded]["mem"][0]["addr"] = "0x0badf00d"
    assert CommitLogComparator().compare(rtl, iss) == [], "loads are documented as not compared"
