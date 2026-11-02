"""#46: _print_summary must not raise on a bug whose severity is present but not a str."""
from ava_patched import AVA


def _results(bug):
    return {
        "run_id": "r", "status": "failed", "execution_time": 0.1,
        "industrial_grade": False,
        "semantic_map": {"dut_module": "core"},
        "extended_reports": {},
        "initial_results": {"coverage": {}, "bugs": [bug]},
        "metadata": {"run_dir": "x"},
        "perf_analysis": {}, "security_report": {}, "adaptive_stimulus": [],
    }


def _summary(bug, capsys):
    ava = AVA(enable_llm=False, enable_database=False)
    ava._print_summary(_results(bug))
    return capsys.readouterr().out


def test_none_severity_renders_question_mark(capsys):
    out = _summary({"severity": None, "kind": "pc_mismatch", "description": "d"}, capsys)
    assert "[?] pc_mismatch" in out


def test_missing_severity_renders_question_mark(capsys):
    out = _summary({"kind": "pc_mismatch", "description": "d"}, capsys)
    assert "[?] pc_mismatch" in out


def test_string_severity_is_uppercased(capsys):
    out = _summary({"severity": "high", "kind": "pc_mismatch", "description": "d"}, capsys)
    assert "[HIGH] pc_mismatch" in out


def test_non_string_severity_is_stringified(capsys):
    out = _summary({"severity": 3, "kind": "pc_mismatch", "description": "d"}, capsys)
    assert "[3] pc_mismatch" in out
