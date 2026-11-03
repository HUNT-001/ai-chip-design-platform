"""#86: `_llm_semantic_analysis` must degrade only on environment failures.

The handler turned every exception from the LLM call into the rule-based
fallback map, including programming errors (AttributeError, NameError,
TypeError) and invalid-argument errors from the client.  Those are our bugs and
must propagate; only "the LLM is unreachable or answered badly" may fall back.

Expected behaviour, from the issue and from the installed ollama client
(0.3.3 and 0.6.3):

* propagate: AttributeError, NameError, TypeError, ollama.RequestError and
  ValueError raised by the client call;
* fall back with the marked map: ollama.ResponseError (HTTP status error),
  httpx transport errors (connect failure, timeout), ConnectionError, OSError
  and asyncio.TimeoutError (reason "timeout", the others "other");
* a model reply that is valid JSON but not an object falls back with reason
  "parse_error".
"""
import asyncio
import importlib

import httpx
import ollama
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


def _replier(content):
    def chat(**kwargs):
        return {"message": {"content": content}}
    return chat


@pytest.mark.parametrize("exc", [
    AttributeError("no attribute"),
    NameError("name 'x' is not defined"),
    TypeError("not subscriptable"),
    ollama.RequestError("invalid request"),
    ValueError("invalid argument"),
], ids=lambda e: type(e).__name__)
def test_programming_and_argument_errors_propagate(
        ava_patched, monkeypatch, tmp_path, exc):
    with pytest.raises(type(exc)):
        _analyse(ava_patched, monkeypatch, tmp_path, _raiser(exc))


@pytest.mark.parametrize("exc,reason", [
    (ollama.ResponseError("server error", 500), "other"),
    (httpx.ConnectError("refused"), "other"),
    (httpx.ReadTimeout("slow"), "other"),
    (ConnectionError("refused"), "other"),
    (OSError("network unreachable"), "other"),
    (asyncio.TimeoutError(), "timeout"),
], ids=lambda p: type(p).__name__ if not isinstance(p, str) else p)
def test_environment_errors_return_the_marked_fallback(
        ava_patched, monkeypatch, tmp_path, exc, reason):
    sem = _analyse(ava_patched, monkeypatch, tmp_path, _raiser(exc))
    assert sem.dut_module == "fb_core"            # the rule-based map
    assert sem.metadata.get("llm_fallback") is True
    assert sem.metadata.get("llm_fallback_reason") == reason
    assert sem.metadata.get("llm_fallback_exception") == type(exc).__name__


@pytest.mark.parametrize("content", ["null", "[1, 2]", '"text"', "42"])
def test_non_object_json_reply_returns_the_marked_parse_error(
        ava_patched, monkeypatch, tmp_path, content):
    sem = _analyse(ava_patched, monkeypatch, tmp_path, _replier(content))
    assert sem.dut_module == "fb_core"
    assert sem.metadata.get("llm_fallback") is True
    assert sem.metadata.get("llm_fallback_reason") == "parse_error"


def test_object_reply_missing_the_required_field_returns_the_marked_parse_error(
        ava_patched, monkeypatch, tmp_path):
    # Valid JSON object, but no "dut_module": bad model output, not our bug.
    sem = _analyse(ava_patched, monkeypatch, tmp_path, _replier("{}"))
    assert sem.dut_module == "fb_core"
    assert sem.metadata.get("llm_fallback") is True
    assert sem.metadata.get("llm_fallback_reason") == "parse_error"
