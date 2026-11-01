"""A rule-based fallback for the LLM semantic map must say it is one (#77).

`_llm_semantic_analysis` turns every LLM-path failure into the regex-derived
`SemanticMap` and returns it with nothing marking it as the degraded one, so a
reader of the result cannot tell an LLM map from a fallback.

The contract these cases hold it to, written from the issue: when the fallback
ran, `SemanticMap.metadata` carries `llm_fallback` (True), `llm_fallback_reason`
(`timeout` | `parse_error` | `other`) and `llm_fallback_exception` (the exception
class name). When the LLM path succeeds `llm_fallback` is absent.

One failing input per route: the stub client raises exactly one exception.
Handlers are not narrowed here; that is a separate issue.
"""
import asyncio
import importlib
import json

import pytest

RTL = "module fb_core (input clk, input rst, output reg [31:0] data_out); endmodule"


@pytest.fixture
def ava_patched():
    return importlib.import_module("ava_patched")


def _analyse(ava_patched, monkeypatch, tmp_path, chat):
    class Client:
        pass

    client = Client()
    client.chat = chat
    monkeypatch.setattr(ava_patched, "_ollama", client, raising=False)
    ava = ava_patched.AVA(enable_llm=True, enable_database=False,
                          run_base_dir=str(tmp_path))
    return asyncio.run(ava._llm_semantic_analysis(RTL))


def _raiser(exc):
    def chat(**kwargs):
        raise exc
    return chat


def test_timeout_route_is_marked(ava_patched, monkeypatch, tmp_path):
    # The stub raises asyncio.TimeoutError from inside the executor call, which
    # reaches the timeout clause the same way a real 90 s wait_for expiry does.
    sem = _analyse(ava_patched, monkeypatch, tmp_path, _raiser(asyncio.TimeoutError()))
    assert sem.dut_module == "fb_core"            # it is the rule-based map
    assert sem.metadata.get("llm_fallback") is True
    assert sem.metadata.get("llm_fallback_reason") == "timeout"
    assert sem.metadata.get("llm_fallback_exception") == "TimeoutError"


@pytest.mark.parametrize("exc", [
    json.JSONDecodeError("bad", "{", 0),
    KeyError("message"),
    TypeError("not subscriptable"),
])
def test_parse_error_route_is_marked(ava_patched, monkeypatch, tmp_path, exc):
    sem = _analyse(ava_patched, monkeypatch, tmp_path, _raiser(exc))
    assert sem.metadata.get("llm_fallback") is True
    assert sem.metadata.get("llm_fallback_reason") == "parse_error"
    assert sem.metadata.get("llm_fallback_exception") == type(exc).__name__


def test_generic_exception_route_is_marked(ava_patched, monkeypatch, tmp_path):
    sem = _analyse(ava_patched, monkeypatch, tmp_path,
                   _raiser(ConnectionError("refused")))
    assert sem.metadata.get("llm_fallback") is True
    assert sem.metadata.get("llm_fallback_reason") == "other"
    assert sem.metadata.get("llm_fallback_exception") == "ConnectionError"


def test_llm_success_is_not_marked(ava_patched, monkeypatch, tmp_path):
    # Control: without it the three cases above could pass on a map that is
    # always marked.
    reply = {"message": {"content": json.dumps({"dut_module": "llm_core"})}}
    sem = _analyse(ava_patched, monkeypatch, tmp_path, lambda **kw: reply)
    assert sem.dut_module == "llm_core"           # it is the LLM map
    assert not sem.metadata.get("llm_fallback")
