"""#9: AGENT_A's schema examples must be accepted by the code that consumes them.

test_agent_a_schema_examples.py checks that each schema accepts its own examples.
That says nothing about the consumers: a parser or validator written against its
own idea of the layout can reject the documented shape while every fixture built
from that idea stays green (#1, #4).  So the examples go through the real code:

* each commitlog example through the comparator's reader (a record compared with
  itself must be read as exactly one step, with no parse warning);
* the run_manifest example through ``validate_manifest``.

Each has a negative control, because a reader that skips what it cannot parse
reports "no mismatches" just as happily as one that read everything.  The
examples are one-record excerpts of a longer log (seq 8, 9, 10, 55, 56), so they
are compared one at a time, never as a single log.
"""
import json
import pathlib

import pytest

import compare_commitlogs as cc
from AGENT_A.semantic_analyzer import validate_manifest

AGENT_A = pathlib.Path(__file__).resolve().parents[1] / "AGENT_A"


def _examples(name):
    return json.loads((AGENT_A / name).read_text(encoding="utf-8"))["examples"]


COMMITLOG = _examples("commitlog.schema.json")


def _compare_with_itself(tmp_path, record):
    log = tmp_path / "log.jsonl"
    log.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return cc.compare(str(log), str(log))


def test_there_are_commitlog_examples_to_read():
    assert len(COMMITLOG) >= 5


@pytest.mark.parametrize("index", range(len(COMMITLOG)))
def test_comparator_reads_every_commitlog_example(tmp_path, index):
    result = _compare_with_itself(tmp_path, COMMITLOG[index])
    assert result.stats.total_steps == 1, "the example was not read as a step"
    assert result.stats.rtl_parse_warnings == 0
    assert result.stats.iss_parse_warnings == 0
    assert result.passed and result.stats.total_mismatches == 0


def test_comparator_reader_notices_a_record_it_cannot_read(tmp_path):
    broken = {k: v for k, v in COMMITLOG[0].items() if k != "seq"}
    result = _compare_with_itself(tmp_path, broken)
    assert result.stats.total_steps == 0
    assert result.stats.rtl_parse_warnings > 0


def test_validate_manifest_accepts_the_schema_example():
    examples = _examples("run_manifest.schema.json")
    assert examples, "no run_manifest example to validate"
    for example in examples:
        assert validate_manifest(example) == []


def test_validate_manifest_notices_a_missing_required_field():
    example = dict(_examples("run_manifest.schema.json")[0])
    del example["run_id"]
    assert validate_manifest(example) != []
