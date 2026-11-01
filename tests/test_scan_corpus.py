"""voe/scan_corpus.py must not hide a parser defect (#72).

`candidates()` wrapped `parse_module` in `except Exception: continue`, so a
TypeError inside the parser dropped the file and the scan simply reported
fewer candidates.  `parse_module` has no `raise` and raised nothing on the
1,795 corpus files, so that handler could only ever hide a bug.
"""
import importlib.util
import os

import pytest

from AGENT_H import rtl_graph

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_spec = importlib.util.spec_from_file_location(
    "voe_scan_corpus", os.path.join(ROOT, "voe", "scan_corpus.py"))
scan_corpus = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scan_corpus)

# A self-contained leaf block that passes every selection filter.
LEAF = """module leaf_counter (
    input  logic       clk,
    input  logic       rst_n,
    input  logic       en,
    output logic [7:0] count,
    output logic       wrap
);

    logic [7:0] next;

    assign next = count + 8'd1;
    assign wrap = en && (count == 8'hff);

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            count <= 8'd0;
        end else if (en) begin
            count <= next;
        end
    end

endmodule
"""


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    (tmp_path / "leaf_counter.sv").write_text(LEAF)
    monkeypatch.setattr(scan_corpus, "CORPUS", str(tmp_path))
    return tmp_path


def test_a_parser_defect_surfaces(corpus, monkeypatch):
    calls = []

    def broken(src, path=""):
        calls.append(path)
        raise TypeError("parser defect")

    monkeypatch.setattr(rtl_graph, "parse_module", broken)
    with pytest.raises(TypeError, match="parser defect"):
        scan_corpus.candidates()
    assert calls, "the corpus file never reached the parser"


def test_a_normal_corpus_file_is_still_returned(corpus):
    rows = scan_corpus.candidates()
    assert [r["module"] for r in rows] == ["leaf_counter"]
    assert (rows[0]["in"], rows[0]["out"], rows[0]["sequential"]) == (3, 2, True)
